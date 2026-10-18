import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from falkordb import FalkorDB
from google import genai
from google.genai import errors as genai_errors
from pydantic import BaseModel

log = logging.getLogger("api")

import continuous_sync
from continuous_sync import (
    BackgroundKnowledgeSyncer,
    handle_discord_message_delete,
    handle_discord_message_edit,
    handle_discord_stream_message,
    handle_github_webhook,
    prune_graph_garbage,
    verify_github_signature,
)
import knowledge
from github_analysis import (
    GitHubNotFound,
    GitHubUnavailable,
    answer_github_question,
    format_friend_competition,
    get_contributors,
    get_issue,
    get_issues,
)

load_dotenv()


def _gemini_client():
    # Re-read .env so a newly added key works without restarting the server
    load_dotenv(override=True)
    key = os.getenv("GEMINI_API_KEY")
    return genai.Client(api_key=key) if key else None


# 1. Database Credentials and lazy connection
_db: FalkorDB | None = None


def get_db() -> FalkorDB:
    global _db
    if _db is None:
        host = os.getenv("FALKORDB_HOST", "localhost")
        port = int(os.getenv("FALKORDB_PORT", 6379))
        username = os.getenv("FALKORDB_USERNAME")
        password = os.getenv("FALKORDB_PASSWORD")
        _db = FalkorDB(host=host, port=port, username=username, password=password)
    return _db


class _LazyDBProxy:
    """Lazy proxy so importing api.py does not initiate an immediate network connection."""

    def select_graph(self, name: str):
        return get_db().select_graph(name)

    def __getattr__(self, name: str):
        return getattr(get_db(), name)


db = _LazyDBProxy()

# Each Discord server gets its own FalkorDB graph, so its tasks, people and chat history never mix
# with another server's, and graph algorithms (PageRank, WCC) only ever see one team's work
GRAPH_PREFIX = os.getenv("FALKORDB_GRAPH_PREFIX", "Sigma_")
TENANT_INDEXES = (("Task", "id"), ("Developer", "name"), ("TaskChange", "id"), ("DiscordMessage", "id"))
_tenant_graphs = {}


