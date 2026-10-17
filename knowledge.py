"""Company knowledge in each Discord server's graph: GitHub work, Markdown docs, teams, and who knows what.

`sync_repository` pulls a GitHub repository's commits (with the files they touch), issues and pull
requests, and Markdown docs into the server's FalkorDB graph, and links them to the tasks and
people they mention. `expertise` answers "who knows about X?" by walking that evidence:

    (:Developer)-[:ASSIGNED_TO]->(:Task)                    owns related work
    (:GitHubUser)-[:AUTHORED]->(:Commit)-[:TOUCHES]->(:File) changed related code
    (:GitHubUser)-[:OPENED|ASSIGNED]->(:Issue)              filed or handles related issues
    (:DocSection)-[:MENTIONS]->(:Developer)                  named in related docs
    (:DiscordAuthor)-[:AUTHORED]->(:DiscordMessage)          talked about it in chat
    (:Developer)-[:MEMBER_OF]->(:Team)                       is on a related team

GitHub accounts and Discord authors count as the same person as a Developer through
(:GitHubUser)-[:SAME_AS]->(:Developer) and (:DiscordAuthor)-[:MAPS_TO]->(:Developer).
`refresh_expertise` stores the result as (person)-[:EXPERT_IN {score}]->(:Topic).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from github_analysis import GitHubNotFound, GitHubUnavailable, gh_get, is_safe_repo

INDEXES = (
    ("GitHubUser", "login"), ("Commit", "sha"), ("Issue", "key"), ("File", "key"),
    ("Document", "key"), ("DocSection", "id"), ("Topic", "name"), ("Team", "key"),
    ("Repository", "full_name"),
)
FULLTEXT_INDEXES = {"DocSection": ("heading", "content"), "Commit": ("message",), "Issue": ("title",)}

# Words that never make a useful topic or search term
STOPWORDS = {
    "about", "after", "again", "also", "and", "any", "are", "best", "can", "could", "does", "for", "from",
    "has", "have", "help", "how", "into", "know", "knows", "most", "need", "our", "person", "should", "that",
    "the", "their", "them", "there", "this", "what", "when", "where", "which", "who", "why", "will", "with",
    "work", "worked", "working", "would", "you", "your", "ask", "expert", "experts", "team", "is", "it",
    "on", "in", "to", "of", "a", "an", "we", "i", "me", "my", "do", "be", "or", "if", "at", "by", "us",
}
# Path parts too generic to be a topic of expertise
GENERIC_PATH_PARTS = {
    "src", "app", "lib", "tests", "test", "index", "main", "init", "__init__", "readme", "license", "docs",
    "utils", "util", "common", "core", "public", "assets", "scripts", "config", "setup", "package",
    "requirements", "dockerfile", "makefile", "github", "workflows", "node_modules", "dist", "build",
    "components", "pages", "styles", "types", "static", "examples", "example", "py", "js", "ts", "md",
    # file formats and tooling
    "lock", "toml", "yml", "yaml", "json", "txt", "cfg", "ini", "pyproject", "poetry", "uv", "mdx", "tsx",
    "jsx", "css", "html", "env", "gitignore", "dockerignore", "github_actions", "dependabot",
}
# Automation accounts that open PRs or commit but aren't people (plus anything ending in [bot])
BOT_LOGINS = ["copilot", "github-actions", "dependabot", "renovate", "codecov", "pre-commit-ci"]
MAX_DOC_FILES = 40
MAX_SECTION_CHARS = 4000
COMMIT_EVIDENCE_CAP = 6  # one prolific committer shouldn't drown out everyone else


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(*parts: str) -> str:
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()


def ensure_indexes(graph: Any) -> None:
    for label, prop in INDEXES:
        _create_index(graph, f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
    for label, props in FULLTEXT_INDEXES.items():
        args = ", ".join(f"'{prop}'" for prop in props)
        _create_index(graph, f"CALL db.idx.fulltext.createNodeIndex('{label}', {args})")


def _create_index(graph: Any, query: str) -> None:
    try:
        graph.query(query)
    except Exception as exc:
        text = str(exc).casefold()
        if "already indexed" not in text and "already exists" not in text:
            raise


# --- Text helpers -------------------------------------------------------------------------------

def search_terms(text: str) -> list[str]:
    """Meaningful words from a question, in order, without stopwords or duplicates."""
    terms: list[str] = []
    for word in re.findall(r"[A-Za-z][A-Za-z0-9_+.#-]{2,}", text or ""):
        word = word.strip(".-").casefold()
        if len(word) >= 3 and word not in STOPWORDS and word not in terms:
            terms.append(word)
    return terms


def fulltext_query(text: str) -> str:
    """A RediSearch OR-query of the question's words (plain tokens only, no operators)."""
    return "|".join(re.sub(r"[^\w]", "", term) for term in search_terms(text) if re.sub(r"[^\w]", "", term))


