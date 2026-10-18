"""GitHub repository analysis for SigmaAgent: contributors, commits, issues, and issue summaries.

Answers repository questions asked in Discord (`@sigmaagent who committed how
much?`, `@sigmaagent what bugs are open?`, `@sigmaagent summarize issue #6`)
from the live GitHub REST API, so the data always matches the repository, and
links every answer back to its GitHub source. Configure with `GITHUB_REPO`
(default `Sumit884-byte/sigmaAgent`) and optionally `GITHUB_TOKEN` for a
higher API rate limit.
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime
from typing import Any

import httpx

GITHUB_API = "https://api.github.com"
DEFAULT_REPO = os.getenv("GITHUB_REPO", "Sumit884-byte/sigmaAgent")
CACHE_TTL_SECONDS = 120
SAFE_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9_])?$")
REPO_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}$")

COMMON_NON_REPOS = {
    "frontend/backend", "backend/frontend", "client/server", "server/client",
    "ci/cd", "cd/ci", "input/output", "read/write", "and/or", "true/false",
    "before/after", "left/right", "top/bottom", "up/down", "yes/no",
    "platform/web", "web/platform",
}


def is_safe_repo(repo: str) -> bool:
    """Validate that owner/name is well-formed, exists as a repo reference, and avoids traversal like '..'."""
    if not repo or "/" not in repo:
        return False
    parts = repo.split("/", 1)
    if len(parts) != 2:
        return False
    owner, name = parts[0].strip(), parts[1].strip()
    if not owner or not name:
        return False
    # Strictly reject path traversal or hidden dot-files
    if ".." in owner or ".." in name:
        return False
    if owner.startswith(".") or name.startswith("."):
        return False
    if f"{owner.lower()}/{name.lower()}" in COMMON_NON_REPOS:
        return False
    return bool(SAFE_OWNER_RE.fullmatch(owner) and SAFE_NAME_RE.fullmatch(name))


ISSUE_NUMBER = re.compile(r"\bissue\s*#?(\d{1,7})\b", re.IGNORECASE)
# github.com/owner/repo inside a pasted URL; extra path segments are ignored
GITHUB_URL_REPO = re.compile(
    r"github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9_][A-Za-z0-9_.-]{0,99})",
    re.IGNORECASE,
)
# github.com/owner/repo/issues/42 or /pull/42 in a pasted URL
URL_ISSUE_NUMBER = re.compile(
    r"github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(?:issues|pull)/(\d{1,7})", re.IGNORECASE
)
ASSIGNEE_RE = re.compile(r"\bassigned to @?([A-Za-z0-9][A-Za-z0-9-]{0,38})\b", re.IGNORECASE)
AUTHOR_RE = re.compile(r"\b(?:opened|created|filed|wrote|written) by @?([A-Za-z0-9][A-Za-z0-9-]{0,38})\b",
                       re.IGNORECASE)
# Only accept double quotes or backticks to avoid apostrophes ('s) becoming fake label filters
QUOTED_LABEL_RE = re.compile(r'["`]([A-Za-z0-9 .+/_-]{2,40})["`]')
# Match whole words with \b to avoid "debug" -> "bug", "docstring" -> "docs", "featured" -> "feature"
LABEL_PATTERNS = {
    "good first issue": [
        re.compile(r"\bgood\s+first\s+issues?\b", re.IGNORECASE),
        re.compile(r"\bbeginner\s+friendly\b", re.IGNORECASE),
    ],
    "help wanted": [re.compile(r"\bhelp\s+wanted\b", re.IGNORECASE)],
    "bug": [re.compile(r"\bbugs?\b", re.IGNORECASE)],
    "enhancement": [
        re.compile(r"\benhancements?\b", re.IGNORECASE),
        re.compile(r"\bfeature\s+requests?\b", re.IGNORECASE),
        re.compile(r"\bfeatures?\b", re.IGNORECASE),
    ],
    "documentation": [
        re.compile(r"\bdocumentation\b", re.IGNORECASE),
        re.compile(r"\bdocs?\b", re.IGNORECASE),
    ],
}
MAX_CACHE_ENTRIES = 200
_CACHE: dict[str, tuple[float, Any]] = {}


class GitHubUnavailable(RuntimeError):
    """GitHub could not be reached or refused the request (network, rate limit, HTTP error)."""


class GitHubNotFound(LookupError):
    """The repository or issue does not exist."""


def _http_get_json(path: str, params: dict[str, str] | None = None) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "SigmaAgent (https://github.com/Sumit884-byte/sigmaAgent)",
    }
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(base_url=GITHUB_API, headers=headers, timeout=10) as http:
            response = http.get(path, params=params)
    except httpx.HTTPError as exc:
        raise GitHubUnavailable(f"could not reach api.github.com ({exc.__class__.__name__})") from exc
    if response.status_code == 404:
        raise GitHubNotFound(path)
    if response.status_code in (403, 429):
        if "rate limit" in response.text.casefold() or response.headers.get("x-ratelimit-remaining") == "0":
            raise GitHubUnavailable(
                "the GitHub API rate limit for this machine is used up; "
                "set GITHUB_TOKEN in the backend .env for a higher limit"
            )
        raise GitHubUnavailable("GitHub denied access; if the repository is private, set GITHUB_TOKEN")
    if response.status_code >= 400:
        raise GitHubUnavailable(f"GitHub API returned HTTP {response.status_code}")
    return response.json()


def gh_get(path: str, params: dict[str, str] | None = None) -> Any:
    key = f"{path}?{sorted((params or {}).items())}"
    now = time.monotonic()
    cached = _CACHE.get(key)
    if cached and now - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    # Clean up expired entries if cache is growing
    if len(_CACHE) >= MAX_CACHE_ENTRIES:
        expired = [k for k, (ts, _) in _CACHE.items() if now - ts >= CACHE_TTL_SECONDS]
        for k in expired:
            _CACHE.pop(k, None)
        if len(_CACHE) >= MAX_CACHE_ENTRIES:
            oldest = sorted(_CACHE.keys(), key=lambda k: _CACHE[k][0])[:MAX_CACHE_ENTRIES // 5]
            for k in oldest:
                _CACHE.pop(k, None)

    value = _http_get_json(path, params)
    _CACHE[key] = (now, value)
    return value


def extract_repo(question: str) -> str | None:
    """The repository named in the question: a github.com URL or an explicit repo reference."""
    match = GITHUB_URL_REPO.search(question or "")
    if match:
        candidate = f"{match.group(1)}/{match.group(2).rstrip('.,;:!?)]}')}"
        if is_safe_repo(candidate):
            return candidate

    # Look for explicit repo: repo: owner/repo, repository: owner/repo
    explicit_match = re.search(
        r"(?:repo|repository)\s*[:=]?\s*([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9_][A-Za-z0-9_.-]{0,99})\b",
        question or "",
        re.IGNORECASE,
    )
    if explicit_match:
        candidate = f"{explicit_match.group(1)}/{explicit_match.group(2).rstrip('.,;:!?)]}')}"
        if is_safe_repo(candidate):
            return candidate

    # Match in/for/to owner/repo only when the question has clear GitHub query intent
    # (e.g. issues, bugs, PRs, commits, contributors, compare) so that plain task messages like
    # "assign to Alice for platform/web" or "explain platform/web" are never misread as repos
    in_match = re.search(
        r"(?:in|for|to)\s+([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9_][A-Za-z0-9_.-]{0,99})\b",
        question or "",
        re.IGNORECASE,
    )
    if in_match and (
        GITHUB_QUERY_INTENT.search(question or "")
        or bool(re.search(r"\b(?:compare|showdown|competition|vs\.?|versus|contributors?|commits?|issues?|bugs?|prs?)\b", question or "", re.IGNORECASE))
    ):
        candidate = f"{in_match.group(1)}/{in_match.group(2).rstrip('.,;:!?)]}')}"
        if is_safe_repo(candidate):
            return candidate

    return None


def _issue_number(question: str) -> int | None:
    """An issue number from a pasted issue URL (github.com/.../issues/42) or 'issue #42'."""
    match = URL_ISSUE_NUMBER.search(question or "") or ISSUE_NUMBER.search(question or "")
    return int(match.group(1)) if match else None


def _repo_path(repo: str, suffix: str = "") -> str:
    if not is_safe_repo(repo):
        raise GitHubNotFound(f"invalid repository reference {repo!r} (expected owner/name)")
    if ".." in suffix:
        raise GitHubNotFound(f"invalid path suffix {suffix!r}")
    return f"/repos/{repo}{suffix}"


def _issue_labels(item: dict[str, Any]) -> list[str]:
    return [
        str(label.get("name"))
        for label in item.get("labels", [])
        if isinstance(label, dict) and label.get("name")
    ]


def get_contributors(repo: str = DEFAULT_REPO) -> list[dict[str, Any]]:
    """Contributors with commit counts, busiest first (GitHub counts each as contributions)."""
    contributors = gh_get(_repo_path(repo, "/contributors"), {"per_page": "100"})
    contributors = [item for item in contributors if isinstance(item, dict)]
    contributors.sort(key=lambda item: item.get("contributions", 0), reverse=True)
    return [
        {
            "login": item.get("login") or "unknown",
            "commits": int(item.get("contributions") or 0),
            "profile_url": item.get("html_url") or "",
        }
        for item in contributors
    ]


def get_issues(repo: str = DEFAULT_REPO, state: str = "open",
               labels: list[str] | None = None,
               author: str | None = None, assignee: str | None = None,
               unassigned: bool = False) -> list[dict[str, Any]]:
    """Issues (pull requests filtered out), newest activity first.

    Fetches up to 100 issues matching server-side filters; answers note that it is
    a recent window.
    """
    if state not in {"open", "closed", "all"}:
        state = "open"
    params: dict[str, str] = {"state": state, "per_page": "100", "sort": "updated", "direction": "desc"}
    if labels:
        params["labels"] = ",".join(labels)
    if author:
        params["creator"] = author.lstrip("@")
    if assignee:
        params["assignee"] = assignee.lstrip("@")

    items = gh_get(_repo_path(repo, "/issues"), params)
    wanted = {label.casefold() for label in labels} if labels else None
    author_id = (author or "").casefold().lstrip("@")
    assignee_id = (assignee or "").casefold().lstrip("@")
    issues = []
    for item in items:
        if not isinstance(item, dict) or "pull_request" in item:
            continue
        item_author = (item.get("user") or {}).get("login") or ""
        item_assignees = [
            (entry or {}).get("login") or ""
            for entry in item.get("assignees", [])
            if isinstance(entry, dict)
        ]
        if unassigned and item_assignees:
            continue
        if author_id and item_author.casefold() != author_id:
            continue
        if assignee_id and assignee_id not in {name.casefold() for name in item_assignees}:
            continue
        label_names = _issue_labels(item)
        if wanted and not (wanted & {name.casefold() for name in label_names}):
            continue
        issues.append({
            "number": item.get("number"),
            "title": item.get("title") or "",
            "state": item.get("state") or "open",
            "author": item_author or "unknown",
            "author_url": (item.get("user") or {}).get("html_url") or "",
            "assignees": [name for name in item_assignees if name],
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
            "labels": label_names,
            "comments": int(item.get("comments") or 0),
            "url": item.get("html_url") or "",
        })
    return issues


def get_issue(repo: str, number: int) -> dict[str, Any]:
    """One issue with its body and latest comments, for summarization."""
    issue = gh_get(_repo_path(repo, f"/issues/{int(number)}"))
    if not isinstance(issue, dict):
        raise GitHubNotFound(f"issue {number}")
    comments_count = int(issue.get("comments") or 0)
    comments = []
    if comments_count > 0:
        # GitHub issue comments are ordered chronologically (page 1 has oldest).
        # Fetch the last page (up to 10 comments) to get the latest discussion.
        page = max(1, (comments_count + 9) // 10)
        comments_data = gh_get(_repo_path(repo, f"/issues/{number}/comments"), {"per_page": "10", "page": str(page)})
        if isinstance(comments_data, list) and comments_data:
            comments = comments_data
        elif page > 1:
            fallback = gh_get(_repo_path(repo, f"/issues/{number}/comments"), {"per_page": "10"})
            if isinstance(fallback, list):
                comments = fallback

    return {
        "number": issue.get("number") or int(number),
        "title": issue.get("title") or "",
        "state": issue.get("state") or "open",
        "is_pull_request": "pull_request" in issue,
        "author": (issue.get("user") or {}).get("login") or "unknown",
        "created_at": issue.get("created_at"),
        "updated_at": issue.get("updated_at"),
        "labels": _issue_labels(issue),
        "comments_count": comments_count,
        "body": issue.get("body") or "",
        "url": issue.get("html_url") or "",
        "comments": [
            {
                "author": (comment.get("user") or {}).get("login") or "unknown",
                "created_at": comment.get("created_at"),
                "body": comment.get("body") or "",
                "url": comment.get("html_url") or "",
            }
            for comment in comments if isinstance(comment, dict)
        ],
    }


def _month_day(iso: Any) -> str:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).strftime("%b %d").replace(" 0", " ")
    except (TypeError, ValueError):
        return "an unknown date"


def _clean_text(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def _repo_url(repo: str) -> str:
    return f"https://github.com/{repo}"


def format_contributors(repo: str, contributors: list[dict[str, Any]]) -> str:
    total = sum(contributor["commits"] for contributor in contributors)
    count = len(contributors)
    if count >= 100:
        lines = [f"**{repo}** top **{count}** contributors have recorded at least **{total:,}** commits:"]
    else:
        lines = [f"**{repo}** has **{total:,}** commits by **{count}** contributor(s):"]
    for contributor in contributors[:10]:
        share = f" ({round(contributor['commits'] * 100 / total)}%)" if total else ""
        lines.append(f"- **{contributor['login']}** — {contributor['commits']:,} commits{share}")
    if count > 10:
        lines.append(f"...and {count - 10} more in this top {count} list ([all contributors]({_repo_url(repo)}/graphs/contributors))")
    else:
        lines.append(f"[Repository]({_repo_url(repo)})")
    return "\n".join(lines)


def format_issues(repo: str, issues: list[dict[str, Any]], *, kind: str) -> str:
    if not issues:
        return f"There are no {kind} in **{repo}** right now."
    total_found = len(issues)
    lines = [f"**{kind.capitalize()} in {repo}** ({total_found} recent):"]
    for issue in issues[:10]:
        labels = f" [{', '.join(issue['labels'])}]" if issue["labels"] else ""
        lines.append(
            f"- #{issue['number']} **{issue['title']}**{labels} — opened by **{issue['author']}** "
            f"on {_month_day(issue['created_at'])}, {issue['comments']} comments "
            f"([view]({issue['url']}))"
        )
    if total_found > 10:
        lines.append(f"...and {total_found - 10} more in this recent window ([all {kind}]({_repo_url(repo)}/issues))")
    return "\n".join(lines)


def format_issue_summary(repo: str, issue: dict[str, Any]) -> str:
    kind = "pull request" if issue.get("is_pull_request") else "issue"
    lines = [
        f"**{kind.capitalize()} #{issue['number']}: {issue['title']}** — {issue['state']}",
        f"Opened by **{issue['author']}** on {_month_day(issue['created_at'])}; "
        f"{issue['comments_count']} comments; last activity {_month_day(issue['updated_at'])}.",
    ]
    if issue["labels"]:
        lines.append(f"Labels: {', '.join(f'`{label}`' for label in issue['labels'])}")
    if issue["body"].strip():
        lines.append(f"> {_clean_text(issue['body'], 500)}")
    if issue["comments"]:
        lines.append("Latest discussion:")
        for comment in issue["comments"][-3:]:
            lines.append(
                f"- **{comment['author']}** ({_month_day(comment['created_at'])}): "
                f"{_clean_text(comment['body'], 200)} ([link]({comment['url']}))"
            )
    lines.append(f"[View on GitHub]({issue['url']})")
    return "\n".join(lines)


GITHUB_HELP_TEXT = (
    "I can answer GitHub questions about **{repo}**:\n"
    "- `who are the contributors?` / `who committed how much?`\n"
    "- `what are the open issues?` / `what bugs are open?` / `unassigned issues`\n"
    "- `issues labeled \"good first issue\"`\n"
    "- `who opened the most issues?` / `who has the most assigned issues?`\n"
    "- `compare alice and bob` (track friend competition for open-source contributions)\n"
    "- `summarize issue #6`\n"
    "Set GITHUB_REPO to analyze a different repository."
)


def extract_labels(question: str) -> list[str]:
    """Labels to filter by: double-quoted/backticked phrases (`labeled "good first issue"`) or bare keywords."""
    labels: list[str] = []
    for name in QUOTED_LABEL_RE.findall(question or ""):
        name = name.strip()
        if name.casefold() not in {label.casefold() for label in labels}:
            labels.append(name)
    for canonical, patterns in LABEL_PATTERNS.items():
        if any(pattern.search(question or "") for pattern in patterns):
            if canonical.casefold() not in {label.casefold() for label in labels}:
                labels.append(canonical)
    return labels


def _issues_kind(state: str, labels: list[str], author: str | None, assignee: str | None,
                 unassigned: bool = False) -> str:
    prefix = f"{state} unassigned" if unassigned else state
    if [label.casefold() for label in labels] == ["bug"]:
        kind = f"{prefix} bugs (labeled `bug`)"
    else:
        kind = f"{prefix} issues"
        if labels:
            kind += f" labeled {', '.join(f'`{label}`' for label in labels)}"
    if assignee:
        kind += f" assigned to **{assignee}**"
    if author:
        kind += f" opened by **{author}**"
    return kind


def format_issue_leaderboard(repo: str, issues: list[dict[str, Any]], *, metric: str = "author") -> str:
    """Who opened or is assigned the most issues over the fetched window."""
    counts: dict[str, int] = {}
    if metric == "assignee":
        for issue in issues:
            for assignee in issue.get("assignees", []):
                counts[assignee] = counts.get(assignee, 0) + 1
        title = f"**Top issue assignees in {repo}**"
        action = "assigned issue(s)"
    else:
        for issue in issues:
            counts[issue["author"]] = counts.get(issue["author"], 0) + 1
        title = f"**Issue authors in {repo}**"
        action = "issue(s)"

    if not counts:
        return f"There are no matching issues with {metric} data in **{repo}** to rank yet."

    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    lines = [f"{title} (top {min(len(ranked), 10)} of {len(ranked)}):"]
    for login, count in ranked[:10]:
        lines.append(f"- **{login}** — {count} {action}")
    lines.append(
        f"Counted from the {len(issues)} issues in the fetched window, not the repo's full "
        f"history ([all issues]({_repo_url(repo)}/issues))"
    )
    return "\n".join(lines)


COMPARE_STOP_WORDS = {
    "the", "this", "that", "these", "those", "repo", "repository", "github",
    "issue", "issues", "bug", "bugs", "commit", "commits", "contributor", "contributors",
    "open", "closed", "all", "me", "who", "what", "which", "how", "for", "in", "on", "at",
    "with", "between", "and", "vs", "versus", "compare", "competition", "showdown", "track",
    "project", "friends", "collaboration", "competing", "winning", "leaderboard", "rank",
    "progress", "status", "task", "tasks", "feature", "features", "work", "update", "updates",
}


def extract_compare_users(text: str) -> list[str]:
    """Extract usernames when the user asks to compare/compete between contributors."""
    lower = (text or "").casefold()
    if not re.search(r"\b(?:compare|competition|compete|competing|showdown|versus|vs\.?|winning|between|track)\b", lower):
        return []

    def _valid_username(u: str) -> bool:
        u_clean = u.lstrip("@").strip()
        if not u_clean or u_clean.casefold() in COMPARE_STOP_WORDS:
            return False
        # Task IDs like T1, T2 and Jira-style ENG-123, AB2-12 (all-uppercase prefix) are NOT usernames
        if re.fullmatch(r"(?:[A-Z][A-Z0-9]*-\d+|[Tt]\d+)", u_clean):
            return False
        return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,38}", u_clean))

    # Check for "between X and Y (and Z)"
    m = re.search(
        r"\bbetween\s+@?([A-Za-z0-9_-]+)\s+(?:and|vs\.?)\s+@?([A-Za-z0-9_-]+)(?:\s+(?:and|vs\.?)\s+@?([A-Za-z0-9_-]+))?",
        text, re.IGNORECASE,
    )
    if m:
        users = [u for u in m.groups() if u and _valid_username(u)]
        if len(users) >= 2:
            return users

    # Check for "compare/track/showdown X and/to/with/vs Y (and Z)"
    m = re.search(
        r"\b(?:compare|showdown|track)\s+@?([A-Za-z0-9_-]+)\s+(?:and|with|to|vs\.?)\s+@?([A-Za-z0-9_-]+)(?:\s+(?:and|vs\.?)\s+@?([A-Za-z0-9_-]+))?",
        text, re.IGNORECASE,
    )
    if m:
        users = [u for u in m.groups() if u and _valid_username(u)]
        if len(users) >= 2:
            return users

    # Check for "X vs Y"
    m = re.search(r"\b@?([A-Za-z0-9_-]+)\s+vs\.?\s+@?([A-Za-z0-9_-]+)\b", text, re.IGNORECASE)
    if m:
        users = [u for u in m.groups() if u and _valid_username(u)]
        if len(users) >= 2:
            return users

    # Check for @mentions in a comparison context
    mentions = re.findall(r"@([A-Za-z0-9_-]+)", text)
    valid_mentions = [m for m in mentions if _valid_username(m)]
    if len(valid_mentions) >= 2:
        return valid_mentions

    return []


def format_friend_competition(
    repo: str,
    users: list[str],
    contributors: list[dict[str, Any]],
    issues: list[dict[str, Any]],
) -> str:
    """Compare multiple friends/contributors across commits, assigned issues, and opened issues."""
    user_logins = []
    seen = set()
    for u in users:
        cleaned = u.lstrip("@").strip()
        if cleaned and cleaned.casefold() not in seen:
            seen.add(cleaned.casefold())
            user_logins.append(cleaned)

    if len(user_logins) < 2:
        return f"Provide at least two GitHub usernames to compare in **{repo}** (e.g. `compare alice and bob`)."

    contrib_map = {c["login"].casefold(): c for c in contributors}
    stats = {}
    for user in user_logins:
        u_fold = user.casefold()
        c_entry = contrib_map.get(u_fold)
        commits = c_entry["commits"] if c_entry else 0
        c_rank = None
        if c_entry:
            for rank, c in enumerate(contributors, 1):
                if c["login"].casefold() == u_fold:
                    c_rank = rank
                    break

        opened_open = 0
        opened_closed = 0
        assigned_open = 0
        assigned_closed = 0

        for issue in issues:
            if issue.get("author", "").casefold() == u_fold:
                if issue.get("state") == "closed":
                    opened_closed += 1
                else:
                    opened_open += 1
            if any(a.casefold() == u_fold for a in issue.get("assignees", [])):
                if issue.get("state") == "closed":
                    assigned_closed += 1
                else:
                    assigned_open += 1

        total_assigned = assigned_open + assigned_closed
        total_opened = opened_open + opened_closed
        score = (commits * 3) + (assigned_closed * 2) + assigned_open + total_opened
        stats[user] = {
            "login": user,
            "commits": commits,
            "rank": c_rank,
            "assigned_open": assigned_open,
            "assigned_closed": assigned_closed,
            "total_assigned": total_assigned,
            "opened_open": opened_open,
            "opened_closed": opened_closed,
            "total_opened": total_opened,
            "score": score,
        }

    lines = [f"**Open-Source Showdown in {repo}** ({len(user_logins)} friends tracked):"]
    sorted_users = sorted(stats.values(), key=lambda s: (-s["score"], -s["commits"], -s["total_assigned"]))
    for s in sorted_users:
        if s["rank"]:
            if len(contributors) >= 100:
                rank_str = f" (#{s['rank']} in top {len(contributors)})"
            else:
                rank_str = f" (#{s['rank']} overall)"
        else:
            if len(contributors) >= 100:
                rank_str = f" (outside top {len(contributors)})"
            else:
                rank_str = ""
        lines.append(
            f"- **{s['login']}** (score: {s['score']}):\n"
            f"  - Commits: **{s['commits']:,}**{rank_str}\n"
            f"  - Assigned Issues: **{s['total_assigned']}** ({s['assigned_closed']} completed, {s['assigned_open']} in progress)\n"
            f"  - Issues Opened: **{s['total_opened']}** ({s['opened_closed']} closed, {s['opened_open']} open)\n"
            f"  - Profile: https://github.com/{s['login']}"
        )

    top = sorted_users[0]
    if len(sorted_users) > 1 and top["score"] > sorted_users[1]["score"]:
        lines.append(
            f"\n**Leader**: **{top['login']}** leads the competition with **{top['score']}** points "
            f"({top['commits']:,} commits, {top['total_assigned']} assigned issues)!"
        )
    elif len(sorted_users) > 1 and top["score"] == sorted_users[1]["score"] and top["score"] > 0:
        lines.append(f"\n**Tie**: It's a tie between **{top['login']}** and **{sorted_users[1]['login']}** with **{top['score']}** points!")

    lines.append(
        f"\nStats based on {repo}'s top contributors and the 100 most recently updated issues "
        f"([issues]({_repo_url(repo)}/issues))."
    )
    return "\n".join(lines)


TASK_COMMAND_PATTERN = re.compile(
    r"^\s*(?:create\s+task|add\s+task|new\s+task|assign\b|unassign\b|rename\s+task|delete\s+task|set\s+status|explain\b|mark\s+task)\b|"
    r"\bas\s+a\s+(?:new\s+)?task\b|"
    r"\bfor\s+a\s+(?:new\s+)?task\b|"
    r"\b(?:[A-Za-z][A-Za-z0-9]*-\d+|T\d+)\s+(?:is\s+blocked\s+by|is\s+unblocked|depends\s+on|now\s+depends|no\s+longer\s+depends|is\s+done|is\s+in\s+progress|is\s+assigned)\b|"
    r"\b(?:blocked\s+by|depends\s+on|waiting\s+on)\s+(?:[A-Za-z][A-Za-z0-9]*-\d+|T\d+)\b|"
    r"\bwho\s+should\s+own\b",
    re.IGNORECASE,
)
EXPLICIT_GITHUB_TRIGGER = re.compile(
    r"(?:https?://github\.com/\S+|\bgithub\b|\bgh\b|\b(?:in|on|for|from)\s+github\b|\brepo:\s*[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+|\b(?:in|for|to)\s+repo\s+[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)
GITHUB_QUERY_INTENT = re.compile(
    r"\b(?:who\s+are\s+the\s+contributors|who\s+committed|who\s+contributed|how\s+many\s+(?:of\s+the\s+people\s+have\s+)?committ?ed|contributor\s+list)\b|"
    r"\b(?:what|show|list|find|get)\s+(?:are\s+the\s+)?(?:all\s+)?(?:open\s+|closed\s+|unassigned\s+)?(?:issues|bugs|tickets)\b|"
    r"\b(?:what\s+bugs\s+are\s+open|open\s+bugs\b|closed\s+issues\b|unassigned\s+issues\b|unclaimed\s+issues\b)"
    r"|\b(?:issues|bugs)\s+labeled\b"
    r"|\bissues\s+assigned\s+to\b"
    r"|\bissues\s+(?:were\s+)?opened\s+by\b"
    r"|\bwho\s+opened\s+the\s+most\s+issues\b"
    r"|\bwho\s+has\s+the\s+most\s+assigned\s+issues\b"
    r"|\b(?:summarize|summary|details\s+of)\s+issue\b"
    r"|\bissue\s+#\d+\b",
    re.IGNORECASE,
)


def answer_github_question(question: str, repo: str | None = None) -> str | None:
    """Answer a repository question from live GitHub data, or None if it isn't one."""
    if not question:
        return None

    # 1. Never claim task mutation commands or task statements
    if TASK_COMMAND_PATTERN.search(question):
        return None

    extracted_repo = extract_repo(question)
    has_explicit_trigger = bool(
        extracted_repo
        or EXPLICIT_GITHUB_TRIGGER.search(question)
        or re.search(r"\b(?:to|in)\s+the\s+repo\b", question, re.IGNORECASE)
    )

    # 2. If repo was not explicitly passed in by the caller, require an explicit trigger or recognized GitHub intent
    if repo is None and not has_explicit_trigger:
        if not GITHUB_QUERY_INTENT.search(question):
            return None

    target_repo = extracted_repo or repo or DEFAULT_REPO
    if not is_safe_repo(target_repo):
        return None

    try:
        number = _issue_number(question)
        if number:
            return format_issue_summary(target_repo, get_issue(target_repo, number))

        # Separate question prose from URLs to prevent '/issues' in URLs from overriding intent
        prose = re.sub(r"https?://\S+", "", question or "").strip()
        prose_lowered = prose.casefold()

        # Check for friend competition / multi-user comparison
        compare_users = extract_compare_users(prose or question)
        if len(compare_users) >= 2:
            if has_explicit_trigger or repo is not None:
                contributors = get_contributors(target_repo)
                issues = get_issues(target_repo, state="all")
                return format_friend_competition(target_repo, compare_users, contributors, issues)
            return None

        wants_commits = bool(
            re.search(
                r"\b(?:commit|commits|committed|commited|committer|committers|contributor|contributors|contributed|contribution|contributions)\b",
                prose_lowered,
            )
        )
        wants_issues_in_prose = bool(
            re.search(r"\b(?:issue|issues|bug|bugs|ticket|tickets|pr|prs|pull request|leaderboard)\b", prose_lowered)
        )

        # If user explicitly asks about commits/contributors in prose, prioritize commits!
        if wants_commits and not wants_issues_in_prose:
            return format_contributors(target_repo, get_contributors(target_repo))

        url_is_issues = bool(re.search(r"github\.com/[^/]+/[^/]+/issues\b", question or "", re.IGNORECASE))
        wants_issues = wants_issues_in_prose or "leaderboard" in prose_lowered or (not prose and url_is_issues)
        if wants_issues:
            labels = extract_labels(question)
            author_match, assignee_match = AUTHOR_RE.search(question), ASSIGNEE_RE.search(question)
            author = author_match.group(1) if author_match else None
            assignee = assignee_match.group(1) if assignee_match else None
            if author == "me" or assignee == "me":
                return ("I can't tell which GitHub account is yours from Discord; "
                        "ask with a GitHub username, e.g. `issues assigned to octocat`.")

            unassigned = bool(
                re.search(r"\b(?:unassigned|unclaimed|available|no assignee|without assignee)\b", prose_lowered)
            )

            # "all the open bugs" -> state="open", not "all"
            if re.search(r"\bclosed\b", prose_lowered):
                state = "closed"
            elif re.search(r"\ball\s+(?:the\s+)?open\b", prose_lowered):
                state = "open"
            elif re.search(r"\ball\b", prose_lowered):
                state = "all"
            else:
                state = "open"

            if "summar" in prose_lowered and not (labels or author or assignee or unassigned):
                issues = get_issues(target_repo, state=state)
                if not issues:
                    return f"There are no {state} issues in **{target_repo}** to summarize."
                return format_issue_summary(target_repo, get_issue(target_repo, issues[0]["number"]))

            # Leaderboard requires explicit leaderboard / ranking intent
            wants_leaderboard = "leaderboard" in prose_lowered or bool(
                re.search(
                    r"\b(?:who\s+opened\s+the\s+most|who\s+has\s+the\s+most|most\s+issues\s+by|top\s+issue\s+authors?|author\s+leaderboard|assignee\s+leaderboard|top\s+assignees?)\b",
                    prose_lowered,
                )
            )
            if wants_leaderboard:
                lb_state = state if "closed" in prose_lowered or bool(re.search(r"\bopen\b", prose_lowered)) else "all"
                issues = get_issues(
                    target_repo, state=lb_state, labels=labels, author=author, assignee=assignee, unassigned=unassigned
                )
                if not issues:
                    return f"There are no matching issues in **{target_repo}** to rank yet."
                metric = "assignee" if any(w in prose_lowered for w in ("assignee", "assigned", "working", "work")) else "author"
                return format_issue_leaderboard(target_repo, issues, metric=metric)

            return format_issues(
                target_repo,
                get_issues(target_repo, state=state, labels=labels, author=author, assignee=assignee, unassigned=unassigned),
                kind=_issues_kind(state, labels, author, assignee, unassigned=unassigned),
            )

        if wants_commits or "commit" in prose_lowered or "contribut" in prose_lowered:
            return format_contributors(target_repo, get_contributors(target_repo))

        if has_explicit_trigger:
            return GITHUB_HELP_TEXT.format(repo=target_repo)

        return None
    except GitHubNotFound:
        return f"I couldn't find that on GitHub for **{target_repo}**. Check the repository or issue number."
    except GitHubUnavailable as exc:
        return f"I couldn't reach GitHub just now: {exc}. Nothing was changed; try again in a minute."
