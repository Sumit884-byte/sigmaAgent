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
from datetime import datetime, timezone, timedelta
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
    """Verify GitHub's HMAC-SHA256 signature from the X-Hub-Signature-256 header.
    
    Rejects requests when no secret is configured unless ALLOW_INSECURE_WEBHOOKS=true is explicitly enabled.
    """
    allow_insecure = os.getenv("ALLOW_INSECURE_WEBHOOKS", "").strip().lower() in ("true", "1", "yes")
    if not secret:
        if allow_insecure:
            return True
        log.warning("Rejecting GitHub webhook: GITHUB_WEBHOOK_SECRET is not configured.")
        return False
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

    branch = repo_data.get("default_branch") or "main"
    ref = payload.get("ref") or ""
    if event_name == "push" and ref and ref != f"refs/heads/{branch}":
        return {
            "status": "ignored",
            "reason": f"push to non-default branch {ref!r} (tracked branch is {branch!r})",
        }

    knowledge.ensure_indexes(graph)
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
                    graph.query(
                        "MATCH (d:Document {key: $key}) "
                        "OPTIONAL MATCH (d)-[:HAS_SECTION]->(s:DocSection) "
                        "DETACH DELETE d, s",
                        {"key": doc_key},
                    )

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


def handle_discord_stream_message(graph: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Ingest a live Discord message into the graph in real time."""
    message_id = str(payload.get("message_id") or "").strip()
    if not message_id:
        return {"status": "ignored", "reason": "missing message_id"}

    channel_id = str(payload.get("channel_id") or "unknown_channel").strip()
    channel_name = payload.get("channel_name") or "general"
    guild_id = str(payload.get("guild_id") or "")
    guild_name = payload.get("guild_name") or ""
    author_id = str(payload.get("author_id") or "unknown_author").strip()
    author_name = payload.get("author_name") or "unknown"
    content = payload.get("content") or ""
    timestamp = payload.get("timestamp") or _now()
    source_url = payload.get("source_url") or ""

    knowledge.ensure_indexes(graph)

    # 1. Upsert Channel, Message, Author, and relationships
    upsert_query = """
    MERGE (c:DiscordChannel {id: $channel_id})
    SET c.name = $channel_name, c.guild_id = $guild_id, c.guild_name = $guild_name
    MERGE (m:DiscordMessage {id: $message_id})
    SET m.channel_id = $channel_id,
        m.content = $content,
        m.timestamp = $timestamp,
        m.source_url = $source_url,
        m.is_deleted = false
    MERGE (m)-[:IN_CHANNEL]->(c)
    MERGE (a:DiscordAuthor {discord_id: $author_id})
    ON CREATE SET a.name = $author_name
    SET a.discord_name = $author_name
    MERGE (a)-[:AUTHORED]->(m)
    """
    graph.query(upsert_query, {
        "channel_id": channel_id,
        "channel_name": channel_name,
        "guild_id": guild_id,
        "guild_name": guild_name,
        "message_id": message_id,
        "content": content,
        "timestamp": timestamp,
        "source_url": source_url,
        "author_id": author_id,
        "author_name": author_name,
    })

    # 2. Map author to Developer if match found
    map_author_query = """
    MATCH (d:Developer)
    WHERE (d.name = $author_name OR d.discord_id = $author_id)
    MATCH (a:DiscordAuthor {discord_id: $author_id})
    MERGE (a)-[:MAPS_TO]->(d)
    """
    try:
        graph.query(map_author_query, {"author_name": author_name, "author_id": author_id})
    except Exception as exc:
        log.debug("Developer mapping query skipped: %s", exc)

    # 3. Detect task mentions and link them immediately
    tasks = [(row[0], row[1]) for row in graph.ro_query("MATCH (t:Task) RETURN t.id, t.title").result_set]
    mentioned = knowledge.mentioned_tasks(content, tasks)
    if mentioned:
        link_query = """
        UNWIND $task_ids AS tid
        MATCH (m:DiscordMessage {id: $message_id}), (t:Task {id: tid})
        MERGE (m)-[:MENTIONS]->(t)
        """
        graph.query(link_query, {"task_ids": mentioned, "message_id": message_id})

    return {
        "status": "streamed",
        "message_id": message_id,
        "channel_id": channel_id,
        "author": author_name,
        "mentioned_tasks": mentioned,
    }


def handle_discord_message_edit(
    graph: Any,
    message_id: str,
    new_content: str,
    edited_at: str | None = None,
) -> dict[str, Any]:
    """Update a Discord message's content and re-evaluate mentioned tasks."""
    edited_at = edited_at or _now()
    graph.query(
        "MATCH (m:DiscordMessage {id: $message_id}) "
        "SET m.content = $content, m.edited_at = $edited_at",
        {"message_id": message_id, "content": new_content, "edited_at": edited_at},
    )

    # Re-link mentioned tasks
    tasks = [(row[0], row[1]) for row in graph.ro_query("MATCH (t:Task) RETURN t.id, t.title").result_set]
    mentioned = knowledge.mentioned_tasks(new_content, tasks)

    # Clear old mentions for this message and link new
    graph.query(
        "MATCH (m:DiscordMessage {id: $message_id})-[r:MENTIONS]->(:Task) DELETE r",
        {"message_id": message_id},
    )
    if mentioned:
        link_query = """
        UNWIND $task_ids AS tid
        MATCH (m:DiscordMessage {id: $message_id}), (t:Task {id: tid})
        MERGE (m)-[:MENTIONS]->(t)
        """
        graph.query(link_query, {"task_ids": mentioned, "message_id": message_id})

    return {"status": "updated", "message_id": message_id, "mentioned_tasks": mentioned}


def handle_discord_message_delete(
    graph: Any,
    message_id: str,
    soft_delete: bool = True,
) -> dict[str, Any]:
    """Soft delete or detach delete a Discord message from the graph."""
    if soft_delete:
        graph.query(
            "MATCH (m:DiscordMessage {id: $message_id}) "
            "SET m.is_deleted = true, m.content = '[deleted]'",
            {"message_id": message_id},
        )
        return {"status": "soft_deleted", "message_id": message_id}
    else:
        graph.query(
            "MATCH (m:DiscordMessage {id: $message_id}) DETACH DELETE m",
            {"message_id": message_id},
        )
        return {"status": "deleted", "message_id": message_id}


def prune_graph_garbage(
    graph: Any,
    max_message_age_days: int | None = None,
    prune_orphan_docsections: bool = True,
    prune_orphan_files: bool = True,
    prune_orphan_topics: bool = True,
    prune_deleted_messages: bool = True,
) -> dict[str, int]:
    """Garbage collect stale, disconnected, or expired graph nodes and relationships."""
    stats = {
        "pruned_doc_sections": 0,
        "pruned_files": 0,
        "pruned_topics": 0,
        "pruned_deleted_messages": 0,
        "pruned_expired_messages": 0,
        "total_pruned": 0,
    }

    # 1. Orphan DocSections
    if prune_orphan_docsections:
        res = graph.query(
            "MATCH (sec:DocSection) WHERE NOT ()-[:HAS_SECTION]->(sec) "
            "WITH sec, count(sec) AS cnt DETACH DELETE sec RETURN cnt"
        )
        count = sum(row[0] for row in res.result_set) if res.result_set else 0
        stats["pruned_doc_sections"] = count

    # 2. Orphan Files (files with no touching commits)
    if prune_orphan_files:
        res = graph.query(
            "MATCH (f:File) WHERE NOT ()-[:TOUCHES]->(f) "
            "WITH f, count(f) AS cnt DETACH DELETE f RETURN cnt"
        )
        count = sum(row[0] for row in res.result_set) if res.result_set else 0
        stats["pruned_files"] = count

    # 3. Orphan Topics (topics with no expert links)
    if prune_orphan_topics:
        res = graph.query(
            "MATCH (top:Topic) WHERE NOT ()-[:EXPERT_IN]->(top) "
            "WITH top, count(top) AS cnt DETACH DELETE top RETURN cnt"
        )
        count = sum(row[0] for row in res.result_set) if res.result_set else 0
        stats["pruned_topics"] = count

    # 4. Soft-deleted messages
    if prune_deleted_messages:
        res = graph.query(
            "MATCH (m:DiscordMessage) WHERE m.is_deleted = true "
            "WITH m, count(m) AS cnt DETACH DELETE m RETURN cnt"
        )
        count = sum(row[0] for row in res.result_set) if res.result_set else 0
        stats["pruned_deleted_messages"] = count

    # 5. Expired messages by retention period
    if max_message_age_days and max_message_age_days > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_message_age_days)).isoformat()
        res = graph.query(
            "MATCH (m:DiscordMessage) WHERE m.timestamp IS NOT NULL AND m.timestamp < $cutoff "
            "WITH m, count(m) AS cnt DETACH DELETE m RETURN cnt",
            {"cutoff": cutoff},
        )
        count = sum(row[0] for row in res.result_set) if res.result_set else 0
        stats["pruned_expired_messages"] = count

    stats["total_pruned"] = sum(v for k, v in stats.items() if k != "total_pruned")
    return stats