def mentioned_tasks(text: str, tasks: list[tuple[str, str]]) -> list[str]:
    """Task IDs referenced in text by ID (T3) or by their full title as a whole phrase."""
    found = set()
    ids = {task_id.upper() for task_id, _ in tasks}
    for match in re.findall(r"\bT\d+\b", text or "", flags=re.IGNORECASE):
        if match.upper() in ids:
            found.add(match.upper())
    for task_id, title in tasks:
        if title and len(title) >= 3 and re.search(rf"(?<!\w){re.escape(title)}(?!\w)", text or "", re.IGNORECASE):
            found.add(task_id)
    return sorted(found)


def mentioned_people(text: str, names: list[str]) -> list[str]:
    return sorted(name for name in names
                  if name and re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text or "", re.IGNORECASE))


def _github_anchor(heading: str) -> str:
    anchor = re.sub(r"[^\w\- ]", "", heading.strip().casefold())
    return anchor.replace(" ", "-")


def _clean_markdown(text: str) -> str:
    text = re.sub(r"\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)", "", text)  # linked badges
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)  # images
    text = re.sub(r"<[^>]+>", "", text)  # inline HTML
    return re.sub(r"\n{3,}", "\n\n", text)


def split_markdown(text: str) -> list[dict[str, str]]:
    """Split Markdown into sections at headings (#, ##, ###), keeping text before the first heading."""
    text = _clean_markdown(text or "")
    sections: list[dict[str, str]] = []
    heading, lines = "", []
    in_code = False
    for line in (text or "").splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
        match = None if in_code else re.match(r"^(#{1,3})\s+(.+?)\s*#*\s*$", line)
        if match:
            if heading or "".join(lines).strip():
                sections.append({"heading": heading, "content": "\n".join(lines).strip()})
            heading, lines = match.group(2).strip(), []
        else:
            lines.append(line)
    if heading or "".join(lines).strip():
        sections.append({"heading": heading, "content": "\n".join(lines).strip()})
    return [s for s in sections if s["content"] or s["heading"]]


def path_topics(path: str) -> list[str]:
    """Topic words from a file path, e.g. 'services/billing/invoice_api.py' -> billing, invoice."""
    words = []
    for part in re.split(r"[/_.\-\s]+", path.casefold()):
        if len(part) >= 3 and part not in GENERIC_PATH_PARTS and part not in STOPWORDS and not part.isdigit():
            words.append(part)
    return words


# --- GitHub sync ----------------------------------------------------------------------------------

def _github_token() -> str | None:
    return os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")


def _raw_file(repo: str, ref: str, path: str) -> str:
    """Read a file through raw.githubusercontent.com, which doesn't use the REST API rate limit."""
    headers = {"User-Agent": "SigmaAgent (https://github.com/Sumit884-byte/sigmaAgent)"}
    token = _github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = httpx.get(f"https://raw.githubusercontent.com/{repo}/{ref}/{path}", headers=headers,
                         timeout=15, follow_redirects=True)
    if response.status_code >= 400:
        raise GitHubUnavailable(f"could not read {path} (HTTP {response.status_code})")
    return response.text


COMMIT_UPSERT = """
UNWIND $rows AS row
MERGE (repo:Repository {full_name: $repo})
MERGE (c:Commit {sha: row.sha})
SET c.message = row.message, c.url = row.url, c.timestamp = row.timestamp, c.repo = $repo, c.is_merge = row.is_merge
MERGE (u:GitHubUser {login: row.login})
SET u.url = row.user_url, u.name = row.author_name
MERGE (u)-[:AUTHORED]->(c)
MERGE (c)-[:IN_REPO]->(repo)
WITH c, row
UNWIND row.files AS f
MERGE (file:File {key: $repo + ':' + f.path})
SET file.path = f.path, file.repo = $repo, file.url = f.url
MERGE (c)-[t:TOUCHES]->(file)
SET t.additions = f.additions, t.deletions = f.deletions
"""