def tenant_graph(guild_id):
    guild_id = str(guild_id or "")
    if not re.fullmatch(r"[0-9]{1,20}", guild_id):
        raise ValueError(f"invalid Discord server ID {guild_id!r}")
    name = f"{GRAPH_PREFIX}{guild_id}"
    if name not in _tenant_graphs:
        graph = db.select_graph(name)
        for label, prop in TENANT_INDEXES:
            try:
                graph.query(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
            except Exception as exc:
                if "already indexed" not in str(exc).casefold():
                    raise
        knowledge.ensure_indexes(graph)
        _tenant_graphs[name] = graph
    return _tenant_graphs[name]


GITHUB_REPO = os.getenv("GITHUB_REPO", "Sumit884-byte/sigmaAgent")


def _default_graph_provider():
    guild_id = os.getenv("DISCORD_GUILD_ID", "1")
    try:
        return tenant_graph(guild_id)
    except Exception:
        return None


background_syncer = BackgroundKnowledgeSyncer(
    graph_provider=_default_graph_provider,
    default_repo=GITHUB_REPO,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    background_syncer.start()
    yield
    background_syncer.stop()


app = FastAPI(title="Sigma Agent API", lifespan=lifespan)


# 3. Create the API Endpoint
@app.get("/dependencies")
def get_task_dependencies(guild_id: str | None = None):
    # Query one server's graph for all blocking relationships
    graph = tenant_graph(guild_id or os.getenv("DISCORD_GUILD_ID"))
    query = """
    MATCH (t:Task)-[:BLOCKED_BY]->(b:Task)
OPTIONAL MATCH (d:Developer)-[:ASSIGNED_TO]->(b)
RETURN t.title, b.title, d.name

    """
    result = graph.query(query)
    
    # Format the data for the AI agent
    dependencies = []
    for record in result.result_set:
        dependencies.append({
            "blocked_task": record[0],
            "waiting_on_task": record[1],
            "assigned_developer": record[2]
        })
        
    return {"status": "success", "data": dependencies}


# GitHub repository analysis (contributors, commits, issues, issue summaries)
GITHUB_REPO = os.getenv("GITHUB_REPO", "Sumit884-byte/sigmaAgent")


def _github_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, GitHubNotFound):
        return HTTPException(status_code=404, detail="GitHub repository or issue not found")
    return HTTPException(status_code=503, detail="GitHub service is currently unavailable")


@app.get("/github/contributors")
def github_contributors(repo: str = GITHUB_REPO):
    """Contributors of a repository with commit counts, busiest first."""
    try:
        return {"status": "success", "repo": repo, "data": get_contributors(repo)}
    except (GitHubNotFound, GitHubUnavailable) as exc:
        raise _github_http_error(exc) from exc


@app.get("/github/issues")
def github_issues(repo: str = GITHUB_REPO, state: str = "open", label: str | None = None,
                  labels: str | None = None, author: str | None = None, assignee: str | None = None,
                  unassigned: bool = False):
    """Issues (pull requests excluded); filter with state=open|closed|all, label(s), author, assignee, unassigned."""
    if labels:
        label_list = [part.strip() for part in labels.split(",") if part.strip()]
    else:
        label_list = [label] if label else None
    try:
        return {"status": "success", "repo": repo,
                "data": get_issues(repo, state=state, labels=label_list, author=author, assignee=assignee, unassigned=unassigned)}
    except (GitHubNotFound, GitHubUnavailable) as exc:
        raise _github_http_error(exc) from exc


@app.get("/github/compare")
def github_compare(users: str, repo: str = GITHUB_REPO):
    """Compare multiple contributors/friends for open source competition and collaboration tracking."""
    user_list = [u.strip() for u in users.split(",") if u.strip()]
    if len(user_list) < 2:
        raise HTTPException(status_code=400, detail="Provide at least two comma-separated users to compare.")
    try:
        contributors = get_contributors(repo)
        issues = get_issues(repo, state="all")
        result = format_friend_competition(repo, user_list, contributors, issues)
        return {"status": "success", "repo": repo, "users": user_list, "summary": result}
    except (GitHubNotFound, GitHubUnavailable) as exc:
        raise _github_http_error(exc) from exc


@app.get("/github/issue/{number}")
def github_issue(number: int, repo: str = GITHUB_REPO):
    """One issue with its body and latest comments."""
    try:
        return {"status": "success", "repo": repo, "data": get_issue(repo, number)}
    except (GitHubNotFound, GitHubUnavailable) as exc:
        raise _github_http_error(exc) from exc


# 4. Investigation endpoint used by the Discord bot
class KnowledgeSyncRequest(BaseModel):
    guild_id: str
    repo: str | None = None
    max_commits: int | None = None


@app.post("/knowledge/sync")
def knowledge_sync(req: KnowledgeSyncRequest, request: Request):
    """Pull a GitHub repo's commits, issues/PRs and Markdown docs into one server's graph."""
    _verify_internal_api_key(request)
    try:
        graph = tenant_graph(req.guild_id)
        return knowledge.sync_repository(graph, req.repo or GITHUB_REPO, max_commits=req.max_commits)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid request parameters") from None
    except (GitHubNotFound, GitHubUnavailable) as exc:
        raise _github_http_error(exc) from exc


def _verify_internal_api_key(request: Request) -> None:
    expected_key = os.getenv("INVESTIGATION_API_KEY", "").strip()
    if not expected_key:
        raise HTTPException(
            status_code=503,
            detail="Investigation API key is not configured on the server",
        )
    auth_header = request.headers.get("Authorization") or ""
    api_key_header = request.headers.get("X-API-Key") or ""
    token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else auth_header.strip()
    expected_bytes = expected_key.encode("utf-8")
    token_valid = hmac.compare_digest(token.encode("utf-8"), expected_bytes) if token else False
    header_valid = hmac.compare_digest(api_key_header.strip().encode("utf-8"), expected_bytes) if api_key_header else False
    if not (token_valid or header_valid):
        raise HTTPException(status_code=401, detail="Unauthorized: invalid or missing API key")


def _resolve_guild_for_repo(repo: str, request: Request | None = None) -> str:
    """Map a repository to its Discord guild on the backend rather than trusting unverified caller headers."""
    repo_map_raw = os.getenv("REPO_TO_GUILD_MAP", "{}").strip() or "{}"
    try:
        repo_map = json.loads(repo_map_raw)
        if isinstance(repo_map, dict) and repo in repo_map:
            return str(repo_map[repo])
    except Exception:
        pass

    default_guild = os.getenv("DISCORD_GUILD_ID")
    if default_guild:
        return str(default_guild)

    if os.getenv("ALLOW_INSECURE_WEBHOOKS", "").lower() in ("true", "1", "yes"):
        if request:
            custom = request.headers.get("X-Discord-Guild-ID") or request.query_params.get("guild_id")
            if custom:
                return str(custom)

    raise ValueError(f"Repository {repo!r} is not mapped to any known Discord guild")


@app.post("/webhooks/github")
async def github_webhook(request: Request):
    """Receive live GitHub webhooks (push, issues, pull_request) to keep the graph automatically current."""
    event = request.headers.get("X-GitHub-Event")
    if not event:
        raise HTTPException(status_code=400, detail="Missing X-GitHub-Event header")

    body_bytes = await request.body()
    secret = os.getenv("GITHUB_WEBHOOK_SECRET", "")
    signature = request.headers.get("X-Hub-Signature-256")
    if not verify_github_signature(body_bytes, signature, secret):
        raise HTTPException(status_code=401, detail="Invalid X-Hub-Signature-256 signature")

    try:
        payload = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Malformed JSON payload") from exc

    repo_data = payload.get("repository") or {}
    repo = repo_data.get("full_name") or ""
    try:
        guild_id = _resolve_guild_for_repo(repo, request)
    except ValueError as exc:
        log.warning("Unmapped repo in webhook: %s", exc)
        raise HTTPException(status_code=400, detail="Repository is not mapped to any known Discord guild") from exc

    try:
        graph = tenant_graph(guild_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid target graph or server configuration") from exc

    try:
        result = await asyncio.to_thread(handle_github_webhook, graph, event, payload)
        background_syncer.webhook_events_count += 1
        return result
    except GitHubNotFound as exc:
        raise HTTPException(status_code=404, detail="Repository not found") from exc
    except GitHubUnavailable as exc:
        raise HTTPException(status_code=503, detail="GitHub API unavailable") from exc


@app.get("/knowledge/sync/status")
def knowledge_sync_status(request: Request):
    """Inspect background knowledge syncer health, stats, and webhook event counts."""
    _verify_internal_api_key(request)
    return background_syncer.get_status()


@app.post("/knowledge/sync/run-now")
async def knowledge_sync_run_now(request: Request):
    """Trigger an immediate background synchronization cycle across configured repositories."""
    _verify_internal_api_key(request)
    return await background_syncer.sync_now()


class DiscordStreamMessageRequest(BaseModel):
    guild_id: str | None = None
    guild_name: str | None = None
    channel_id: str
    channel_name: str | None = None
    message_id: str
    author_id: str
    author_name: str
    content: str
    timestamp: str | None = None
    source_url: str | None = None


class DiscordMessageEditRequest(BaseModel):
    content: str
    edited_at: str | None = None
    guild_id: str | None = None


class GraphGCRequest(BaseModel):
    guild_id: str | None = None
    max_message_age_days: int | None = None
    prune_orphan_docsections: bool = True
    prune_orphan_files: bool = True
    prune_orphan_topics: bool = True
    prune_deleted_messages: bool = True


@app.post("/discord/stream-message")
def discord_stream_message(req: DiscordStreamMessageRequest, request: Request):
    """Ingest a live Discord message into the server's graph in real time."""
    _verify_internal_api_key(request)
    guild_id = req.guild_id or os.getenv("DISCORD_GUILD_ID", "")
    if not guild_id:
        raise HTTPException(status_code=400, detail="guild_id is required when DISCORD_GUILD_ID is not set")
    try:
        graph = tenant_graph(guild_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid target graph or server configuration") from exc

    payload = req.model_dump()
    result = handle_discord_stream_message(graph, payload)
    background_syncer.streamed_messages_count += 1
    return result


@app.put("/discord/stream-message/{message_id}")
def discord_stream_message_edit(message_id: str, req: DiscordMessageEditRequest, request: Request):
    """Update a live Discord message's content in the server's graph in real time."""
    _verify_internal_api_key(request)
    guild_id = req.guild_id or os.getenv("DISCORD_GUILD_ID", "")
    if not guild_id:
        raise HTTPException(status_code=400, detail="guild_id is required when DISCORD_GUILD_ID is not set")
    try:
        graph = tenant_graph(guild_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid target graph or server configuration") from exc

    return handle_discord_message_edit(graph, message_id, req.content, req.edited_at)


@app.delete("/discord/stream-message/{message_id}")
def discord_stream_message_delete(message_id: str, request: Request, guild_id: str | None = None, soft_delete: bool = True):
    """Delete or soft-delete a Discord message in the server's graph."""
    _verify_internal_api_key(request)
    gid = guild_id or os.getenv("DISCORD_GUILD_ID", "")
    if not gid:
        raise HTTPException(status_code=400, detail="guild_id is required when DISCORD_GUILD_ID is not set")
    try:
        graph = tenant_graph(gid)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid target graph or server configuration") from exc

    return handle_discord_message_delete(graph, message_id, soft_delete=soft_delete)


@app.post("/knowledge/gc")
def knowledge_gc(request: Request, req: GraphGCRequest | None = None):
    """Run graph garbage collection to prune disconnected sections, files, topics, and stale messages."""
    _verify_internal_api_key(request)
    req = req or GraphGCRequest()
    guild_id = req.guild_id or os.getenv("DISCORD_GUILD_ID", "")
    if not guild_id:
        raise HTTPException(status_code=400, detail="guild_id is required when DISCORD_GUILD_ID is not set")
    try:
        graph = tenant_graph(guild_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid target graph or server configuration") from exc

    stats = prune_graph_garbage(
        graph,
        max_message_age_days=req.max_message_age_days,
        prune_orphan_docsections=req.prune_orphan_docsections,
        prune_orphan_files=req.prune_orphan_files,
        prune_orphan_topics=req.prune_orphan_topics,
        prune_deleted_messages=req.prune_deleted_messages,
    )
    background_syncer.total_gc_count += 1
    background_syncer.last_gc_time = datetime.now(timezone.utc).isoformat()
    background_syncer.last_gc_result = stats
    return {"status": "success", "guild_id": guild_id, "stats": stats}


@app.get("/knowledge/gc/status")
def knowledge_gc_status(request: Request):
    """Get the latest garbage collection metrics."""
    _verify_internal_api_key(request)
    return {
        "total_gc_count": background_syncer.total_gc_count,
        "last_gc_time": background_syncer.last_gc_time,
        "last_gc_result": background_syncer.last_gc_result,
    }



class InvestigateRequest(BaseModel):
    question: str
    # Sent by the Discord bot: guild_id, channel_id, message_id, bot_name, ...
    discord: dict | None = None
    # Web callers set this explicitly. Requests without Discord provenance are also read-only.
    read_only: bool = False


class SourceItem(BaseModel):
    title: str
    url: str
    type: str  # discord | github | doc | external


class InvestigateResponse(BaseModel):
    answer: str
    sources: list[SourceItem] = []
    cypher_used: str | None = None


def _infer_source_type(url: str) -> str:
    lower = url.lower()
    if "discord.com" in lower:
        return "discord"
    if "github.com" in lower:
        return "github"
    if url.startswith("#") or "doc" in lower or ".md" in lower:
        return "doc"
    return "external"


def _extract_sources_and_cypher(answer_text: str) -> tuple[list[dict[str, str]], str | None]:
    """Parse out structured sources and Cypher query from an answer string."""
    sources: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    for title, url in re.findall(r"\[([^\]]+)\]\((https?://[^\s)]+|#[^\s)]+)\)", answer_text):
        if url not in seen_urls:
            seen_urls.add(url)
            sources.append({
                "title": title.strip(),
                "url": url.strip(),
                "type": _infer_source_type(url),
            })

    for url in re.findall(r"(?<!\()(https?://[^\s)<>\"']+)", answer_text):
        clean_url = url.rstrip(".,;!?")
        if clean_url not in seen_urls:
            seen_urls.add(clean_url)
            sources.append({
                "title": clean_url,
                "url": clean_url,
                "type": _infer_source_type(clean_url),
            })

    cypher_matches = re.findall(r"```cypher\s*\n(.*?)\n```", answer_text, flags=re.DOTALL)
    cypher_used = "\n\n".join(m.strip() for m in cypher_matches) if cypher_matches else None
    return sources, cypher_used


def _format_investigate_response(answer: str) -> dict[str, Any]:
    sources, cypher_used = _extract_sources_and_cypher(answer)
    return {
        "answer": answer,
        "sources": sources,
        "cypher_used": cypher_used,
    }


class TaskChange(BaseModel):
    # create | assign | unassign | block | unblock | set_status | rename | delete
    # | join_team | leave_team | link_github
    action: str
    task_id: str | None = None  # existing task ID; leave empty for create
    title: str | None = None  # for create / rename
    developer: str | None = None  # for create / assign
    blocked_by: str | None = None  # ID of the blocking task, for create / block / unblock
    status: str | None = None  # todo | in_progress | done, for set_status
    inferred: bool | None = None  # block only: dependency guessed from the kind of work, not stated
    team: str | None = None  # for join_team / leave_team
    github_login: str | None = None  # for link_github


class AgentReply(BaseModel):
    answer: str
    changes: list[TaskChange]


# Everything SigmaAgent can do, in one place: the `help` reply, the fallback answer and the
# Gemini prompt are all built from this, so the bot always describes itself accurately.
CAPABILITIES = [
    ("Tasks and dependencies", [
        ("what is blocked?", "blocked tasks and what they wait on"),
        ("what is Person 2 working on?", "someone's tasks and who waits on them"),
        ("Priya will do the frontend", "plain-language updates create, assign, block, finish, rename or delete tasks"),
        ("why is T2 assigned to Priya?", "who made a change, when, with a link to their message"),
    ]),
    ("Graph analysis (instant, shows the Cypher)", [
        ("explain T5", "dependency chain with owners and who assigned them"),
        ("impact Backend", "everything that slips if a task slips"),
        ("bottlenecks", "tasks and people others wait on most (PageRank)"),
    ]),
    ("Who knows what", [
        ("who knows about billing?", "people ranked by evidence: tasks, teams, commits, issues, docs, chat"),
        ("experts", "topics and their top people"),
        ("teams", "teams and members; say `Priya joined the payments team` to update"),
        ("Naseem77 is Person 1 on github", "link a GitHub account to a person"),
    ]),
    ("GitHub and docs", [
        ("sync github owner/repo", "pull commits, issues/PRs and Markdown docs into this server's graph"),
        ("who are the contributors?", "commit counts per person"),
        ("what bugs are open?", "open issues, by label, author or assignee"),
        ("summarize issue #6", "an issue's summary and latest discussion"),
    ]),
]


def _render_capabilities(markdown=True):
    lines = []
    for section, items in CAPABILITIES:
        lines.append(f"**{section}**" if markdown else f"{section}:")
        lines += [f"- `{example}`: {description}" if markdown else f"- '{example}': {description}"
                  for example, description in items]
    return "\n".join(lines)


HELP_TEXT = (
    "I'm **SigmaAgent**, this server's project brain. Mention me with a question or an update:\n\n"
    + _render_capabilities()
    + "\n\nAnything else, just ask: I answer from this server's tasks, chat history, docs and GitHub "
    "and link my sources. Each server has its own graph."
)
HELP_RE = re.compile(
    r"^\s*(?:help|commands|capabilities|what\s+(?:can|do)\s+you\s+do|what\s+are\s+you|who\s+are\s+you|"
    r"how\s+do\s+i\s+use\s+you|what\s+can\s+i\s+ask(?:\s+you)?|what\s+do\s+you\s+know)\s*[?.!]*\s*$",
    re.IGNORECASE,
)


def _all_tasks(guild_id):
    graph = tenant_graph(guild_id)
    result = graph.query("""
    MATCH (t:Task {guild_id: $guild_id})
    OPTIONAL MATCH (d:Developer)-[:ASSIGNED_TO]->(t)
    OPTIONAL MATCH (t)-[:BLOCKED_BY]->(b:Task)
    WHERE coalesce(b.status, 'todo') <> 'done'
    RETURN t.id, t.title, d.name, b.title, t.status
    ORDER BY t.id
    """, {"guild_id": guild_id})
    return result.result_set


# Graph analysis runs inside FalkorDB on this server's graph: multi-hop traversals for
# upstream/downstream work and the critical path, PageRank for bottlenecks, WCC for workstreams.
# BLOCKED_BY points from a task to the task it waits on, so PageRank flows to the work others need.
TASK_OVERVIEW_QUERY = """
MATCH (t:Task)
OPTIONAL MATCH (d:Developer)-[:ASSIGNED_TO]->(t)
WITH t, collect(DISTINCT d.name) AS owners
OPTIONAL MATCH (t)-[r:BLOCKED_BY]->(b:Task)
WITH t, owners, collect(DISTINCT [b.id, r.inferred]) AS deps
OPTIONAL MATCH chain = (t)-[:BLOCKED_BY*1..20]->(up:Task)
WITH t, owners, deps, collect(DISTINCT up.id) AS upstream, collect([n IN nodes(chain) | n.id]) AS chains
OPTIONAL MATCH (down:Task)-[:BLOCKED_BY*1..20]->(t)
WHERE coalesce(down.status, 'todo') <> 'done'
RETURN t.id, t.title, coalesce(t.status, 'todo'), owners, deps, upstream, collect(DISTINCT down.id), chains
ORDER BY t.id
""".strip()

CRITICAL_PATH_QUERY = """
MATCH p = (:Task)-[:BLOCKED_BY*1..20]->(:Task)
WHERE all(n IN nodes(p) WHERE coalesce(n.status, 'todo') <> 'done')
WITH [n IN nodes(p) | n.id] AS chain, length(p) AS hops
RETURN chain, hops
ORDER BY hops DESC
LIMIT 1
""".strip()

BOTTLENECK_QUERY = """
CALL algo.pageRank('Task', 'BLOCKED_BY') YIELD node, score
WITH node, score WHERE coalesce(node.status, 'todo') <> 'done'
OPTIONAL MATCH (d:Developer)-[:ASSIGNED_TO]->(node)
OPTIONAL MATCH (waiting:Task)-[:BLOCKED_BY*1..20]->(node)
WHERE coalesce(waiting.status, 'todo') <> 'done'
WITH node, score, collect(DISTINCT d.name) AS owners, count(DISTINCT waiting) AS waiting_count
RETURN node.id, node.title, owners, score, waiting_count
ORDER BY score DESC
""".strip()

IMPACT_QUERY = """
MATCH p = (down:Task)-[:BLOCKED_BY*1..20]->(:Task {id: $task_id})
WHERE coalesce(down.status, 'todo') <> 'done'
OPTIONAL MATCH (d:Developer)-[:ASSIGNED_TO]->(down)
WITH down, min(length(p)) AS hops, collect(DISTINCT d.name) AS owners
RETURN down.id, down.title, hops, owners
ORDER BY hops, down.id
""".strip()

WORKSTREAMS_QUERY = """
CALL algo.WCC({nodeLabels: ['Task'], relationshipTypes: ['BLOCKED_BY']}) YIELD node, componentId
RETURN componentId, collect(node.id)
""".strip()


def _bottlenecks(graph):
    """Open tasks ranked by PageRank, plus each owner's share of the work they hold up."""
    tasks = [
        {"id": row[0], "title": row[1], "owners": row[2], "score": row[3], "waiting": row[4]}
        for row in graph.ro_query(BOTTLENECK_QUERY).result_set
    ]
    people = {}
    for task in tasks:
        for owner in task["owners"]:
            person = people.setdefault(owner, {"score": 0.0, "waiting": 0, "tasks": []})
            person["score"] += task["score"]
            person["waiting"] += task["waiting"]
            person["tasks"].append(task["id"])
    ranked_people = sorted(people.items(), key=lambda item: (item[1]["waiting"], item[1]["score"]), reverse=True)
    return tasks, ranked_people


def _dependency_report(guild_id):
    """Describe each task's owners, dependencies, impact, bottlenecks and critical path, for Gemini."""
    graph = tenant_graph(guild_id)
    rows = graph.ro_query(TASK_OVERVIEW_QUERY).result_set
    if not rows:
        return "(no tasks for this server)"

    tasks = {
        row[0]: {"title": row[1], "status": row[2], "owners": row[3],
                 "deps": {dep_id: bool(inferred) for dep_id, inferred in row[4] if dep_id},
                 "upstream": row[5], "downstream": row[6],
                 # Longest blocker chain from this task to its root; ties go to the lowest IDs
                 "path": max(sorted(chain for chain in row[7] if chain), key=len, default=[row[0]])}
        for row in rows
    }

    def label(task_id):
        task = tasks.get(task_id, {"title": "?", "status": "?"})
        return f"{task_id} {task['title']} ({task['status']})"

    def owners(task_id):
        return ", ".join(tasks[task_id]["owners"]) or "unassigned" if task_id in tasks else "unknown"

    lines = []
    for task_id, task in tasks.items():
        deps = [f"{label(d)}{' [inferred]' if inferred else ''}" for d, inferred in task["deps"].items()]
        open_deps = [d for d in task["deps"] if tasks.get(d, {}).get("status") != "done"]
        if task["status"] == "done":
            readiness = "finished"
        elif open_deps:
            readiness = "waiting on " + ", ".join(open_deps)
        else:
            readiness = "ready to work on now"
        lines.append(
            f"- {task_id}: {task['title']} | owner: {owners(task_id)} | status: {task['status']}\n"
            f"    depends on: {'; '.join(deps) or 'nothing'}\n"
            f"    dependency path: {' ← '.join(f'{node} ({owners(node)})' for node in task['path'])}\n"
            f"    everything upstream: {', '.join(sorted(task['upstream'])) or 'nothing'}\n"
            f"    if this slips, it also delays: "
            f"{', '.join(f'{d} ({owners(d)})' for d in sorted(task['downstream'])) or 'nothing'}\n"
            f"    {readiness}"
        )

    critical = graph.ro_query(CRITICAL_PATH_QUERY).result_set
    if critical:
        lines.append("Critical path (longest chain of unfinished work): "
                     + " ← ".join(f"{label(t)} [{owners(t)}]" for t in critical[0][0]))

    ranked_tasks, ranked_people = _bottlenecks(graph)
    holding_up = [t for t in ranked_tasks if t["waiting"]]
    if holding_up:
        lines.append("Bottleneck tasks (PageRank over dependencies; open tasks waiting on each): " + "; ".join(
            f"{t['id']} {t['title']} [{', '.join(t['owners']) or 'unassigned'}] holds up {t['waiting']}"
            for t in holding_up[:5]))
        lines.append("Bottleneck people (open tasks waiting on their work): " + "; ".join(
            f"{name} holds up {p['waiting']} via {', '.join(p['tasks'])}"
            for name, p in ranked_people if p["waiting"]))

    streams = [sorted(ids) for _, ids in graph.ro_query(WORKSTREAMS_QUERY).result_set]
    if len(streams) > 1:
        lines.append("Independent workstreams (can run in parallel): "
                     + " | ".join(", ".join(stream) for stream in sorted(streams)))
    return "\n".join(lines)


def _cypher_block(query, params):
    return f"```cypher\n{query}\n\n// parameters: {json.dumps(params, ensure_ascii=False)}\n```"


def _impact_answer(question, guild_id):
    """`impact <task>`: everything that slips if this task slips, straight from a multi-hop query."""
    match = re.match(r"^\s*impact\b\s*(.*)$", question, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    ref = re.sub(r"^(of|on)\s+", "", match.group(1).strip(), flags=re.IGNORECASE).strip("?.,!`*_\"' ")
    if not ref:
        return "Usage: `@sigmaagent impact T3` (or a task title)."
    task_id = _resolve_task(guild_id, ref)
    if not task_id:
        return f"I couldn't find a task matching **{ref}** in this server's graph."
    graph = tenant_graph(guild_id)
    title = graph.ro_query("MATCH (t:Task {id: $id}) RETURN t.title", {"id": task_id}).result_set[0][0]
    params = {"task_id": task_id}
    rows = graph.ro_query(IMPACT_QUERY, params).result_set
    if not rows:
        answer = f"Nothing unfinished depends on **{task_id}: {title}**, so a delay there doesn't hold anyone else up."
    else:
        people = sorted({owner for row in rows for owner in row[3]})
        answer = (f"If **{task_id}: {title}** slips, {len(rows)} open task(s) slip with it"
                  + (f", affecting **{', '.join(people)}**" if people else "") + ":\n"
                  + "\n".join(f"- **{row[0]}: {row[1]}** ({', '.join(row[3]) or 'unassigned'}), "
                               f"{'directly' if row[2] == 1 else f'{row[2]} steps away'}" for row in rows))
    return f"{answer}\n\nGraph query used:\n{_cypher_block(IMPACT_QUERY, params)}"


def _bottleneck_answer(question, guild_id):
    """`bottlenecks`: PageRank over the dependency graph, rolled up per person."""
    if not re.match(r"^\s*bottlenecks?\b\s*[?.!]*\s*$", question, flags=re.IGNORECASE):
        return None
    ranked_tasks, ranked_people = _bottlenecks(tenant_graph(guild_id))
    holding_up = [t for t in ranked_tasks if t["waiting"]]
    if not holding_up:
        return "No unfinished task is holding up other work right now."
    lines = ["Tasks other work is waiting on (ranked by PageRank over dependencies):"]
    lines += [f"- **{t['id']}: {t['title']}** ({', '.join(t['owners']) or 'unassigned'}) holds up "
              f"{t['waiting']} open task(s)" for t in holding_up]
    people = [(name, p) for name, p in ranked_people if p["waiting"]]
    if people:
        lines.append("People others are waiting on: " + ", ".join(
            f"**{name}** ({p['waiting']} task(s) via {', '.join(p['tasks'])})" for name, p in people))
    return "\n".join(lines) + f"\n\nGraph query used:\n{_cypher_block(BOTTLENECK_QUERY, {})}"


EXPERTS_OVERVIEW_RE = re.compile(r"^\s*(?:experts|who\s+knows\s+what)\s*[?.!]*\s*$", re.IGNORECASE)
EXPERTISE_RE = re.compile(
    r"^\s*(?:who\s+(?:knows|understands)(?:\s+(?:the\s+)?most)?|"
    r"who(?:'s|\s+is)\s+(?:the\s+|an\s+|our\s+)?(?:expert|go-to(?:\s+person)?|best\s+person)|"
    r"who\s+should\s+i\s+(?:ask|talk\s+to)|who\s+can\s+help(?:\s+me)?|experts?)"
    r"\s+(?:about|on|in|with|for|regarding)?\s*(?P<topic>.+?)\s*[?.!]*\s*$",
    re.IGNORECASE,
)
TEAM_MEMBER_RE = re.compile(
    r"^\s*(?:is|are)\s+(?P<person>.+?)\s+(?:on|in|part\s+of)\s+(?:the\s+)?(?P<team>.+?)\s+team\s*[?.!]*\s*$",
    re.IGNORECASE,
)
TEAMS_RE = re.compile(
    r"^\s*(?:(?:list\s+)?teams|who(?:'s|\s+is|\s+are)\s+(?:on|in)\s+(?:the\s+)?(?P<team>.+?)\s+team)\s*[?.!]*\s*$",
    re.IGNORECASE,
)
SYNC_RE = re.compile(
    r"^\s*sync\s+(?:github|repo|knowledge|docs)(?:\s+(?:repo\s+)?(?P<repo>[A-Za-z0-9][\w-]*/[\w.-]+))?\s*[.!]*\s*$",
    re.IGNORECASE,
)
GC_RE = re.compile(
    r"^\s*(?:run\s+gc|garbage\s+collect(?:ion)?|prune\s+graph|clean\s+graph|clean\s+stale\s+knowledge)\s*[?!.]*\s*$",
    re.IGNORECASE,
)
PEOPLE_STATEMENT_RE = re.compile(
    r"\b(?:is|are|'s)\s+[^.?!\n]{1,40}?\s+on\s+github\b|\bgithub\s+(?:user(?:name)?|handle|account|login|id)\b|"
    r"\bgithub\s+(?:is|=|:)\s*@?[A-Za-z0-9-]+|\bon\s+github\s+as\s+@?[A-Za-z0-9-]+|"
    r"\blink\s+(?:github|@?[\w-]+\s+to)\b|\b(?:joined|left|is\s+on|are\s+on|moved\s+to)\s+(?:the\s+)?[\w -]+\s+team\b",
    re.IGNORECASE,
)
EVIDENCE_PATHS = (
    "(Developer)-[:ASSIGNED_TO]->(Task) · (Developer)-[:MEMBER_OF]->(Team) · "
    "(GitHubUser)-[:AUTHORED]->(Commit)-[:TOUCHES]->(File) · (GitHubUser)-[:OPENED|ASSIGNED]->(Issue) · "
    "(DocSection)-[:MENTIONS]->(Developer) · (DiscordAuthor)-[:AUTHORED]->(DiscordMessage)"
)


def _evidence_line(item):
    label = " ".join(str(item["label"]).split())[:90]
    text = f"{item['kind']}: {label}" if label else item["kind"]
    return f"[{text}]({item['url']})" if item["url"] else text


EXPERTISE_EVIDENCE_QUERY = """
MATCH (d:Developer)
OPTIONAL MATCH (d)-[:ASSIGNED_TO]->(t:Task)
OPTIONAL MATCH (d)-[:SAME_AS]->(gh:GitHubUser)-[:AUTHORED]->(c:Commit)-[:TOUCHES]->(f:File)
OPTIONAL MATCH (gh)-[:OPENED|ASSIGNED]->(i:Issue)
OPTIONAL MATCH (sec:DocSection)-[:MENTIONS]->(d)
RETURN d.name, count(DISTINCT c) AS commits, count(DISTINCT i) AS issues, count(DISTINCT sec) AS docs
""".strip()


def _expertise_answer(question, guild_id, discord, *, read_only=False):
    """`who knows about X`: people ranked by graph evidence, each with linked proof."""
    if EXPERTS_OVERVIEW_RE.match(question):
        graph = tenant_graph(guild_id)
        if not read_only:
            knowledge.refresh_expertise(graph)
        overview = knowledge.experts_overview(graph)
        if not overview:
            return ("I don't have enough evidence for who knows what yet. "
                    "Run `sync github owner/repo` to bring in commits, issues and docs.")
        lines = ["Who knows what (strongest evidence first):"]
        lines += [f"- **{topic}**: " + ", ".join(f"**{name}** ({score:g})" for name, score in people)
                  for topic, people in overview]
        return "\n".join(lines) + "\n\nAsk `who knows about <topic>` to see the evidence behind any of these."
    match = EXPERTISE_RE.match(question)
    if not match:
        return None
    topic = re.sub(r"(?i)^(?:the|a|an|our|my|this|that)\s+", "", match.group("topic").strip("\"'`*_ "))
    if not topic or not knowledge.search_terms(topic):
        return "Usage: `@sigmaagent who knows about billing?`"
    ranked = knowledge.expertise(tenant_graph(guild_id), topic, exclude=[discord.get("bot_name") or "sigmaagent"])
    if not ranked:
        return (f"I couldn't find anyone linked to **{topic}** in this server's tasks, teams, commits, issues, "
                "docs or chat. If the code lives on GitHub, run `sync github owner/repo` first.")
    lines = [f"Who knows about **{topic}** (ranked by evidence in the graph):"]
    for rank, person in enumerate(ranked, 1):
        evidence = "; ".join(_evidence_line(item) for item in person["evidence"][:3])
        more = len(person["evidence"]) - 3
        lines.append(f"{rank}. **{person['person']}** (score {person['score']:g}): {evidence}"
                     + (f"; +{more} more" if more > 0 else ""))
    params = {"topic": topic}
    return "\n".join(lines) + f"\n\nEvidence paths searched: {EVIDENCE_PATHS}\n\nGraph query used:\n{_cypher_block(EXPERTISE_EVIDENCE_QUERY, params)}"


def _teams_answer(question, guild_id):
    member = TEAM_MEMBER_RE.match(question)
    if member:
        key = " ".join(member.group("team").split()).casefold()
        rows = {name.casefold(): (name, people) for name, people in knowledge.teams(tenant_graph(guild_id))}
        if key not in rows:
            return f"There's no **{member.group('team')}** team yet."
        team_name, people = rows[key]
        asked = [name.strip() for name in re.split(r",|\band\b", member.group("person")) if name.strip()]
        lines = []
        for name in asked:
            found = next((p for p in people if p.casefold() == name.casefold()), None)
            lines.append(f"Yes, **{found}** is on the **{team_name}** team." if found
                         else f"No, **{name}** isn't on the **{team_name}** team.")
        return "\n".join(lines) + f"\nMembers: {', '.join(people) or 'none'}."
    match = TEAMS_RE.match(question)
    if not match:
        return None
    rows = knowledge.teams(tenant_graph(guild_id))
    wanted = match.group("team")
    if wanted:
        key = re.sub(r"(?i)\s+team$", "", " ".join(wanted.split())).casefold()
        rows = [row for row in rows if row[0].casefold() == key]
        if not rows:
            return f"There's no **{wanted}** team yet. Say e.g. `Priya is on the {wanted} team` to create it."
    if not rows:
        return "No teams yet. Tell me who's on which team, e.g. `@sigmaagent Priya is on the payments team`."
    return "\n".join(f"- **{name}**: {', '.join(members) or 'no members'}" for name, members in rows)


def _sync_answer(question, guild_id):
    match = SYNC_RE.match(question)
    if not match:
        return None
    graph = tenant_graph(guild_id)
    stored = graph.ro_query(
        "MATCH (r:Repository) RETURN r.full_name ORDER BY r.synced_at DESC LIMIT 1").result_set
    repo = match.group("repo") or (stored[0][0] if stored else GITHUB_REPO)
    try:
        counts = knowledge.sync_repository(graph, repo)
    except GitHubNotFound:
        return (f"I couldn't find **{repo}** on GitHub. If it's private, set `GITHUB_TOKEN` in the backend `.env`.")
    except GitHubUnavailable:
        return "I couldn't reach GitHub right now. Nothing was changed."
    answer = (f"Synced **{repo}** into this server's graph: {counts['commits']} commits, "
              f"{counts['issues']} issues/PRs, {counts['documents']} docs ({counts['sections']} sections), "
              f"expertise for {counts['topics']} topics. Try `who knows about <topic>` or `experts`.")
    if counts["warnings"]:
        answer += "\nNote: " + "; ".join(counts["warnings"][:3])
    return answer


def _gc_answer(question, guild_id):
    if not GC_RE.match(question):
        return None
    return (
        "Garbage collection modifies graph state and cannot be run via regular chat commands. "
        "To maintain read-only safety, garbage collection must be executed through the authenticated "
        "administration endpoint `/knowledge/gc` with explicit parameters."
    )


DEPENDENCY_PATH_QUERY = """
MATCH (task:Task {guild_id: $guild_id})-[:BLOCKED_BY]->(blocker:Task {guild_id: $guild_id})
RETURN task.id, blocker.id
""".strip()

TASK_ASSIGNMENT_SOURCES_QUERY = """
MATCH (event:TaskChange {guild_id: $guild_id, action: 'assign'})
WHERE event.task_id IN $task_ids
OPTIONAL MATCH (event)-[:MADE_BY]->(actor:DiscordAuthor)
OPTIONAL MATCH (event)-[:TRIGGERED_BY]->(message:DiscordMessage)
RETURN event.task_id, event.developer_name,
       coalesce(event.actor_name, actor.discord_name),
       coalesce(event.timestamp, message.timestamp),
       coalesce(event.source_url, message.source_url)
ORDER BY event.timestamp DESC
LIMIT 100
""".strip()


def _aggregate_task_rows(rows):
    """Collapse _all_tasks' owner/blocker cross product into one stable row per task."""
    grouped = {}
    for row in rows:
        task_id, title, owner, blocker, status = row
        entry = grouped.setdefault(str(task_id), {
            "id": str(task_id), "title": title, "owners": set(), "blockers": set(), "status": status,
        })
        if owner:
            entry["owners"].add(str(owner))
        if blocker:
            entry["blockers"].add(str(blocker))
        if not entry["status"] and status:
            entry["status"] = status
    return [
        [entry["id"], entry["title"], sorted(entry["owners"]), sorted(entry["blockers"]), entry["status"]]
        for entry in grouped.values()
    ]


def _explain_dependency_path(question, guild_id, tasks):
    """Explain the longest blocker chain for `explain <task>` and expose its read-only query."""
    graph = tenant_graph(guild_id)
    match = re.match(r"^\s*explain\b\s*(.*)$", question, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    raw_ref = match.group(1).strip()
    target_ref = raw_ref.strip("?.,!`*_ ").strip("\"'")
    if not target_ref:
        return "Usage: `@sigmaagent explain T5` (or provide a task title)."

    task_rows = _aggregate_task_rows(tasks)
    task_matches = [row for row in task_rows if str(row[0]).casefold() == target_ref.casefold()]
    is_task_id = bool(re.fullmatch(r"(?:[A-Za-z][A-Za-z0-9]*-\d+|T\d+)", target_ref, flags=re.IGNORECASE))
    is_quoted_title = raw_ref.startswith(("\"", "'")) and raw_ref[-1:] == raw_ref[:1]
    if not task_matches:
        task_matches = [row for row in task_rows if str(row[1]).casefold() == target_ref.casefold()]
    if not task_matches and not is_task_id and not is_quoted_title:
        # Ordinary prose such as "explain why T5 is late" belongs to the normal Q&A flow.
        return None
    if not task_matches:
        return f"I couldn't identify one task matching **{target_ref}** in this server's graph."
    if len(task_matches) > 1:
        options = ", ".join(f"{row[0]}: {row[1]}" for row in task_matches)
        return f"**{target_ref}** matches multiple tasks: {options}. Use a task ID to disambiguate."

    task = task_matches[0]
    task_id = str(task[0])
    params = {"guild_id": str(guild_id)}
    result = graph.query(DEPENDENCY_PATH_QUERY, params).result_set
    dependencies = {}
    for blocked_id, blocker_id in result:
        dependencies.setdefault(str(blocked_id), []).append(str(blocker_id))
    task_by_id = {str(row[0]): row for row in task_rows}
    chain_cache = {}

    def longest_path(current_id, visiting=frozenset()):
        if current_id in visiting:
            return [current_id]
        if current_id in chain_cache:
            return chain_cache[current_id]
        candidates = [longest_path(blocker_id, visiting | {current_id})
                      for blocker_id in dependencies.get(current_id, ()) if blocker_id in task_by_id]
        path = [current_id] + max(candidates, key=len, default=[])
        chain_cache[current_id] = path
        return path

    ids = longest_path(task_id)
    chain = [task_by_id[item] for item in ids if item in task_by_id]
    chain_text = " ← ".join(
        f"{row[0]}: {row[1]} ({', '.join(row[2]) or 'unassigned'}; {row[4] or 'todo'})"
        for row in chain
    ) or f"{task_id}: {task[1]}"
    source_params = {"guild_id": str(guild_id), "task_ids": [row[0] for row in chain]}
    source_rows = graph.query(TASK_ASSIGNMENT_SOURCES_QUERY, source_params).result_set
    assignment_sources = {}
    for source_row in source_rows:
        assignment_sources.setdefault(str(source_row[0]), []).append(source_row)
    source_lines = []
    for row in chain:
        for owner in row[2]:
            event = next((item for item in assignment_sources.get(str(row[0]), [])
                          if item[1] and str(item[1]).casefold() == str(owner).casefold()
                          and item[2] and item[4]), None)
            if event:
                source_lines.append(
                    f"- {row[0]} owner **{owner}** was assigned by **{event[2]}** "
                    f"([source message]({event[4]}))."
                )
            else:
                source_lines.append(f"- {row[0]} owner **{owner}** has no recorded assignment source.")
    query_display = (
        f"Dependency path query:\n```cypher\n{DEPENDENCY_PATH_QUERY}\n\n"
        f"// parameters: {json.dumps(params, ensure_ascii=False)}\n```\n"
        f"Assignment source query:\n```cypher\n{TASK_ASSIGNMENT_SOURCES_QUERY}\n\n"
        f"// parameters: {json.dumps(source_params, ensure_ascii=False)}\n```"
    )
    if len(chain) <= 1:
        answer = f"**{task_id}: {task[1]}** has no recorded blocker chain."
    else:
        answer = f"Dependency path: {chain_text}"
    if source_lines:
        answer += "\n\nSource trail:\n" + "\n".join(source_lines)
    return f"{answer}\n\nGraph query used:\n{query_display}"


TASK_CHANGE_PROVENANCE_QUERY = """
MATCH (event:TaskChange {guild_id: $guild_id})
WHERE ($task_id IS NULL OR event.task_id = $task_id)
  AND ($action IS NULL OR event.action = $action)
  AND ($developer_name IS NULL OR event.developer_name = $developer_name)
OPTIONAL MATCH (event)-[:AFFECTS]->(task:Task {guild_id: $guild_id})
WHERE $task_id IS NULL OR task.id = $task_id
OPTIONAL MATCH (event)-[:MADE_BY]->(actor:DiscordAuthor)
OPTIONAL MATCH (event)-[:TRIGGERED_BY]->(message:DiscordMessage)
RETURN event.action, event.task_id, event.task_title, event.summary,
       coalesce(event.actor_name, actor.discord_name), event.actor_id,
       coalesce(event.timestamp, message.timestamp),
       coalesce(event.source_url, message.source_url),
       event.developer_name, event.details
ORDER BY event.timestamp DESC
LIMIT 50
""".strip()


def _task_change_history(guild_id, task_id=None, *, action=None, developer_name=None):
    """Return source-linked task changes, newest first, for this Discord server."""
    graph = tenant_graph(guild_id)
    result = graph.query(
        TASK_CHANGE_PROVENANCE_QUERY,
        {"guild_id": guild_id, "task_id": task_id, "action": action,
         "developer_name": developer_name},
    )
    return [
        {
            "action": row[0], "task_id": row[1], "task_title": row[2],
            "summary": row[3], "actor_name": row[4], "actor_id": row[5],
            "timestamp": row[6], "source_url": row[7],
            "developer_name": row[8], "details": row[9],
        }
        for row in result.result_set
    ]


def _ownership_provenance_answer(question, guild_id, tasks):
    """Answer ownership 'why' questions only when an auditable source exists."""
    lowered = question.casefold()
    if "why" not in lowered or not re.search(r"\b(owner|owned|assign|assigned)\b", lowered):
        return None
    task_match = re.search(r"\b(?:[A-Za-z][A-Za-z0-9]*-\d+|T\d+)\b", question, flags=re.IGNORECASE)
    grouped_tasks = _aggregate_task_rows(tasks)
    if task_match:
        matches = [row for row in grouped_tasks if row[0].casefold() == task_match.group(0).casefold()]
    else:
        matches = [row for row in grouped_tasks
                   if re.search(rf"(?<!\w){re.escape(str(row[1]))}(?!\w)", question, flags=re.IGNORECASE)]
    if len(matches) != 1:
        return None

    task_id, title, owners = matches[0][0], matches[0][1], matches[0][2]
    if not owners:
        return None
    lines = [f"Current owner(s) of **{task_id}: {title}**: **{', '.join(owners)}**."]
    params = {"guild_id": str(guild_id), "task_id": task_id, "action": "assign"}
    for owner in owners:
        history = _task_change_history(guild_id, task_id, action="assign", developer_name=owner)
        source = next((item for item in history if item["source_url"] and item["actor_name"]), None)
        if not source:
            lines.append(f"No recorded Discord source for **{owner}**'s assignment.")
            continue
        try:
            timestamp = datetime.fromisoformat(str(source["timestamp"]).replace("Z", "+00:00"))
            date = timestamp.strftime("%b %d").replace(" 0", " ")
        except (TypeError, ValueError):
            date = "an unknown date"
        lines.append(f"**{owner}** was assigned by **{source['actor_name']}** on {date} "
                     f"([source message]({source['source_url']})).")
    answer = "\n".join(lines)
    return f"{answer}\n\nGraph query used:\n{_cypher_block(TASK_CHANGE_PROVENANCE_QUERY, params)}"


def _relevant_task_change_context(question, guild_id, tasks):
    """Fetch a small audit trail only for explicit, current tasks in a 'why' question."""
    if not re.search(r"\bwhy\b", question, flags=re.IGNORECASE):
        return "(no task-change history requested)"
    grouped_tasks = _aggregate_task_rows(tasks)
    ids = {match.casefold() for match in re.findall(r"\b(?:[A-Za-z][A-Za-z0-9]*-\d+|T\d+)\b", question, flags=re.IGNORECASE)}
    matched = [row for row in grouped_tasks if row[0].casefold() in ids]
    if not matched:
        matched = [row for row in grouped_tasks
                   if re.search(rf"(?<!\w){re.escape(str(row[1]))}(?!\w)", question, flags=re.IGNORECASE)]
    if not matched or len(matched) > 3:
        return "(no unambiguous current task reference; no task-change history fetched)"

    lines = []
    for task in matched:
        for change in _task_change_history(guild_id, task[0]):
            line = f"- {change['summary']} | {change['actor_name'] or 'unknown author'} | {change['timestamp'] or 'unknown date'}"
            if change["source_url"]:
                line += f" | [source message]({change['source_url']})"
            lines.append(line)
            if len(lines) >= 12:
                return "\n".join(lines)
    return "\n".join(lines) or "(no recorded changes for the referenced task)"


def _channel_history(question, discord):
    """Ask the context retrieval service for past messages in this channel related to the question."""
    url = os.getenv("CONTEXT_API_URL")
    key = os.getenv("CONTEXT_API_KEY")
    channel_id = discord.get("channel_id")
    if not url or not key or not channel_id:
        return []

    headers = {"X-Context-API-Key": key}
    try:
        with httpx.Client(base_url=url, headers=headers, timeout=10) as http:
            # Pull new messages into the graph first so the latest conversation is searchable
            http.post("/context/sync-channel",
                      params={"channel_id": channel_id, "max_messages": 200, "mode": "incremental"}
                      ).raise_for_status()
            response = http.post("/context/retrieve", json={
                "feature_title": question[:300], "channel_id": channel_id,
                "guild_id": discord.get("guild_id"), "limit": 15,
            })
            response.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"Context retrieval failed, answering from tasks only: {exc}")
        return []

    # Keyword search misses recent statements that share no words with the question
    # ("person 1 will do auth" vs "how is the work divided"), so always add the latest messages too
    recent = tenant_graph(discord.get("guild_id")).query("""
    MATCH (m:DiscordMessage)-[:IN_CHANNEL]->(:DiscordChannel {id: $channel_id})
    OPTIONAL MATCH (a:DiscordAuthor)-[:AUTHORED]->(m)
    RETURN m.id, m.content, m.timestamp, m.source_url, a.discord_name
    ORDER BY m.timestamp DESC
    LIMIT 25
    """, {"channel_id": channel_id}).result_set
    found = {m["message_id"]: m for m in response.json()["evidence"]}
    for message_id, content, timestamp, source_url, author_name in recent:
        found.setdefault(message_id, {"message_id": message_id, "content": content, "timestamp": timestamp,
                                      "source_url": source_url, "author": author_name})

    skip_ids = {discord.get("message_id")}
    bot_name = discord.get("bot_name")
    messages = [m for m in found.values()
                if m["message_id"] not in skip_ids and m["author"] != bot_name and (m["content"] or "").strip()]
    return sorted(messages, key=lambda m: m["timestamp"] or "")


def _format_task(task_id, title, developer, blocked_by, status=None):
    line = f"**{task_id}: {title}** - {developer or 'unassigned'}"
    if status and status != "todo":
        line += f" [{status}]"
    if blocked_by:
        line += f" (blocked by *{blocked_by}*)"
    return line


def _next_task_id(guild_id):
    graph = tenant_graph(guild_id)
    ids = graph.query("MATCH (t:Task {guild_id: $g}) RETURN t.id", {"g": guild_id}).result_set
    numbers = [int(row[0][1:]) for row in ids if re.fullmatch(r"T\d+", row[0] or "")]
    return f"T{max(numbers, default=0) + 1}"


def _developer_name(guild_id, name):
    """Reuse an existing developer's spelling ("person 1" -> "Person 1") so case doesn't split people."""
    graph = tenant_graph(guild_id)
    name = " ".join(name.split())
    existing = graph.query("MATCH (d:Developer) WHERE toLower(d.name) = toLower($name) RETURN d.name LIMIT 1",
                           {"name": name}).result_set
    if existing:
        return existing[0][0]
    return name[:1].upper() + name[1:] if name.islower() else name


def _resolve_task(guild_id, ref):
    """Turn 'T2', 't2' or a task title into this server's task ID; None if there's no such task."""
    graph = tenant_graph(guild_id)
    # Model output can carry quotes, zero-width characters or words like "the ... task"
    ref = re.sub(r"[\u200b-\u200f\ufeff\"'`*]", "", ref or "")
    ref = re.sub(r"(?i)^\s*(?:the\s+)?(.*?)(?:\s+task)?\s*$", r"\1", " ".join(ref.split()))
    if not ref:
        return None
    if _task_exists(guild_id, ref.upper()):
        return ref.upper()
    match = graph.query("MATCH (t:Task {guild_id: $g}) WHERE toLower(t.title) = toLower($title) RETURN t.id LIMIT 1",
                        {"g": guild_id, "title": ref}).result_set
    if match:
        return match[0][0]
    # Fall back to a unique partial title match ("checkout" -> "Checkout Flow"), never a guess between several
    partial = graph.query("""
    MATCH (t:Task {guild_id: $g})
    WHERE toLower(t.title) CONTAINS toLower($title) OR toLower($title) CONTAINS toLower(t.title)
    RETURN t.id LIMIT 2
    """, {"g": guild_id, "title": ref}).result_set
    return partial[0][0] if len(partial) == 1 else None


def _task_exists(guild_id, task_id):
    graph = tenant_graph(guild_id)
    return bool(graph.query("MATCH (t:Task {id: $id, guild_id: $g}) RETURN t.id",
                            {"id": task_id, "g": guild_id}).result_set)


TASK_CHANGE_AUDIT_CLAUSE = """
MERGE (actor:DiscordAuthor {discord_id: $actor_id})
SET actor.discord_name = $actor_name
MERGE (source:DiscordMessage {id: $source_message_id})
SET source.guild_id = $g, source.channel_id = $source_channel_id,
    source.content = $source_content, source.timestamp = $source_timestamp,
    source.source_url = $source_url
MERGE (actor)-[:AUTHORED]->(source)
MERGE (event:TaskChange {id: $event_id})
ON CREATE SET event.guild_id = $g, event.action = $event_action,
    event.details = $event_details, event.summary = $event_summary,
    event.task_id = t.id, event.task_title = t.title,
    event.actor_id = $actor_id, event.actor_name = $actor_name,
    event.source_message_id = $source_message_id,
    event.source_channel_id = $source_channel_id, event.source_url = $source_url,
    event.source_content = $source_content,
    event.timestamp = $source_timestamp, event.recorded_at = $recorded_at,
    event.developer_name = $event_developer_name,
    event.blocked_by_id = $event_blocked_by_id
MERGE (event)-[:MADE_BY]->(actor)
MERGE (event)-[:TRIGGERED_BY]->(source)
MERGE (event)-[:AFFECTS]->(t)
"""

TASK_CREATE_AUDIT_QUERY = """
MERGE (event:TaskChange {id: $event_id})
ON CREATE SET event.applied = false, event.guild_id = $g, event.action = $event_action,
    event.details = $event_details, event.summary = $event_summary,
    event.task_id = $id, event.task_title = $title,
    event.actor_id = $actor_id, event.actor_name = $actor_name,
    event.source_message_id = $source_message_id,
    event.source_channel_id = $source_channel_id, event.source_url = $source_url,
    event.source_content = $source_content, event.timestamp = $source_timestamp,
    event.recorded_at = $recorded_at
WITH event, coalesce(event.applied, false) AS already_applied
FOREACH (_ IN CASE WHEN already_applied THEN [] ELSE [1] END |
    CREATE (:Task {id: $id, guild_id: $g, title: $title, status: 'todo', updated_by: $by})
)
WITH event
MATCH (t:Task {id: $id, guild_id: $g})
MERGE (actor:DiscordAuthor {discord_id: $actor_id})
SET actor.discord_name = $actor_name
MERGE (source:DiscordMessage {id: $source_message_id})
SET source.guild_id = $g, source.channel_id = $source_channel_id,
    source.content = $source_content, source.timestamp = $source_timestamp,
    source.source_url = $source_url
MERGE (actor)-[:AUTHORED]->(source)
MERGE (event)-[:MADE_BY]->(actor)
MERGE (event)-[:TRIGGERED_BY]->(source)
MERGE (event)-[:AFFECTS]->(t)
SET event.applied = true
""".strip()


def _discord_provenance(discord, guild_id, question):
    """Validate the Discord identity/source fields required for auditable mutations."""
    if not isinstance(discord, dict):
        return None
    source_guild = str(discord.get("guild_id") or guild_id or "")
    channel_id = str(discord.get("channel_id") or "")
    message_id = str(discord.get("message_id") or "")
    actor_id = str(discord.get("author_id") or "")
    snowflake = r"[0-9]{17,20}"
    if not all(re.fullmatch(snowflake, value) for value in (source_guild, channel_id, message_id, actor_id)):
        return None
    try:
        timestamp = datetime.fromisoformat(
            str(discord.get("timestamp") or discord.get("created_at") or "").replace("Z", "+00:00")
        )
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        timestamp = timestamp.astimezone(timezone.utc).isoformat()
    except ValueError:
        timestamp = datetime.now(timezone.utc).isoformat()
    return {
        "guild_id": source_guild,
        "channel_id": channel_id,
        "message_id": message_id,
        "actor_id": actor_id,
        "actor_name": str(discord.get("author_name") or f"Discord user {actor_id}"),
        "timestamp": timestamp,
        "source_url": f"https://discord.com/channels/{source_guild}/{channel_id}/{message_id}",
        "content": str(discord.get("message_content") or question or ""),
    }


def _audit_event_params(guild_id, change, provenance, *, index, path, summary, developer_name=None,
                        blocked_by_id=None):
    if provenance is None:
        raise ValueError("task changes require a Discord message, author, and source link")
    details = change.model_dump(exclude_none=True)
    event_seed = json.dumps(
        [guild_id, provenance["message_id"], index, path, details],
        sort_keys=True, separators=(",", ":"),
    )
    return {
        "event_id": hashlib.sha256(event_seed.encode("utf-8")).hexdigest(),
        "event_action": change.action,
        "event_details": json.dumps(details, sort_keys=True, ensure_ascii=False),
        "event_summary": summary,
        "event_developer_name": developer_name,
        "event_blocked_by_id": blocked_by_id,
        "actor_id": provenance["actor_id"],
        "actor_name": provenance["actor_name"],
        "source_message_id": provenance["message_id"],
        "source_channel_id": provenance["channel_id"],
        "source_content": provenance["content"],
        "source_timestamp": provenance["timestamp"],
        "source_url": provenance["source_url"],
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }


# Team and GitHub-identity changes are audited like task changes, without an affected task
PEOPLE_CHANGE_AUDIT_CLAUSE = (
    TASK_CHANGE_AUDIT_CLAUSE
    .replace("    event.task_id = t.id, event.task_title = t.title,\n", "")
    .replace("MERGE (event)-[:AFFECTS]->(t)\n", "")
)
assert "t.id" not in PEOPLE_CHANGE_AUDIT_CLAUSE and "(t)" not in PEOPLE_CHANGE_AUDIT_CLAUSE


def _apply_people_change(graph, guild_id, change, author, provenance, *, index, path):
    """join_team / leave_team / link_github, each recorded with its Discord source."""
    if not change.developer:
        raise ValueError("no person given")
    params = {"g": guild_id, "by": author, "dev": _developer_name(guild_id, change.developer)}
    if change.action in ("join_team", "leave_team"):
        if not change.team or not change.team.strip():
            raise ValueError("no team given")
        team_name = " ".join(change.team.split())
        team_name = re.sub(r"(?i)\s+team$", "", team_name) or team_name
        existing = graph.ro_query("MATCH (team:Team {key: $key}) RETURN team.name", {"key": team_name.casefold()}).result_set
        team_name = existing[0][0] if existing else team_name
        params.update(team=team_name, team_key=team_name.casefold())
        if change.action == "join_team":
            summary = f"**{params['dev']}** joined the **{team_name}** team"
            mutation = """
            MERGE (d:Developer {name: $dev})
            MERGE (team:Team {key: $team_key}) ON CREATE SET team.name = $team
            MERGE (d)-[:MEMBER_OF]->(team)
            WITH d, team
            """
        else:
            summary = f"**{params['dev']}** left the **{team_name}** team"
            mutation = """
            MATCH (d:Developer {name: $dev}), (team:Team {key: $team_key})
            OPTIONAL MATCH (d)-[m:MEMBER_OF]->(team)
            DELETE m
            WITH DISTINCT d, team
            """
        tail = "MERGE (event)-[:AFFECTS_DEVELOPER]->(d) MERGE (event)-[:AFFECTS_TEAM]->(team)"
    else:  # link_github
        login = (change.github_login or "").strip().lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", login):
            raise ValueError("no valid GitHub username given")
        params["login"] = login
        summary = f"Linked GitHub **@{login}** to **{params['dev']}**"
        mutation = """
        MERGE (d:Developer {name: $dev})
        MERGE (u:GitHubUser {login: $login})
        MERGE (u)-[:SAME_AS]->(d)
        WITH d, u
        """
        tail = "MERGE (event)-[:AFFECTS_DEVELOPER]->(d)"
    params.update(_audit_event_params(
        guild_id, change, provenance, index=index, path=path, summary=summary, developer_name=params["dev"],
    ))
    if change.action == "leave_team" and not graph.ro_query(
            "MATCH (:Developer {name: $dev})-[:MEMBER_OF]->(:Team {key: $team_key}) RETURN 1",
            {"dev": params["dev"], "team_key": params["team_key"]}).result_set:
        raise ValueError(f"{params['dev']} isn't on the {params['team']} team")
    graph.query(mutation + PEOPLE_CHANGE_AUDIT_CLAUSE + tail, params)
    return summary


def _apply_change(guild_id, change, author, provenance, *, index=0, path=""):
    """Apply one change to this server's tasks and describe it, or raise ValueError."""
    graph = tenant_graph(guild_id)
    action = change.action
    task_id = _resolve_task(guild_id, change.task_id) or (change.task_id or "").strip().upper() or None
    blocked_by = _resolve_task(guild_id, change.blocked_by) or (change.blocked_by or "").strip().upper() or None
    params = {"g": guild_id, "id": task_id, "by": author}

    if provenance is not None:
        identity = _audit_event_params(
            guild_id, change, provenance, index=index, path=path, summary="",
        )
        previous = graph.query(
            "MATCH (event:TaskChange {id: $event_id}) RETURN event.summary LIMIT 1",
            {"event_id": identity["event_id"]},
        ).result_set
        if previous:
            return previous[0][0] or "This Discord change was already applied."

    if action == "create":
        if not change.title:
            raise ValueError("a new task needs a title")
        params.update(id=_next_task_id(guild_id), title=change.title.strip())
        result = f"Created **{params['id']}: {params['title']}**"
        task_id = params["id"]
        params.update(_audit_event_params(
            guild_id, change, provenance, index=index, path=path, summary=result,
        ))
        graph.query(TASK_CREATE_AUDIT_QUERY, params)
        if change.developer:
            result += "; " + _apply_change(
                guild_id, TaskChange(action="assign", task_id=task_id, developer=change.developer),
                author, provenance, index=index, path=f"{path}/assign",
            )
        if blocked_by:
            result += "; " + _apply_change(
                guild_id, TaskChange(action="block", task_id=task_id, blocked_by=blocked_by),
                author, provenance, index=index, path=f"{path}/block",
            )
        return result

    if action in ("join_team", "leave_team", "link_github"):
        return _apply_people_change(graph, guild_id, change, author, provenance, index=index, path=path)

    if not task_id or not _task_exists(guild_id, task_id):
        raise ValueError(f"task {task_id or '(none given)'} doesn't exist in this server")

    if action == "assign":
        if not change.developer:
            raise ValueError("no developer given")
        params["dev"] = _developer_name(guild_id, change.developer)
        summary = f"Assigned **{task_id}** to **{params['dev']}**"
        params.update(_audit_event_params(
            guild_id, change, provenance, index=index, path=path,
            summary=summary, developer_name=params["dev"],
        ))
        graph.query("""
        MATCH (t:Task {id: $id, guild_id: $g})
        OPTIONAL MATCH (:Developer)-[old:ASSIGNED_TO]->(t)
        DELETE old
        WITH DISTINCT t
        MERGE (d:Developer {name: $dev})
        MERGE (d)-[:ASSIGNED_TO]->(t)
        SET t.updated_by = $by
        WITH t, d
        """ + TASK_CHANGE_AUDIT_CLAUSE + "MERGE (event)-[:AFFECTS_DEVELOPER]->(d)", params)
        return summary

    if action == "unassign":
        summary = f"Unassigned **{task_id}**"
        params.update(_audit_event_params(
            guild_id, change, provenance, index=index, path=path, summary=summary,
        ))
        graph.query("""
        MATCH (t:Task {id: $id, guild_id: $g})
        OPTIONAL MATCH (old:Developer)-[r:ASSIGNED_TO]->(t)
        WITH t, collect(old.name) AS previous_owners, collect(old) AS previous_owner_nodes,
             collect(r) AS old_links
        FOREACH (old_link IN old_links | DELETE old_link)
        WITH t, previous_owners, previous_owner_nodes
        """ + TASK_CHANGE_AUDIT_CLAUSE + """
        SET event.previous_owners = coalesce(event.previous_owners, previous_owners), t.updated_by = $by
        WITH event, previous_owner_nodes
        UNWIND previous_owner_nodes AS previous_owner
        MERGE (event)-[:AFFECTS_DEVELOPER]->(previous_owner)
        """, params)
        return summary

    if action == "block":
        if not blocked_by or blocked_by == task_id or not _task_exists(guild_id, blocked_by):
            raise ValueError(f"blocking task {blocked_by or '(none given)'} doesn't exist in this server")
        params.update(b=blocked_by, inferred=bool(change.inferred))
        cycle = graph.query("""
        MATCH (b:Task {id: $b, guild_id: $g})-[:BLOCKED_BY*1..]->(t:Task {id: $id, guild_id: $g})
        RETURN count(*)
        """, params).result_set[0][0]
        if cycle:
            raise ValueError(f"{blocked_by} already depends on {task_id}, so that would be a loop")
        summary = f"**{task_id}** now depends on **{blocked_by}**" + (" (inferred)" if params["inferred"] else "")
        params.update(_audit_event_params(
            guild_id, change, provenance, index=index, path=path, summary=summary,
            blocked_by_id=blocked_by,
        ))
        graph.query("""
        MATCH (t:Task {id: $id, guild_id: $g}), (b:Task {id: $b, guild_id: $g})
        MERGE (t)-[r:BLOCKED_BY]->(b)
        SET r.inferred = $inferred, t.updated_by = $by
        WITH t, b
        """ + TASK_CHANGE_AUDIT_CLAUSE + "MERGE (event)-[:REFERENCES]->(b)", params)
        return summary

    if action == "unblock":
        params["b"] = blocked_by
        summary = f"**{task_id}** no longer depends on " + (f"**{blocked_by}**" if blocked_by else "anything")
        params.update(_audit_event_params(
            guild_id, change, provenance, index=index, path=path, summary=summary,
            blocked_by_id=blocked_by,
        ))
        graph.query("""
        MATCH (t:Task {id: $id, guild_id: $g})
        OPTIONAL MATCH (t)-[r:BLOCKED_BY]->(b:Task)
        WHERE $b IS NULL OR b.id = $b
        WITH t, collect(r) AS old_links, collect(b.id) AS previous_blockers,
             collect(b) AS previous_blocker_nodes
        FOREACH (old_link IN old_links | DELETE old_link)
        WITH t, previous_blockers, previous_blocker_nodes
        """ + TASK_CHANGE_AUDIT_CLAUSE + """
        SET event.previous_blockers = coalesce(event.previous_blockers, previous_blockers), t.updated_by = $by
        WITH event, previous_blocker_nodes
        UNWIND previous_blocker_nodes AS previous_blocker
        MERGE (event)-[:REFERENCES]->(previous_blocker)
        """, params)
        return summary

    if action == "set_status":
        status = (change.status or "").strip().lower().replace(" ", "_")
        if status not in {"todo", "in_progress", "done"}:
            raise ValueError(f"unknown status {change.status!r}")
        params["status"] = status
        summary = f"Marked **{task_id}** as **{status.replace('_', ' ')}**"
        params.update(_audit_event_params(
            guild_id, change, provenance, index=index, path=path, summary=summary,
        ))
        graph.query(
            "MATCH (t:Task {id: $id, guild_id: $g}) SET t.status = $status, t.updated_by = $by\n"
            "WITH t\n" + TASK_CHANGE_AUDIT_CLAUSE, params,
        )
        # Dependencies on a finished task are kept as history and simply count as satisfied
        return summary

    if action == "rename":
        if not change.title:
            raise ValueError("no new title given")
        params["title"] = change.title.strip()
        summary = f"Renamed **{task_id}** to **{params['title']}**"
        params.update(_audit_event_params(
            guild_id, change, provenance, index=index, path=path, summary=summary,
        ))
        graph.query(
            "MATCH (t:Task {id: $id, guild_id: $g}) SET t.title = $title, t.updated_by = $by\n"
            "WITH t\n" + TASK_CHANGE_AUDIT_CLAUSE, params,
        )
        return summary

    if action == "delete":
        summary = f"Deleted **{task_id}**"
        params.update(_audit_event_params(
            guild_id, change, provenance, index=index, path=path, summary=summary,
        ))
        graph.query(
            "MATCH (t:Task {id: $id, guild_id: $g})\nWITH t\n"
            + TASK_CHANGE_AUDIT_CLAUSE + "DETACH DELETE t", params,
        )
        return summary

    raise ValueError(f"unknown action {action!r}")


# Each model has its own free-tier quota, so when one is used up or overloaded we move to the next
DEFAULT_GEMINI_MODELS = "gemini-3.5-flash,gemini-2.5-flash-lite,gemini-2.5-flash"
# Stay under the bot's API timeout while waiting out per-minute limits
GEMINI_WAIT_BUDGET_SECONDS = 90


class GeminiUnavailable(Exception):
    pass


def _generate_with_fallback(gemini, prompt, config):
    models = [m.strip() for m in os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODELS).split(",") if m.strip()]
    deadline = time.monotonic() + GEMINI_WAIT_BUDGET_SECONDS
    for model in models:
        while True:
            try:
                return gemini.models.generate_content(model=model, contents=prompt, config=config)
            except genai_errors.APIError as exc:
                text = str(exc)
                per_minute = exc.code == 429 and "PerDay" not in text
                match = re.search(r"retry in ([\d.]+)s", text)
                delay = (float(match.group(1)) if match else 10) + 0.5
                if per_minute and time.monotonic() + delay < deadline:
                    print(f"{model}: per-minute limit, retrying in {delay:.0f}s")
                    time.sleep(delay)
                    continue
                if exc.code in (400, 404, 429, 500, 503):
                    print(f"{model}: unavailable ({exc.code}), trying the next model")
                    break
                raise
    raise GeminiUnavailable("all Gemini models are rate-limited or unavailable")


def _answer_after_changes(gemini, question, guild_id, applied):
    """Changes are applied after Gemini replies, so re-answer any question against the updated graph."""
    prompt = (
        "You are SigmaAgent, a project assistant in the team's Discord. These changes were just applied:\n"
        + "\n".join(applied)
        + f"\n\nUpdated task graph:\n{_dependency_report(guild_id)}\n\n"
        f"Answer the question part of this message from the updated graph in 1-4 short sentences "
        "(don't repeat the changes). Use **bold** for people and task names; no code blocks or backticks. "
        f"If the message has no question, reply with exactly NONE.\n\nMessage: {question}"
    )
    config = genai.types.GenerateContentConfig(thinking_config=genai.types.ThinkingConfig(thinking_budget=0))
    text = (_generate_with_fallback(gemini, prompt, config).text or "").strip()
    return None if text.upper().startswith("NONE") else text


def _ask_gemini(gemini, question, guild_id, messages, author, tasks):
    context = _dependency_report(guild_id)
    change_context = _relevant_task_change_context(question, guild_id, tasks)
    knowledge_context = knowledge.knowledge_context(tenant_graph(guild_id), question)
    history = "\n".join(
        f"- [{(m['timestamp'] or '')[:10]}] {m['author']}: {m['content'][:500]} (link: {m['source_url']})"
        for m in messages
    ) or "(no related messages)"
    prompt = (
        "You are SigmaAgent, a project assistant in the team's Discord. "
        "Your capabilities are listed here; when someone asks what you can do or how to do something, answer from this "
        "list and quote the exact command to type (never claim abilities that aren't listed):\n"
        f"{_render_capabilities(markdown=False)}\n\n"
        "Answer the question using only the task graph and channel messages below. If the answer isn't in them, say so. "
        "The task graph is the source of truth for owners and blockers; use the messages for extra context "
        "and link to a message when you rely on it, as [date](link). "
        "For 'why' questions about a task's owner, blocker, or status, use the relevant source-backed task change history below; "
        "name who made the change, when they made it, and link the originating Discord message. "
        "If no matching source-backed change exists, say that the decision has no recorded source; do not guess. "
        "Reply in 1-4 short sentences or a few bullet points (up to about 8 when explaining dependencies). "
        "Use **bold** for people and task names. Never use code blocks, backticks or angle brackets.\n\n"
        "You can also change tasks. Only do so when the message clearly asks for a change or states a new fact about "
        "the work (e.g. 'Priya is doing the frontend', 'T1 is done', 'T3 no longer waits on T2'); never for a plain question. "
        "Put each change in `changes` using existing task IDs: create (title, optional developer, optional blocked_by), "
        "assign (task_id, developer), unassign (task_id), block (task_id, blocked_by), unblock (task_id, optional blocked_by), "
        "set_status (task_id, status: todo, in_progress or done), rename (task_id, title), delete (task_id, only if explicitly asked). "
        "If the message mentions work that matches no existing task, create one. "
        "Always give a create its developer when the message (or the messages it refers to) names who does it; "
        "when one person is given several pieces of work ('person 4 will do testing and the CI/CD pipeline'), "
        "create each task with that same developer. Each change is applied separately, so include every field it needs. "
        "Write task titles as short Title Case names (e.g. 'Auth', 'CI/CD Pipeline').\n"
        "People changes: join_team (developer, team) / leave_team (developer, team) when a message says someone is on, "
        "joined or left a team; link_github (developer, github_login) when it says a GitHub username belongs to someone. "
        "These need no task_id.\n"
        "One message can contain several changes of different kinds; include every one. For example "
        "'Sam will build the billing API, and he's on the payments team' needs a create (title 'Billing API', developer 'Sam') "
        "AND a join_team (developer 'Sam', team 'payments'). Before answering, re-read the message and check that each "
        "stated fact about tasks, owners, dependencies, teams or GitHub accounts has its own change.\n"
        "Docs, commits and issues: when they answer the question, cite them with their link as [title](link). "
        "When citing Discord messages, link directly to the message as [date](link) or [author](link). "
        "Always provide clickable markdown links for every fact derived from docs, commits, issues, or Discord messages. "
        "If multiple sources contribute to an answer, conclude with a bulleted 'Sources:' section linking each reference.\n"
        "For 'who knows about X' questions, suggest asking exactly 'who knows about X' for a ranked, evidence-backed answer.\n"
        "Dependencies: 'depends on' means the task can't finish until those are done. When explaining dependencies, "
        "show the ordered dependency path (for example T5 ← T4 ← T3) and the owner of each step, "
        "say which tasks are ready to start now, "
        "and point out the critical path and who is holding up the most work. "
        "For 'what breaks if X slips' questions use that task's 'if this slips, it also delays' line and name the affected owners; "
        "for 'who is the bottleneck' questions use the PageRank bottleneck tasks and people; "
        "mention independent workstreams when suggesting what can run in parallel. "
        "Tell users they can say 'impact T3', 'bottlenecks' or 'explain T5' to see the graph query behind an answer. "
        "When tasks are created, or the user asks how tasks depend on each other, add any missing dependencies a "
        "typical software team would clearly agree with as block changes with inferred=true (e.g. Frontend depends on "
        "Auth and Backend, Testing depends on the features it tests, deployment/CI-CD release steps come after testing). "
        "Don't infer anything doubtful, never create loops, and never mark stated dependencies as inferred. "
        "In the answer, briefly explain why each inferred dependency makes sense and say they can be removed "
        "(e.g. 'T2 doesn't depend on T3'). Inferred dependencies already in the graph are marked [inferred].\n"
        "When the user asks for a change, always put it in `changes` even if you think it's a bad idea; "
        "the system validates every change (missing tasks, dependency loops) and tells the user why one is rejected. "
        "Every task change must have task_id set to an existing task ID (except create).\n"
        "When you make changes, keep `answer` to a short acknowledgement and don't list the changes; they are appended for you.\n\n"
        f"Task graph for this server:\n{context}\n\n"
        f"Relevant source-backed task changes:\n{change_context}\n\n"
        f"Related docs, commits, issues and teams:\n{knowledge_context}\n\n"
        "If the messages say who is doing what but that isn't in the task graph yet, answer from the messages, "
        "say it isn't tracked as a task yet, and suggest the user ask you to add it.\n\n"
        f"Recent and related messages from this channel (oldest first):\n{history}\n\n"
        f"Message from {author}: {question}"
    )
    # Thinking adds several seconds per reply and isn't needed for lookups this small
    config = genai.types.GenerateContentConfig(
        thinking_config=genai.types.ThinkingConfig(thinking_budget=0),
        response_mime_type="application/json",
        response_schema=AgentReply,
    )
    response = _generate_with_fallback(gemini, prompt, config)
    return response.parsed


NON_DEV_PHRASES = {
    "next sprint", "this sprint", "sprint", "tomorrow", "today", "yesterday",
    "v1", "v2", "mvp", "production", "staging", "testing", "review",
    "frontend", "backend", "database", "api", "mobile", "ios", "android",
    "release", "deployment", "demo", "hackathon",
}


def _clean_developer_name(dev: str | None, graph: Any) -> str | None:
    if not dev:
        return None
    cleaned = dev.strip().strip("\"'`")
    if not cleaned or cleaned.casefold() in NON_DEV_PHRASES:
        return None
    known = {row[0].casefold(): row[0] for row in graph.ro_query("MATCH (d:Developer) RETURN d.name").result_set}
    if cleaned.casefold() in known:
        return known[cleaned.casefold()]
    if re.search(r"\b(?:and|or|for|with|in|on|at|by|to|is|will|should|next|this|last)\b", cleaned, re.IGNORECASE):
        return None
    if len(cleaned.split()) > 3:
        return None
    return cleaned


DIRECT_CREATE_TASK_RE = re.compile(
    r"^\s*(?:create|add|new)\s+task\s+(?P<title>.+?)(?:\s+(?:for|assigned to)\s+(?P<dev>[A-Za-z0-9_ -]+))?\s*$",
    re.IGNORECASE,
)
DIRECT_ASSIGN_RE = re.compile(
    r"^\s*assign\s+(?P<task>(?:[A-Za-z][A-Za-z0-9]*-\d+|T\d+)|[A-Za-z0-9_ -]+?)\s+to\s+(?P<dev>[A-Za-z0-9_ -]+)\s*[!.?]*\s*$",
    re.IGNORECASE,
)
DIRECT_STATUS_RE = re.compile(
    r"^\s*(?:mark|set\s+status\s+of)\s+(?P<task>(?:[A-Za-z][A-Za-z0-9]*-\d+|T\d+)|[A-Za-z0-9_ -]+?)\s+(?:as\s+|to\s+)?(?P<status>todo|in\s+progress|in_progress|done|completed|finished)\s*[!.?]*\s*$",
    re.IGNORECASE,
)
DIRECT_LIST_TASKS_RE = re.compile(
    r"^\s*(?:list\s+tasks|what\s+are\s+the\s+tasks|show\s+tasks|tasks)\s*[?!.]*\s*$",
    re.IGNORECASE,
)


def _direct_task_command_answer(question, guild_id, discord):
    """Direct, deterministic task commands that execute immediately without requiring LLM quota."""
    if DIRECT_LIST_TASKS_RE.match(question):
        tasks = _all_tasks(guild_id)
        if not tasks:
            return "There are no tasks tracked in this server's graph yet."
        grouped = _aggregate_task_rows(tasks)
        lines = ["**Tasks in this server:**"]
        for tid, title, owners, status, _ in grouped:
            owner_str = f" ({', '.join(owners)})" if owners else " (unassigned)"
            status_str = f" [{status}]" if status and status != "todo" else ""
            lines.append(f"- **{tid}**: {title}{owner_str}{status_str}")
        return "\n".join(lines)

    provenance = _discord_provenance(discord, guild_id, question)
    author = (discord or {}).get("author_name") or "someone"
    if provenance is None:
        # No valid Discord source — let the Gemini path handle it with a consistent error message
        return None

    match = DIRECT_CREATE_TASK_RE.match(question)
    if match:
        title = match.group("title").strip()
        raw_dev = match.group("dev").strip() if match.group("dev") else None
        graph = tenant_graph(guild_id)
        dev = _clean_developer_name(raw_dev, graph)
        if raw_dev and not dev:
            title = f"{title} for {raw_dev}"

        if not title or len(title) < 2 or title.casefold().startswith("for "):
            return "Please provide a valid task title to create a task."

        change = TaskChange(action="create", title=title, developer=dev)
        try:
            applied_summary = _apply_change(guild_id, change, author, provenance)
            return f"- {applied_summary}"
        except Exception as exc:
            log.exception("Direct task creation failed: %s", exc)
            return "Couldn't create task due to an internal error. Please try again."

    match = DIRECT_ASSIGN_RE.match(question)
    if match:
        task_ref = match.group("task").strip()
        raw_dev = match.group("dev").strip()
        graph = tenant_graph(guild_id)
        dev = _clean_developer_name(raw_dev, graph)
        if not dev:
            return f"I couldn't identify '{raw_dev}' as a developer. Please name a person to assign to."
        task_id = _resolve_task(guild_id, task_ref)
        if not task_id:
            return f"I couldn't find a task matching '{task_ref}'."
        change = TaskChange(action="assign", task_id=task_id, developer=dev)
        try:
            applied_summary = _apply_change(guild_id, change, author, provenance)
            return f"- {applied_summary}"
        except Exception as exc:
            log.exception("Direct task assign failed: %s", exc)
            return "Couldn't assign task due to an internal error. Please try again."

    match = DIRECT_STATUS_RE.match(question)
    if match:
        task_ref = match.group("task").strip()
        raw_status = match.group("status").strip().lower()
        if raw_status in ("completed", "finished", "done"):
            status_val = "done"
        elif raw_status in ("in progress", "in_progress"):
            status_val = "in_progress"
        else:
            status_val = "todo"
        task_id = _resolve_task(guild_id, task_ref)
        if not task_id:
            return f"I couldn't find a task matching '{task_ref}'."
        change = TaskChange(action="set_status", task_id=task_id, status=status_val)
        try:
            applied_summary = _apply_change(guild_id, change, author, provenance)
            return f"- {applied_summary}"
        except Exception as exc:
            log.exception("Direct task status update failed: %s", exc)
            return "Couldn't update status due to an internal error. Please try again."

    return None


@app.post("/investigate")
def investigate(req: InvestigateRequest):

    discord = req.discord or {}
    read_only = req.read_only or not discord
    if HELP_RE.match(req.question):
        return _format_investigate_response(HELP_TEXT)
    if read_only and re.match(r"^\s*sync\b", req.question, re.IGNORECASE):
        return _format_investigate_response(
            "This workspace is read-only and cannot sync repositories. Use the authenticated administration endpoint instead."
        )
    # Requests from outside Discord (e.g. curl) use the default server
    server_id = discord.get("guild_id") if req.discord else os.getenv("DISCORD_GUILD_ID")
    if server_id:
        commands = (_teams_answer, _gc_answer) if read_only else (_sync_answer, _teams_answer, _gc_answer)
        for command in commands:
            command_answer = command(req.question, server_id)
            if command_answer is not None:
                return _format_investigate_response(command_answer)
        expertise_answer = _expertise_answer(req.question, server_id, discord, read_only=read_only)
        if expertise_answer is not None:
            return _format_investigate_response(expertise_answer)
    # Repository questions don't depend on a server's task graph, so answer them next
    is_people_statement = PEOPLE_STATEMENT_RE.search(req.question) and not req.question.rstrip().endswith("?")
    github_answer = None if is_people_statement else answer_github_question(req.question)
    if github_answer is not None:
        return _format_investigate_response(github_answer)
    # Requests from outside Discord (e.g. curl) use the default server
    guild_id = discord.get("guild_id") if req.discord else os.getenv("DISCORD_GUILD_ID")
    if not guild_id:
        return _format_investigate_response("Ask me in a server channel; I only know about tasks that belong to a server.")

    tasks = _all_tasks(guild_id)
    explain_answer = _explain_dependency_path(req.question, guild_id, tasks)
    if explain_answer is not None:
        return _format_investigate_response(explain_answer)
    for command in (_impact_answer, _bottleneck_answer):
        command_answer = command(req.question, guild_id)
        if command_answer is not None:
            return _format_investigate_response(command_answer)
    ownership_answer = _ownership_provenance_answer(req.question, guild_id, tasks)
    if ownership_answer is not None:
        return _format_investigate_response(ownership_answer)
    task_command_answer = _direct_task_command_answer(
        req.question, guild_id, {} if read_only else discord
    )
    if task_command_answer is not None:
        return _format_investigate_response(task_command_answer)
    gemini = _gemini_client()
    if gemini:
        try:
            author = discord.get("author_name") or "someone"
            messages = _channel_history(req.question, discord)
            reply = _ask_gemini(gemini, req.question, guild_id, messages, author, tasks)
        except GeminiUnavailable:
            return _format_investigate_response("Gemini is out of free quota or overloaded on every model I can use, "
                                              "so I can't answer properly right now; nothing was changed. "
                                              "Try again later, or add billing to the Gemini key for higher limits.")
        except Exception as exc:
            print(f"Gemini request failed, using rule-based answer: {exc}")
        else:
            if read_only and reply.changes:
                return _format_investigate_response(
                    "This workspace is read-only, so I didn't make any changes. Ask a question about the current work instead."
                )
            applied, lines = [], []
            provenance = _discord_provenance(discord, guild_id, req.question)
            for index, change in enumerate(reply.changes):
                if provenance is None:
                    lines.append(
                        "- Couldn't apply task changes: a valid Discord message, channel, and author are required "
                        "so the change can be traced to its source."
                    )
                    break
                try:
                    applied.append(
                        f"- {_apply_change(guild_id, change, author, provenance, index=index)}"
                    )
                    print(f"[{guild_id}] {author}: {change.model_dump(exclude_none=True)}")
                except ValueError as exc:
                    print(f"[{guild_id}] rejected change {change.model_dump(exclude_none=True)!r}: {exc}")
                    target = (" ".join(filter(None, [change.developer, change.team, change.github_login]))
                              if change.action in ("join_team", "leave_team", "link_github")
                              else " ".join(filter(None, [change.task_id, change.title])) or "task")
                    lines.append(f"- Couldn't {change.action.replace('_', ' ')} {target}: invalid request or task not found.")
            answer = reply.answer.strip()
            if applied and "?" in req.question:
                try:
                    answer = _answer_after_changes(gemini, req.question, guild_id, applied) or answer
                except Exception as exc:
                    print(f"Follow-up answer failed, keeping the first one: {exc}")
            return _format_investigate_response("\n".join([answer] + applied + lines))

    if not tasks:
        return _format_investigate_response("This server has no tasks yet. Add them to `tasks.json` with this server's guild_id and run `ingest_tasks.py`.")

    return _format_investigate_response(_rule_based_answer(req.question, tasks))


def _rule_based_answer(question, tasks):
    question = question.lower()
    words = set(re.findall(r"\w+", question))
    developers = {row[2] for row in tasks if row[2]}

    # Questions about a specific developer
    named = [d for d in developers if set(re.findall(r"\w+", d.lower())) <= words]
    if named:
        lines = []
        for dev in sorted(named):
            mine = [row for row in tasks if row[2] == dev]
            lines.append(f"**{dev}** is assigned:")
            lines += [f"- {_format_task(*row)}" for row in mine]
            my_titles = {row[1] for row in mine}
            waiting = [row for row in tasks if row[3] in my_titles]
            if waiting:
                lines.append("Waiting on them: " + ", ".join(f"*{row[1]}*" for row in waiting))
        return "\n".join(lines)

    # Questions about a specific task (delayed / blocked / status)
    matched = [row for row in tasks
               if row[0].lower() in words or (set(re.findall(r"\w+", row[1].lower())) & words) - {"the", "a", "ui"}]
    if matched and not words & {"tasks", "list", "all"}:
        lines = []
        for row in matched:
            lines.append(_format_task(*row))
            if row[3]:
                blocker = next((r for r in tasks if r[1] == row[3]), None)
                owner = blocker[2] if blocker else None
                lines.append(f"It can't move until *{row[3]}* is done ({owner or 'unassigned'}).")
        return "\n".join(lines)

    if words & {"blocked", "blocking", "delayed", "delay", "stuck", "dependencies", "depends", "waiting"}:
        blocked = [row for row in tasks if row[3]]
        if not blocked:
            return "Nothing is blocked right now."
        lines = ["Blocked tasks:"]
        for row in blocked:
            blocker = next((r for r in tasks if r[1] == row[3]), None)
            owner = blocker[2] if blocker else None
            lines.append(f"- *{row[1]}* is waiting on *{row[3]}* ({owner or 'unassigned'})")
        return "\n".join(lines)

    if words & {"tasks", "list", "all", "status", "everything"}:
        return "\n".join(_format_task(*row) for row in tasks)

    return HELP_TEXT
