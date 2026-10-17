import os
import re

import time

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI
from falkordb import FalkorDB
from google import genai
from google.genai import errors as genai_errors
from pydantic import BaseModel

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

    def longest_open_chain(task_id, visiting=()):
        # Longest chain of unfinished work ending at this task (guards against cycles)
        if task_id in visiting or tasks[task_id]["status"] == "done":
            return []
        best = []
        for dep_id in tasks[task_id]["deps"]:
            if dep_id in tasks:
                chain = longest_open_chain(dep_id, visiting + (task_id,))
                best = max(best, chain, key=len)
        return best + [task_id]

    def label(task_id):
        task = tasks[task_id]
        return f"{task_id} {task['title']} ({task['status']})"

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
        lines.append(
            f"- {task_id}: {task['title']} | owner: {', '.join(task['owners']) or 'unassigned'} | status: {task['status']}\n"
            f"    depends on: {'; '.join(deps) or 'nothing'}\n"
            f"    everything upstream: {', '.join(sorted(upstream(task_id))) or 'nothing'}\n"
            f"    holds up: {', '.join(dependents.get(task_id, [])) or 'nothing'}\n"
            f"    {readiness}"
        )
    critical = max((longest_open_chain(t) for t in tasks), key=len, default=[])
    if len(critical) > 1:
        lines.append("Longest chain of unfinished work (critical path): " + " -> ".join(label(t) for t in critical))
    return "\n".join(lines)


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


def _apply_change(guild_id, change, author):
    """Apply one change to this server's tasks and describe it, or raise ValueError."""
    action = change.action
    task_id = _resolve_task(guild_id, change.task_id) or (change.task_id or "").strip().upper() or None
    blocked_by = _resolve_task(guild_id, change.blocked_by) or (change.blocked_by or "").strip().upper() or None
    params = {"g": guild_id, "id": task_id, "by": author}

    if action == "create":
        if not change.title:
            raise ValueError("a new task needs a title")
        params.update(id=_next_task_id(guild_id), title=change.title.strip())
        graph.query("CREATE (:Task {id: $id, guild_id: $g, title: $title, status: 'todo', updated_by: $by})", params)
        result = f"Created **{params['id']}: {params['title']}**"
        task_id = params["id"]
        if change.developer:
            result += "; " + _apply_change(guild_id, TaskChange(action="assign", task_id=task_id, developer=change.developer), author)
        if blocked_by:
            result += "; " + _apply_change(guild_id, TaskChange(action="block", task_id=task_id, blocked_by=blocked_by), author)
        return result

    if not task_id or not _task_exists(guild_id, task_id):
        raise ValueError(f"task {task_id or '(none given)'} doesn't exist in this server")

    if action == "assign":
        if not change.developer:
            raise ValueError("no developer given")
        params["dev"] = _developer_name(change.developer)
        graph.query("""
        MATCH (t:Task {id: $id, guild_id: $g})
        OPTIONAL MATCH (:Developer)-[old:ASSIGNED_TO]->(t)
        DELETE old
        WITH DISTINCT t
        MERGE (d:Developer {name: $dev})
        MERGE (d)-[:ASSIGNED_TO]->(t)
        SET t.updated_by = $by
        """, params)
        return f"Assigned **{task_id}** to **{params['dev']}**"

    if action == "unassign":
        graph.query("MATCH (:Developer)-[r:ASSIGNED_TO]->(t:Task {id: $id, guild_id: $g}) DELETE r", params)
        return f"Unassigned **{task_id}**"

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
        graph.query("""
        MATCH (t:Task {id: $id, guild_id: $g}), (b:Task {id: $b, guild_id: $g})
        MERGE (t)-[r:BLOCKED_BY]->(b)
        SET r.inferred = $inferred, t.updated_by = $by
        """, params)
        return f"**{task_id}** now depends on **{blocked_by}**" + (" (inferred)" if params["inferred"] else "")

    if action == "unblock":
        params["b"] = blocked_by
        graph.query("""
        MATCH (t:Task {id: $id, guild_id: $g})-[r:BLOCKED_BY]->(b:Task)
        WHERE $b IS NULL OR b.id = $b
        DELETE r
        """, params)
        return f"**{task_id}** no longer depends on " + (f"**{blocked_by}**" if blocked_by else "anything")

    if action == "set_status":
        status = (change.status or "").strip().lower().replace(" ", "_")
        if status not in {"todo", "in_progress", "done"}:
            raise ValueError(f"unknown status {change.status!r}")
        params["status"] = status
        graph.query("MATCH (t:Task {id: $id, guild_id: $g}) SET t.status = $status, t.updated_by = $by", params)
        # Dependencies on a finished task are kept as history and simply count as satisfied
        return f"Marked **{task_id}** as **{status.replace('_', ' ')}**"

    if action == "rename":
        if not change.title:
            raise ValueError("no new title given")
        params["title"] = change.title.strip()
        graph.query("MATCH (t:Task {id: $id, guild_id: $g}) SET t.title = $title, t.updated_by = $by", params)
        return f"Renamed **{task_id}** to **{params['title']}**"

    if action == "delete":
        graph.query("MATCH (t:Task {id: $id, guild_id: $g}) DETACH DELETE t", params)
        return f"Deleted **{task_id}**"

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


def _ask_gemini(gemini, question, guild_id, messages, author):
    context = _dependency_report(guild_id)
    history = "\n".join(
        f"- [{(m['timestamp'] or '')[:10]}] {m['author']}: {m['content'][:500]} (link: {m['source_url']})"
        for m in messages
    ) or "(no related messages)"
    prompt = (
        "You are SigmaAgent, a project assistant in the team's Discord. "
        "Answer the question using only the task graph and channel messages below. If the answer isn't in them, say so. "
        "The task graph is the source of truth for owners and blockers; use the messages for extra context "
        "and link to a message when you rely on it, as [date](link). "
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
        "walk the chain (what waits on what, who owns each step), say which tasks are ready to start now, "
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
    # Requests from outside Discord (e.g. curl) use the default server
    guild_id = discord.get("guild_id") if req.discord else os.getenv("DISCORD_GUILD_ID")
    if not guild_id:
        return {"answer": "Ask me in a server channel; I only know about tasks that belong to a server."}

    tasks = _all_tasks(guild_id)
    gemini = _gemini_client()
    if gemini:
        try:
            author = discord.get("author_name") or "someone"
            messages = _channel_history(req.question, discord)
            reply = _ask_gemini(gemini, req.question, guild_id, messages, author)
        except GeminiUnavailable:
            return {"answer": "Gemini is out of free quota or overloaded on every model I can use, "
                              "so I can't answer properly right now; nothing was changed. "
                              "Try again later, or add billing to the Gemini key for higher limits."}
        except Exception as exc:
            print(f"Gemini request failed, using rule-based answer: {exc}")
        else:
            applied, lines = [], []
            for change in reply.changes:
                try:
                    applied.append(f"- {_apply_change(guild_id, change, author)}")
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