ISSUE_UPSERT = """
UNWIND $rows AS row
MERGE (repo:Repository {full_name: $repo})
MERGE (i:Issue {key: row.key})
SET i.number = row.number, i.title = row.title, i.state = row.state, i.url = row.url,
    i.is_pull_request = row.is_pull_request, i.labels = row.labels, i.repo = $repo, i.updated_at = row.updated_at
MERGE (i)-[:IN_REPO]->(repo)
MERGE (author:GitHubUser {login: row.author})
MERGE (author)-[:OPENED]->(i)
WITH i, row
UNWIND row.assignees AS login
MERGE (assignee:GitHubUser {login: login})
MERGE (assignee)-[:ASSIGNED]->(i)
"""

DOC_UPSERT = """
UNWIND $docs AS d
MERGE (repo:Repository {full_name: $repo})
MERGE (doc:Document {key: d.key})
SET doc.path = d.path, doc.title = d.title, doc.url = d.url, doc.repo = $repo, doc.synced_at = $now
MERGE (doc)-[:IN_REPO]->(repo)
WITH doc, d
OPTIONAL MATCH (doc)-[:HAS_SECTION]->(old:DocSection)
WHERE NOT old.id IN d.section_ids
DETACH DELETE old
"""

SECTION_UPSERT = """
UNWIND $sections AS s
MATCH (doc:Document {key: s.doc_key})
MERGE (sec:DocSection {id: s.id})
SET sec.heading = s.heading, sec.content = s.content, sec.url = s.url, sec.position = s.position,
    sec.path = s.path
MERGE (doc)-[:HAS_SECTION]->(sec)
"""

MENTION_LINKS = {
    "Commit": "MATCH (n:Commit {sha: l.source}), (t:Task {id: l.target}) MERGE (n)-[:MENTIONS]->(t)",
    "Issue": "MATCH (n:Issue {key: l.source}), (t:Task {id: l.target}) MERGE (n)-[:MENTIONS]->(t)",
    "DocSection": "MATCH (n:DocSection {id: l.source}), (t:Task {id: l.target}) MERGE (n)-[:MENTIONS]->(t)",
    "DocSectionPerson": "MATCH (n:DocSection {id: l.source}), (d:Developer {name: l.target}) MERGE (n)-[:MENTIONS]->(d)",
}


def _link(graph: Any, kind: str, links: list[dict[str, str]]) -> None:
    if links:
        graph.query("UNWIND $links AS l " + MENTION_LINKS[kind], {"links": links})