class BackgroundKnowledgeSyncer:
    """Asynchronous background worker that periodically pulls changes from configured repositories and prunes garbage."""

    def __init__(
        self,
        graph_provider: Callable[[], Any],
        default_repo: str,
        interval_seconds: int = DEFAULT_SYNC_INTERVAL,
        gc_interval_cycles: int = 5,
    ):
        self.graph_provider = graph_provider
        self.default_repo = default_repo
        self.interval_seconds = interval_seconds
        self.gc_interval_cycles = gc_interval_cycles
        self.running = False
        self.task: asyncio.Task | None = None
        self.last_sync_time: str | None = None
        self.last_sync_result: dict[str, Any] | None = None
        self.total_sync_count = 0
        self.total_error_count = 0
        self.webhook_events_count = 0
        self.streamed_messages_count = 0
        self.total_gc_count = 0
        self.last_gc_time: str | None = None
        self.last_gc_result: dict[str, Any] | None = None

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

    async def sync_now(self, run_gc: bool = False) -> dict[str, Any]:
        """Execute a synchronization cycle immediately, optionally running garbage collection."""
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

            gc_summary = None
            if run_gc or (self.gc_interval_cycles > 0 and self.total_sync_count % self.gc_interval_cycles == 0):
                gc_summary = await asyncio.to_thread(prune_graph_garbage, graph)
                self.last_gc_time = self.last_sync_time
                self.last_gc_result = gc_summary
                self.total_gc_count += 1

            return {
                "status": "success",
                "repo": repo,
                "result": result,
                "gc": gc_summary,
                "synced_at": self.last_sync_time,
            }
        except Exception as exc:
            self.total_error_count += 1
            log.exception("Background knowledge sync failed: %s", exc)
            return {"status": "error", "error": str(exc)}

    async def run_gc(self, max_message_age_days: int | None = None) -> dict[str, Any]:
        """Execute garbage collection directly."""
        try:
            graph = self.graph_provider()
            if not graph:
                return {"status": "skipped", "reason": "no graph available"}
            result = await asyncio.to_thread(
                prune_graph_garbage,
                graph,
                max_message_age_days=max_message_age_days,
            )
            self.last_gc_time = _now()
            self.last_gc_result = result
            self.total_gc_count += 1
            return {"status": "success", "gc": result, "timestamp": self.last_gc_time}
        except Exception as exc:
            log.exception("Garbage collection failed: %s", exc)
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
            "streamed_messages_count": self.streamed_messages_count,
            "total_gc_count": self.total_gc_count,
            "last_gc_time": self.last_gc_time,
            "last_gc_result": self.last_gc_result,
            "last_sync_result": self.last_sync_result,
        }