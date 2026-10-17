import os
import re
import hashlib
import json
import time
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from falkordb import FalkorDB
from google import genai
from google.genai import errors as genai_errors
from pydantic import BaseModel

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

app = FastAPI(title="Sigma Agent API")

# 1. Database Credentials
CLOUD_HOST = os.getenv("FALKORDB_HOST", "r-6jissuruar.instance-gijzjhvh6.hc-7up0crkyn.ap-south-1.aws.f2e0a955bb84.cloud")
CLOUD_PORT = int(os.getenv("FALKORDB_PORT", 52078))
CLOUD_USERNAME = os.getenv("FALKORDB_USERNAME", "falkordb")
CLOUD_PASSWORD = os.getenv("FALKORDB_PASSWORD", "password")

# 2. Connect to database
db = FalkorDB(host=CLOUD_HOST, port=CLOUD_PORT, username=CLOUD_USERNAME, password=CLOUD_PASSWORD)
graph = db.select_graph("SigmaCompanyBrain")

# 3. Create the API Endpoint
@app.get("/dependencies")
def get_task_dependencies():
    # Query the graph for all blocking relationships
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
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=503, detail=str(exc))


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
class InvestigateRequest(BaseModel):
    question: str
    # Sent by the Discord bot: guild_id, channel_id, message_id, bot_name, ...
    discord: dict | None = None


class TaskChange(BaseModel):
    action: str  # create | assign | unassign | block | unblock | set_status | rename | delete
    task_id: str | None = None  # existing task ID; leave empty for create
    title: str | None = None  # for create / rename
    developer: str | None = None  # for create / assign
    blocked_by: str | None = None  # ID of the blocking task, for create / block / unblock
    status: str | None = None  # todo | in_progress | done, for set_status
    inferred: bool | None = None  # block only: dependency guessed from the kind of work, not stated


class AgentReply(BaseModel):
    answer: str
    changes: list[TaskChange]


HELP_TEXT = (
    "I answer questions about the team's task graph. Try:\n"
    "- `what is blocked?` / `why is X delayed?`\n"
    "- `what is <developer> working on?`\n"
    "- `list tasks`"
)


def _all_tasks(guild_id):
    result = graph.query("""
    MATCH (t:Task {guild_id: $guild_id})
    OPTIONAL MATCH (d:Developer)-[:ASSIGNED_TO]->(t)
    OPTIONAL MATCH (t)-[:BLOCKED_BY]->(b:Task)
    WHERE coalesce(b.status, 'todo') <> 'done'
    RETURN t.id, t.title, d.name, b.title, t.status
    ORDER BY t.id
    """, {"guild_id": guild_id})
    return result.result_set