def sync_repository(graph: Any, repo: str, *, max_commits: int | None = None) -> dict[str, Any]:
    """Pull one repository's commits, issues/PRs and Markdown docs into this server's graph."""
    if not is_safe_repo(repo):
        raise GitHubNotFound(f"invalid repository {repo!r}")
    ensure_indexes(graph)
    # Unauthenticated GitHub allows 60 requests an hour; each commit's file list costs one
    max_commits = max_commits or (100 if _github_token() else 20)
    meta = gh_get(f"/repos/{repo}")
    branch = meta.get("default_branch") or "main"
    graph.query("MERGE (r:Repository {full_name: $repo}) SET r.url = $url, r.default_branch = $branch, r.synced_at = $now",
                {"repo": repo, "url": meta.get("html_url") or f"https://github.com/{repo}", "branch": branch, "now": _now()})
    tasks = [(row[0], row[1]) for row in graph.ro_query("MATCH (t:Task) RETURN t.id, t.title").result_set]
    developers = [row[0] for row in graph.ro_query("MATCH (d:Developer) RETURN d.name").result_set]
    warnings: list[str] = []

    # Commits and the files each one touched
    commits = gh_get(f"/repos/{repo}/commits", {"per_page": str(max_commits), "sha": branch})
    rows, task_links = [], []
    for item in commits if isinstance(commits, list) else []:
        sha = item.get("sha")
        if not sha:
            continue
        files = []
        is_merge = len(item.get("parents") or []) > 1
        try:
            if is_merge:
                raise StopIteration  # a merge's files belong to the commits it merges
            detail = gh_get(f"/repos/{repo}/commits/{sha}")
            files = [{"path": f.get("filename"), "additions": int(f.get("additions") or 0),
                      "deletions": int(f.get("deletions") or 0),
                      "url": f"https://github.com/{repo}/blob/{sha}/{f.get('filename')}"}
                     for f in detail.get("files", []) if f.get("filename")]
        except StopIteration:
            pass
        except GitHubUnavailable as exc:
            if not warnings:
                warnings.append(f"file lists stopped early: {exc}")
        commit = item.get("commit") or {}
        message = (commit.get("message") or "").strip()
        user = item.get("author") or {}
        author_name = (commit.get("author") or {}).get("name") or "unknown"
        rows.append({
            "sha": sha, "message": message.splitlines()[0][:300] if message else "", "url": item.get("html_url") or "",
            "timestamp": (commit.get("author") or {}).get("date") or "",
            # Commits from an email that isn't tied to a GitHub account keep the git author name
            "login": user.get("login") or f"git:{author_name}", "user_url": user.get("html_url") or "",
            "author_name": author_name, "files": files, "is_merge": is_merge,
        })
        task_links += [{"source": sha, "target": task_id} for task_id in mentioned_tasks(message, tasks)]
    if rows:
        graph.query(COMMIT_UPSERT, {"repo": repo, "rows": rows})
    _link(graph, "Commit", task_links)

    # Issues and pull requests (GitHub returns both from /issues)
    issues = gh_get(f"/repos/{repo}/issues", {"state": "all", "per_page": "100", "sort": "updated", "direction": "desc"})
    issue_rows, task_links = [], []
    for item in issues if isinstance(issues, list) else []:
        key = f"{repo}#{item.get('number')}"
        issue_rows.append({
            "key": key, "number": item.get("number"), "title": (item.get("title") or "")[:300],
            "state": item.get("state") or "open", "url": item.get("html_url") or "",
            "is_pull_request": "pull_request" in item, "updated_at": item.get("updated_at") or "",
            "labels": [label.get("name") for label in item.get("labels", []) if isinstance(label, dict) and label.get("name")],
            "author": (item.get("user") or {}).get("login") or "unknown",
            "assignees": [a.get("login") for a in item.get("assignees", []) if isinstance(a, dict) and a.get("login")],
        })
        task_links += [{"source": key, "target": task_id}
                       for task_id in mentioned_tasks(f"{item.get('title')}\n{item.get('body') or ''}", tasks)]
    if issue_rows:
        graph.query(ISSUE_UPSERT, {"repo": repo, "rows": issue_rows})
    _link(graph, "Issue", task_links)

    # Markdown docs, split into sections at headings
    tree = gh_get(f"/repos/{repo}/git/trees/{branch}", {"recursive": "1"})
    paths = [entry["path"] for entry in tree.get("tree", [])
             if entry.get("type") == "blob" and entry["path"].casefold().endswith((".md", ".mdx"))
             and not re.search(r"(^|/)(node_modules|\.github|vendor|dist|build)/", entry["path"])][:MAX_DOC_FILES]
    docs, sections, task_links, person_links = [], [], [], []
    for path in paths:
        try:
            text = _raw_file(repo, branch, path)
        except (GitHubUnavailable, httpx.HTTPError) as exc:
            warnings.append(f"skipped {path}: {exc}")
            continue
        doc_key = f"{repo}:{path}"
        parts = split_markdown(text)
        title = next((p["heading"] for p in parts if p["heading"]), path.rsplit("/", 1)[-1])
        ids = []
        for position, part in enumerate(parts):
            section_id = _stable_id(doc_key, str(position), part["heading"])
            ids.append(section_id)
            anchor = f"#{_github_anchor(part['heading'])}" if part["heading"] else ""
            body = f"{part['heading']}\n{part['content']}"
            sections.append({"id": section_id, "doc_key": doc_key, "heading": part["heading"] or title,
                             "content": part["content"][:MAX_SECTION_CHARS], "position": position, "path": path,
                             "url": f"https://github.com/{repo}/blob/{branch}/{path}{anchor}"})
            task_links += [{"source": section_id, "target": t} for t in mentioned_tasks(body, tasks)]
            person_links += [{"source": section_id, "target": p} for p in mentioned_people(body, developers)]
        docs.append({"key": doc_key, "path": path, "title": title, "section_ids": ids,
                     "url": f"https://github.com/{repo}/blob/{branch}/{path}"})
    if docs:
        graph.query(DOC_UPSERT, {"repo": repo, "docs": docs, "now": _now()})
        graph.query(SECTION_UPSERT, {"sections": sections})
        graph.query("MATCH (doc:Document), (f:File {key: doc.key}) MERGE (doc)-[:STORED_AS]->(f)")
    _link(graph, "DocSection", task_links)
    _link(graph, "DocSectionPerson", person_links)

    link_people(graph)
    topics = refresh_expertise(graph)
    return {"repo": repo, "commits": len(rows), "issues": len(issue_rows), "documents": len(docs),
            "sections": len(sections), "topics": topics, "warnings": warnings}


