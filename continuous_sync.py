"""Continuous Knowledge Synchronization: GitHub Webhooks and Periodic Background Sync.

Keeps the company brain graph automatically up to date as GitHub commits, issues, PRs,
and documentation change, without requiring manual `@sigmaagent sync github` calls.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

import knowledge
from github_analysis import GitHubNotFound, GitHubUnavailable, is_safe_repo

log = logging.getLogger("continuous_sync")

DEFAULT_SYNC_INTERVAL = int(os.getenv("KNOWLEDGE_SYNC_INTERVAL_SECONDS", "300"))
WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def verify_github_signature(payload_bytes: bytes, signature_header: str | None, secret: str | None) -> bool:
    """Verify GitHub's HMAC-SHA256 signature from the X-Hub-Signature-256 header."""
    if not secret:
        # If no secret is configured, allow the webhook (permissive local development)
        return True
    if not signature_header:
        return False
    if not signature_header.startswith("sha256="):
        return False

    expected = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    received = signature_header[7:].strip()
    return hmac.compare_digest(expected, received)


def handle_github_webhook(graph: Any, event_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Ingest a GitHub webhook event into the FalkorDB graph in real time."""
    repo_data = payload.get("repository") or {}
    repo = repo_data.get("full_name") or ""
    if not repo:
        return {"status": "ignored", "reason": "missing repository in payload"}
    if not is_safe_repo(repo):
        raise GitHubNotFound(f"invalid repository {repo!r}")

    knowledge.ensure_indexes(graph)
    branch = repo_data.get("default_branch") or "main"
    graph.query(
        "MERGE (r:Repository {full_name: $repo}) SET r.url = $url, r.default_branch = $branch, r.synced_at = $now",
        {"repo": repo, "url": repo_data.get("html_url") or f"https://github.com/{repo}", "branch": branch, "now": _now()},
    )

    tasks = [(row[0], row[1]) for row in graph.ro_query("MATCH (t:Task) RETURN t.id, t.title").result_set]
    developers = [row[0] for row in graph.ro_query("MATCH (d:Developer) RETURN d.name").result_set]

    stats: dict[str, Any] = {"status": "processed", "event": event_name, "repo": repo}

    if event_name == "push":
        commits = payload.get("commits") or []
        commit_rows, task_links = [], []
        doc_files_changed: set[str] = set()

        for c in commits:
            sha = c.get("id") or c.get("sha")
            if not sha:
                continue
            message = (c.get("message") or "").strip()
            author_data = c.get("author") or {}
            author_name = author_data.get("name") or "unknown"
            login = author_data.get("username") or f"git:{author_name}"

            files = []
            for path in c.get("added", []) + c.get("modified", []):
                files.append({
                    "path": path,
                    "additions": 0,
                    "deletions": 0,
                    "url": f"https://github.com/{repo}/blob/{sha}/{path}",
                })
                if path.casefold().endswith((".md", ".mdx")):
                    doc_files_changed.add(path)

            for path in c.get("removed", []):
                if path.casefold().endswith((".md", ".mdx")):
                    # Delete removed document node and its sections
                    doc_key = f"{repo}:{path}"
                    graph.query("MATCH (d:Document {key: $key}) DETACH DELETE d", {"key": doc_key})

            commit_rows.append({
                "sha": sha,
                "message": message.splitlines()[0][:300] if message else "",
                "url": c.get("url") or f"https://github.com/{repo}/commit/{sha}",
                "timestamp": c.get("timestamp") or _now(),
                "login": login,
                "user_url": f"https://github.com/{login}" if not login.startswith("git:") else "",
                "author_name": author_name,
                "files": files,
                "is_merge": False,
            })
            task_links += [{"source": sha, "target": tid} for tid in knowledge.mentioned_tasks(message, tasks)]

        if commit_rows:
            graph.query(knowledge.COMMIT_UPSERT, {"repo": repo, "rows": commit_rows})
            knowledge._link(graph, "Commit", task_links)

        # Update any modified Markdown files in the graph
        updated_docs = 0
        for path in doc_files_changed:
            try:
                text = knowledge._raw_file(repo, branch, path)
            except (GitHubUnavailable, httpx.HTTPError, Exception) as exc:
                log.warning("Could not fetch updated doc %s: %s", path, exc)
                continue

            doc_key = f"{repo}:{path}"
            parts = knowledge.split_markdown(text)
            title = next((p["heading"] for p in parts if p["heading"]), path.rsplit("/", 1)[-1])
            section_ids = []
            sections = []
            d_task_links, d_person_links = [], []

            for position, part in enumerate(parts):
                sec_id = knowledge._stable_id(doc_key, str(position), part["heading"])
                section_ids.append(sec_id)
                anchor = f"#{knowledge._github_anchor(part['heading'])}" if part["heading"] else ""
                body = f"{part['heading']}\n{part['content']}"
                sections.append({
                    "id": sec_id,
                    "doc_key": doc_key,
                    "heading": part["heading"] or title,
                    "content": part["content"][:knowledge.MAX_SECTION_CHARS],
                    "position": position,
                    "path": path,
                    "url": f"https://github.com/{repo}/blob/{branch}/{path}{anchor}",
                })
                d_task_links += [{"source": sec_id, "target": t} for t in knowledge.mentioned_tasks(body, tasks)]
                d_person_links += [{"source": sec_id, "target": p} for p in knowledge.mentioned_people(body, developers)]

            graph.query(knowledge.DOC_UPSERT, {
                "repo": repo,
                "now": _now(),
                "docs": [{"key": doc_key, "path": path, "title": title, "section_ids": section_ids,
                          "url": f"https://github.com/{repo}/blob/{branch}/{path}"}],
            })
            if sections:
                graph.query(knowledge.SECTION_UPSERT, {"sections": sections})
            knowledge._link(graph, "DocSection", d_task_links)
            knowledge._link(graph, "DocSectionPerson", d_person_links)
            updated_docs += 1

        stats["commits_ingested"] = len(commit_rows)
        stats["docs_updated"] = updated_docs

    elif event_name in ("issues", "pull_request"):
        is_pr = (event_name == "pull_request")
        item = payload.get("issue") if not is_pr else payload.get("pull_request")
        if not item:
            return {"status": "ignored", "reason": f"missing {event_name} payload"}

        number = item.get("number")
        key = f"{repo}#{number}"
        title = (item.get("title") or "")[:300]
        body = item.get("body") or ""
        state = item.get("state") or "open"
        url = item.get("html_url") or ""
        updated_at = item.get("updated_at") or _now()
        author = (item.get("user") or {}).get("login") or "unknown"
        labels = [l.get("name") for l in item.get("labels", []) if isinstance(l, dict) and l.get("name")]
        assignees = [a.get("login") for a in item.get("assignees", []) if isinstance(a, dict) and a.get("login")]

        row = {
            "key": key,
            "number": number,
            "title": title,
            "state": state,
            "url": url,
            "is_pull_request": is_pr,
            "updated_at": updated_at,
            "labels": labels,
            "author": author,
            "assignees": assignees,
        }

        graph.query(knowledge.ISSUE_UPSERT, {"repo": repo, "rows": [row]})
        task_links = [{"source": key, "target": tid} for tid in knowledge.mentioned_tasks(f"{title}\n{body}", tasks)]
        knowledge._link(graph, "Issue", task_links)

        stats["number"] = number
        stats["action"] = payload.get("action")
        stats["state"] = state

    else:
        return {"status": "ignored", "reason": f"unhandled event type: {event_name}"}

    # Automatically refresh expertise topics after any knowledge change
    try:
        knowledge.refresh_expertise(graph)
        stats["expertise_refreshed"] = True
    except Exception as exc:
        log.warning("Expertise refresh after webhook failed: %s", exc)
        stats["expertise_refreshed"] = False

    return stats


class BackgroundKnowledgeSyncer:
    """Asynchronous background worker that periodically pulls changes from configured repositories."""

    def __init__(
        self,
        graph_provider: Callable[[], Any],
        default_repo: str,
        interval_seconds: int = DEFAULT_SYNC_INTERVAL,
    ):
        self.graph_provider = graph_provider
        self.default_repo = default_repo
        self.interval_seconds = interval_seconds
        self.running = False
        self.task: asyncio.Task | None = None
        self.last_sync_time: str | None = None
        self.last_sync_result: dict[str, Any] | None = None
        self.total_sync_count = 0
        self.total_error_count = 0
        self.webhook_events_count = 0

    def start(self) -> None:
        if self.running or self.interval_seconds <= 0:
            return
        self.running = True
        self.task = asyncio.create_task(self._run_loop())
        log.info("Background knowledge syncer started with %ds interval", self.interval_seconds)

    def stop(self) -> None:
        self.running = False
        if self.task and not self.task.done():
            self.task.cancel()
            self.task = None
        log.info("Background knowledge syncer stopped")

    async def sync_now(self) -> dict[str, Any]:
        """Execute a synchronization cycle immediately."""
        try:
            graph = self.graph_provider()
            if not graph:
                return {"status": "skipped", "reason": "no graph available"}

            # Sync default repo
            repo = self.default_repo
            result = await asyncio.to_thread(knowledge.sync_repository, graph, repo)
            await asyncio.to_thread(knowledge.refresh_expertise, graph)

            self.last_sync_time = _now()
            self.last_sync_result = result
            self.total_sync_count += 1
            return {"status": "success", "repo": repo, "result": result, "synced_at": self.last_sync_time}
        except Exception as exc:
            self.total_error_count += 1
            log.exception("Background knowledge sync failed: %s", exc)
            return {"status": "error", "error": str(exc)}

    async def _run_loop(self) -> None:
        # Initial brief wait after server boot
        await asyncio.sleep(min(10, self.interval_seconds))
        while self.running:
            try:
                await self.sync_now()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.exception("Unexpected error in background syncer loop: %s", exc)
            await asyncio.sleep(self.interval_seconds)

    def get_status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "interval_seconds": self.interval_seconds,
            "last_sync_time": self.last_sync_time,
            "total_sync_count": self.total_sync_count,
            "total_error_count": self.total_error_count,
            "webhook_events_count": self.webhook_events_count,
            "last_sync_result": self.last_sync_result,
        }