def _dependency_report(guild_id):
    """Describe each task's owners, dependencies, what it holds up, and what's ready, for Gemini."""
    rows = graph.query("""
    MATCH (t:Task {guild_id: $g})
    OPTIONAL MATCH (d:Developer)-[:ASSIGNED_TO]->(t)
    WITH t, collect(d.name) AS owners
    OPTIONAL MATCH (t)-[r:BLOCKED_BY]->(b:Task)
    RETURN t.id, t.title, coalesce(t.status, 'todo'), owners, collect([b.id, r.inferred])
    ORDER BY t.id
    """, {"g": guild_id}).result_set
    if not rows:
        return "(no tasks for this server)"

    tasks = {}
    for task_id, title, status, owners, deps in rows:
        tasks[task_id] = {"title": title, "status": status, "owners": owners,
                          "deps": {dep_id: bool(inferred) for dep_id, inferred in deps if dep_id}}
    dependents = {task_id: [] for task_id in tasks}
    for task_id, task in tasks.items():
        for dep_id in task["deps"]:
            dependents.setdefault(dep_id, []).append(task_id)

    def upstream(task_id, seen=None):
        seen = set() if seen is None else seen
        for dep_id in tasks[task_id]["deps"]:
            if dep_id in tasks and dep_id not in seen:
                seen.add(dep_id)
                upstream(dep_id, seen)
        return seen

    longest_open_cache = {}

    def longest_open_chain(task_id, visiting=()):
        # Longest chain of unfinished work ending at this task (guards against cycles)
        if task_id in visiting:
            return []
        if task_id in longest_open_cache:
            return longest_open_cache[task_id]
        if tasks[task_id]["status"] == "done":
            longest_open_cache[task_id] = []
            return []
        best = []
        for dep_id in tasks[task_id]["deps"]:
            if dep_id in tasks:
                chain = longest_open_chain(dep_id, visiting + (task_id,))
                best = max(best, chain, key=len)
        result = best + [task_id]
        longest_open_cache[task_id] = result
        return result

    def label(task_id):
        task = tasks[task_id]
        return f"{task_id} {task['title']} ({task['status']})"

    def dependency_chain(task_id, visiting=()):
        """Return the longest upstream blocker chain, from this task to its root dependency."""
        if task_id in visiting:
            return [task_id]
        if task_id in dependency_chain.cache:
            return dependency_chain.cache[task_id]
        candidates = [dependency_chain(dep_id, visiting + (task_id,))
                      for dep_id in tasks[task_id]["deps"] if dep_id in tasks]
        chain = [task_id] + max(candidates, key=len, default=[])
        dependency_chain.cache[task_id] = chain
        return chain

    dependency_chain.cache = {}

    lines = []
    for task_id, task in tasks.items():
        deps = [f"{label(d)}{' [inferred]' if inferred else ''}" for d, inferred in task["deps"].items() if d in tasks]
        open_deps = [d for d in task["deps"] if d in tasks and tasks[d]["status"] != "done"]
        if task["status"] == "done":
            readiness = "finished"
        elif open_deps:
            readiness = "waiting on " + ", ".join(open_deps)
        else:
            readiness = "ready to work on now"
        path_label = " ← ".join(
            f"{node} ({', '.join(tasks[node]['owners']) or 'unassigned'})"
            for node in dependency_chain(task_id)
        )
        lines.append(
            f"- {task_id}: {task['title']} | owner: {', '.join(task['owners']) or 'unassigned'} | status: {task['status']}\n"
            f"    depends on: {'; '.join(deps) or 'nothing'}\n"
            f"    dependency path: {path_label}\n"
            f"    everything upstream: {', '.join(sorted(upstream(task_id))) or 'nothing'}\n"
            f"    holds up: {', '.join(dependents.get(task_id, [])) or 'nothing'}\n"
            f"    {readiness}"
        )
    critical = max((longest_open_chain(t) for t in tasks), key=len, default=[])
    if len(critical) > 1:
        lines.append("Longest chain of unfinished work (critical path): " + " -> ".join(label(t) for t in critical))
    return "\n".join(lines)


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
    match = re.match(r"^\s*explain\b\s*(.*)$", question, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    raw_ref = match.group(1).strip()
    target_ref = raw_ref.strip("?.,!`*_ ").strip("\"'")
    if not target_ref:
        return "Usage: `@sigmaagent explain T5` (or provide a task title)."

    task_rows = _aggregate_task_rows(tasks)
    task_matches = [row for row in task_rows if str(row[0]).casefold() == target_ref.casefold()]
    is_task_id = bool(re.fullmatch(r"T\d+", target_ref, flags=re.IGNORECASE))
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


def _task_change_history(guild_id, task_id=None, *, action=None, developer_name=None):
    """Return source-linked task changes, newest first, for this Discord server."""
    result = graph.query(
        """
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
        """,
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
    task_match = re.search(r"\bT\d+\b", question, flags=re.IGNORECASE)
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
    return "\n".join(lines)


def _relevant_task_change_context(question, guild_id, tasks):
    """Fetch a small audit trail only for explicit, current tasks in a 'why' question."""
    if not re.search(r"\bwhy\b", question, flags=re.IGNORECASE):
        return "(no task-change history requested)"
    grouped_tasks = _aggregate_task_rows(tasks)
    ids = {match.casefold() for match in re.findall(r"\bT\d+\b", question, flags=re.IGNORECASE)}
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
                "feature_title": question[:300], "channel_id": channel_id, "limit": 15,
            })
            response.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"Context retrieval failed, answering from tasks only: {exc}")
        return []

    # Keyword search misses recent statements that share no words with the question
    # ("person 1 will do auth" vs "how is the work divided"), so always add the latest messages too
    recent = graph.query("""
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
    ids = graph.query("MATCH (t:Task {guild_id: $g}) RETURN t.id", {"g": guild_id}).result_set
    numbers = [int(row[0][1:]) for row in ids if re.fullmatch(r"T\d+", row[0] or "")]
    return f"T{max(numbers, default=0) + 1}"


def _developer_name(name):
    """Reuse an existing developer's spelling ("person 1" -> "Person 1") so case doesn't split people."""
    name = " ".join(name.split())
    existing = graph.query("MATCH (d:Developer) WHERE toLower(d.name) = toLower($name) RETURN d.name LIMIT 1",
                           {"name": name}).result_set
    if existing:
        return existing[0][0]
    return name[:1].upper() + name[1:] if name.islower() else name


def _resolve_task(guild_id, ref):
    """Turn 'T2', 't2' or a task title into this server's task ID; None if there's no such task."""
    ref = (ref or "").strip()
    if not ref:
        return None
    if _task_exists(guild_id, ref.upper()):
        return ref.upper()
    match = graph.query("MATCH (t:Task {guild_id: $g}) WHERE toLower(t.title) = toLower($title) RETURN t.id LIMIT 1",
                        {"g": guild_id, "title": ref}).result_set
    return match[0][0] if match else None


def _task_exists(guild_id, task_id):
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


def _apply_change(guild_id, change, author, provenance, *, index=0, path=""):
    """Apply one change to this server's tasks and describe it, or raise ValueError."""
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

    if not task_id or not _task_exists(guild_id, task_id):
        raise ValueError(f"task {task_id or '(none given)'} doesn't exist in this server")

    if action == "assign":
        if not change.developer:
            raise ValueError("no developer given")
        params["dev"] = _developer_name(change.developer)
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
    history = "\n".join(
        f"- [{(m['timestamp'] or '')[:10]}] {m['author']}: {m['content'][:500]} (link: {m['source_url']})"
        for m in messages
    ) or "(no related messages)"
    prompt = (
        "You are SigmaAgent, a project assistant in the team's Discord. "
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
        "Dependencies: 'depends on' means the task can't finish until those are done. When explaining dependencies, "
        "show the ordered dependency path (for example T5 ← T4 ← T3) and the owner of each step, "
        "say which tasks are ready to start now, "
        "and point out the critical path and who is holding up the most work. "
        "When tasks are created, or the user asks how tasks depend on each other, add any missing dependencies a "
        "typical software team would clearly agree with as block changes with inferred=true (e.g. Frontend depends on "
        "Auth and Backend, Testing depends on the features it tests, deployment/CI-CD release steps come after testing). "
        "Don't infer anything doubtful, never create loops, and never mark stated dependencies as inferred. "
        "In the answer, briefly explain why each inferred dependency makes sense and say they can be removed "
        "(e.g. 'T2 doesn't depend on T3'). Inferred dependencies already in the graph are marked [inferred].\n"
        "When the user asks for a change, always put it in `changes` even if you think it's a bad idea; "
        "the system validates every change (missing tasks, dependency loops) and tells the user why one is rejected. "
        "Every change must have task_id set to an existing task ID (except create).\n"
        "When you make changes, keep `answer` to a short acknowledgement and don't list the changes; they are appended for you.\n\n"
        f"Task graph for this server:\n{context}\n\n"
        f"Relevant source-backed task changes:\n{change_context}\n\n"
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


@app.post("/investigate")
def investigate(req: InvestigateRequest):
    discord = req.discord or {}
    # Repository questions don't depend on a server's task graph, so answer them first
    github_answer = answer_github_question(req.question)
    if github_answer is not None:
        return {"answer": github_answer}
    # Requests from outside Discord (e.g. curl) use the default server
    guild_id = discord.get("guild_id") if req.discord else os.getenv("DISCORD_GUILD_ID")
    if not guild_id:
        return {"answer": "Ask me in a server channel; I only know about tasks that belong to a server."}

    tasks = _all_tasks(guild_id)
    explain_answer = _explain_dependency_path(req.question, guild_id, tasks)
    if explain_answer is not None:
        return {"answer": explain_answer}
    ownership_answer = _ownership_provenance_answer(req.question, guild_id, tasks)
    if ownership_answer is not None:
        return {"answer": ownership_answer}
    gemini = _gemini_client()
    if gemini:
        try:
            author = discord.get("author_name") or "someone"
            messages = _channel_history(req.question, discord)
            reply = _ask_gemini(gemini, req.question, guild_id, messages, author, tasks)
        except GeminiUnavailable:
            return {"answer": "Gemini is out of free quota or overloaded on every model I can use, "
                              "so I can't answer properly right now; nothing was changed. "
                              "Try again later, or add billing to the Gemini key for higher limits."}
        except Exception as exc:
            print(f"Gemini request failed, using rule-based answer: {exc}")
        else:
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
                    target = " ".join(filter(None, [change.task_id, change.title])) or "task"
                    lines.append(f"- Couldn't {change.action.replace('_', ' ')} {target}: {exc}")
            answer = reply.answer.strip()
            if applied and "?" in req.question:
                try:
                    answer = _answer_after_changes(gemini, req.question, guild_id, applied) or answer
                except Exception as exc:
                    print(f"Follow-up answer failed, keeping the first one: {exc}")
            return {"answer": "\n".join([answer] + applied + lines)}

    if not tasks:
        return {"answer": "This server has no tasks yet. Add them to `tasks.json` with this server's guild_id and run `ingest_tasks.py`."}

    return {"answer": _rule_based_answer(req.question, tasks)}


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