def link_people(graph: Any) -> None:
    """Treat a GitHub account as a Developer when configured, or when the names match exactly."""
    try:
        mapping = json.loads(os.getenv("GITHUB_DEVELOPER_MAP") or "{}")
    except json.JSONDecodeError:
        mapping = {}
    for login, name in mapping.items():
        graph.query("MATCH (u:GitHubUser {login: $login}) MERGE (d:Developer {name: $name}) MERGE (u)-[:SAME_AS]->(d)",
                    {"login": login, "name": name})
    graph.query("""
    MATCH (u:GitHubUser), (d:Developer)
    WHERE NOT (u)-[:SAME_AS]->(:Developer)
      AND (toLower(u.login) = toLower(d.name) OR toLower(coalesce(u.name, '')) = toLower(d.name))
    MERGE (u)-[:SAME_AS]->(d)
    """)


# --- Who knows what -------------------------------------------------------------------------------

# Every branch returns the same columns: the person node, their display name, the kind of evidence,
# a label, a link, and a weight. GitHub and Discord identities resolve to their Developer when linked.
EVIDENCE_QUERY = """
MATCH (p:Developer)-[:ASSIGNED_TO]->(t:Task)
WHERE toLower(t.title) CONTAINS $term
RETURN id(p) AS pid, p.name AS person, 'owns task' AS kind, t.id + ': ' + t.title AS label, '' AS url,
       CASE WHEN t.status = 'done' THEN 4 ELSE 3 END AS weight
UNION ALL
MATCH (p:Developer)-[:MEMBER_OF]->(team:Team)
WHERE toLower(team.name) CONTAINS $term
RETURN id(p) AS pid, p.name AS person, 'on the team' AS kind, team.name AS label, '' AS url, 3 AS weight
UNION ALL
MATCH (u:GitHubUser)-[:AUTHORED]->(c:Commit)-[:TOUCHES]->(f:File)
WHERE toLower(f.path) CONTAINS $term AND NOT coalesce(c.is_merge, false)
  AND NOT c.message STARTS WITH 'Merge ' AND NOT u.login ENDS WITH '[bot]' AND NOT toLower(u.login) IN $bots
OPTIONAL MATCH (u)-[:SAME_AS]->(d:Developer)
RETURN id(coalesce(d, u)) AS pid, coalesce(d.name, CASE WHEN u.login STARTS WITH 'git:' THEN u.name ELSE '@' + u.login END) AS person, 'changed ' + f.path AS kind,
       c.message AS label, c.url AS url, 1 AS weight
UNION ALL
MATCH (u:GitHubUser)-[:AUTHORED]->(c:Commit)
WHERE toLower(c.message) CONTAINS $term AND NOT coalesce(c.is_merge, false)
  AND NOT c.message STARTS WITH 'Merge ' AND NOT u.login ENDS WITH '[bot]' AND NOT toLower(u.login) IN $bots
OPTIONAL MATCH (u)-[:SAME_AS]->(d:Developer)
RETURN id(coalesce(d, u)) AS pid, coalesce(d.name, CASE WHEN u.login STARTS WITH 'git:' THEN u.name ELSE '@' + u.login END) AS person, 'commit' AS kind,
       c.message AS label, c.url AS url, 1 AS weight
UNION ALL
MATCH (u:GitHubUser)-[r:OPENED|ASSIGNED]->(i:Issue)
WHERE (toLower(i.title) CONTAINS $term OR any(l IN i.labels WHERE toLower(l) CONTAINS $term))
  AND NOT u.login ENDS WITH '[bot]' AND NOT toLower(u.login) IN $bots
OPTIONAL MATCH (u)-[:SAME_AS]->(d:Developer)
RETURN id(coalesce(d, u)) AS pid, coalesce(d.name, CASE WHEN u.login STARTS WITH 'git:' THEN u.name ELSE '@' + u.login END) AS person,
       CASE WHEN type(r) = 'ASSIGNED' THEN 'assigned issue' ELSE 'opened issue' END AS kind,
       '#' + toString(i.number) + ' ' + i.title AS label, i.url AS url,
       CASE WHEN type(r) = 'ASSIGNED' THEN 3 ELSE 2 END AS weight
UNION ALL
MATCH (sec:DocSection)-[:MENTIONS]->(p:Developer)
WHERE toLower(sec.heading) CONTAINS $term OR toLower(sec.content) CONTAINS $term
RETURN id(p) AS pid, p.name AS person, 'named in docs' AS kind, sec.heading AS label, sec.url AS url, 2 AS weight
UNION ALL
MATCH (a:DiscordAuthor)-[:AUTHORED]->(m:DiscordMessage)
WHERE toLower(m.content) CONTAINS $term AND NOT m.content STARTS WITH '<@'
  AND NOT toLower(coalesce(a.discord_name, '')) IN $exclude
OPTIONAL MATCH (a)-[:MAPS_TO]->(d:Developer)
RETURN id(coalesce(d, a)) AS pid, coalesce(d.name, a.discord_name) AS person, 'said in chat' AS kind,
       left(m.content, 120) AS label, m.source_url AS url, 1 AS weight
""".strip()


def expertise(graph: Any, topic: str, *, exclude: list[str] | None = None, limit: int = 5) -> list[dict[str, Any]]:
    """People ranked by evidence that they know about `topic`, each with their strongest evidence."""
    phrase = re.sub(r"^(?:the|a|an|our|my|this|that)\s+", "", " ".join(topic.casefold().split()))
    words = search_terms(phrase)
    terms = [phrase] + [word for word in words if word != phrase] if phrase else words
    seen, people = set(), {}
    exclude_names = [name.casefold() for name in (exclude or [])]
    for term in terms:
        rows = graph.ro_query(EVIDENCE_QUERY, {"term": term, "exclude": exclude_names, "bots": BOT_LOGINS}).result_set
        for pid, person, kind, label, url, weight in rows:
            key = (pid, kind, label)
            if key in seen or not person:
                continue
            seen.add(key)
            entry = people.setdefault(pid, {"pid": pid, "person": person, "score": 0.0, "commit_score": 0.0,
                                            "evidence": []})
            is_commit = kind == "commit" or kind.startswith("changed ")
            if is_commit:
                if entry["commit_score"] >= COMMIT_EVIDENCE_CAP:
                    continue
                entry["commit_score"] += weight
            # The exact phrase counts fully; matches on single words of a longer phrase count half
            entry["score"] += weight if term == phrase else weight / 2
            entry["evidence"].append({"kind": kind, "label": label or "", "url": url or "", "weight": weight})
    ranked = sorted(people.values(), key=lambda p: p["score"], reverse=True)[:limit]
    for person in ranked:
        person["evidence"].sort(key=lambda e: e["weight"], reverse=True)
    return ranked


def candidate_topics(graph: Any, *, limit: int = 40) -> list[str]:
    """Topics worth tracking: task titles, issue labels, team names, and recurring code areas."""
    topics: list[str] = []

    def add(name: str) -> None:
        name = " ".join((name or "").casefold().split())
        if len(name) >= 3 and name not in STOPWORDS and name not in topics:
            topics.append(name)

    for (title,) in graph.ro_query("MATCH (t:Task) RETURN t.title").result_set:
        add(title)
    for (name,) in graph.ro_query("MATCH (team:Team) RETURN team.name").result_set:
        add(name)
    for (labels,) in graph.ro_query(
            "MATCH (u:GitHubUser)-[:OPENED]->(i:Issue) WHERE NOT u.login ENDS WITH '[bot]' "
            "AND NOT toLower(u.login) IN $bots RETURN i.labels", {"bots": BOT_LOGINS}).result_set:
        for label in labels or []:
            if re.fullmatch(r"[a-z][a-z0-9 _-]{2,30}", label.casefold()) and label.casefold() not in GENERIC_PATH_PARTS:
                add(label)
    counts: dict[str, int] = {}
    for (path, commits) in graph.ro_query(
            "MATCH (c:Commit)-[:TOUCHES]->(f:File) RETURN f.path, count(c)").result_set:
        for word in set(path_topics(path)):
            counts[word] = counts.get(word, 0) + commits
    for word, _ in sorted(counts.items(), key=lambda item: item[1], reverse=True):
        if counts[word] >= 2:
            add(word)
    return topics[:limit]


def refresh_expertise(graph: Any) -> int:
    """Rebuild (person)-[:EXPERT_IN {score, evidence}]->(:Topic) from the current evidence."""
    graph.query("MATCH ()-[e:EXPERT_IN]->() DELETE e")
    topics = candidate_topics(graph)
    for topic in topics:
        ranked = expertise(graph, topic, limit=3)
        if not ranked:
            continue
        graph.query("""
        MERGE (t:Topic {name: $topic})
        WITH t
        UNWIND $people AS p
        MATCH (person) WHERE id(person) = p.pid
        MERGE (person)-[e:EXPERT_IN]->(t)
        SET e.score = p.score, e.evidence = p.evidence
        """, {"topic": topic, "people": [
            {"pid": p["pid"], "score": round(p["score"], 2), "evidence": len(p["evidence"])} for p in ranked
        ]})
    graph.query("MATCH (t:Topic) WHERE NOT ()-[:EXPERT_IN]->(t) DELETE t")
    return len(topics)


def experts_overview(graph: Any, limit: int = 15) -> list[tuple[str, list[tuple[str, float]]]]:
    rows = graph.ro_query("""
    MATCH (p)-[e:EXPERT_IN]->(t:Topic)
    WITH t, CASE WHEN 'GitHubUser' IN labels(p) AND NOT p.login STARTS WITH 'git:' THEN '@' + p.login ELSE coalesce(p.name, p.discord_name) END AS person,
         e.score AS score
    ORDER BY score DESC
    WITH t, collect([person, score]) AS people, max(score) AS best
    RETURN t.name, people[..3], best
    ORDER BY best DESC
    LIMIT $limit
    """, {"limit": limit}).result_set
    return [(name, [(p[0], p[1]) for p in people]) for name, people, _ in rows]


# --- Teams ----------------------------------------------------------------------------------------

def teams(graph: Any) -> list[tuple[str, list[str]]]:
    rows = graph.ro_query("""
    MATCH (team:Team)
    OPTIONAL MATCH (d:Developer)-[:MEMBER_OF]->(team)
    RETURN team.name, collect(d.name) ORDER BY team.name
    """).result_set
    return [(name, sorted(members)) for name, members in rows]


# --- Context for answers --------------------------------------------------------------------------

def knowledge_context(graph: Any, question: str, *, per_kind: int = 4) -> str:
    """Docs, commits and issues related to the question, with links, for the answering model."""
    search = fulltext_query(question)
    lines: list[str] = []
    if search:
        queries = (
            ("doc", "DocSection", "RETURN node.heading, left(node.content, 400), node.url, node.path"),
            ("commit", "Commit", "RETURN node.message, '', node.url, ''"),
            ("issue", "Issue", "RETURN '#' + toString(node.number) + ' ' + node.title, node.state, node.url, ''"),
        )
        for kind, label, projection in queries:
            try:
                rows = graph.ro_query(
                    f"CALL db.idx.fulltext.queryNodes('{label}', $q) YIELD node, score "
                    f"WITH node, score ORDER BY score DESC LIMIT {per_kind} {projection}", {"q": search}).result_set
            except Exception:
                continue  # index not created yet for this server
            for title, detail, url, path in rows:
                text = " ".join(str(detail or "").split())
                lines.append(f"- [{kind}] {title}{f' ({path})' if path else ''}: {text} (link: {url})".strip())
    team_rows = teams(graph)
    if team_rows:
        lines.append("Teams: " + "; ".join(f"{name}: {', '.join(members) or 'no members'}" for name, members in team_rows))
    return "\n".join(lines) or "(no related docs, commits, issues or teams)"
