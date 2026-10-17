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
REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
ISSUE_NUMBER = re.compile(r"\bissue\s*#?(\d{1,7})\b", re.IGNORECASE)
# github.com/owner/repo inside a pasted URL; extra path segments are ignored
GITHUB_URL_REPO = re.compile(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", re.IGNORECASE)
# github.com/owner/repo/issues/42 or /pull/42 in a pasted URL
URL_ISSUE_NUMBER = re.compile(
    r"github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(?:issues|pull)/(\d{1,7})", re.IGNORECASE
)
# Bare owner/name ("barchart/marketdata-api-js") — both parts must start with a
# letter so dates (24/7) and fractions don't look like repositories
BARE_REPO = re.compile(
    r"(?<![\w./@])([A-Za-z][A-Za-z0-9_.-]{0,58})/([A-Za-z][A-Za-z0-9_.-]{0,98})(?![\w-])"
)
# Substring stems so one entry covers commits/committed/committers, issue/issues, etc.
HINT_STEMS = ("github", "repo", "repositor", "commit", "contribut", "issue", "bug")

# Short-lived cache: repeated mentions shouldn't burn the GitHub rate limit.
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
    value = _http_get_json(path, params)
    _CACHE[key] = (now, value)
    return value


def extract_repo(question: str) -> str | None:
    """The repository named in the question: a github.com URL or a bare owner/name."""
    match = GITHUB_URL_REPO.search(question or "")
    if match:
        owner, name = match.group(1), match.group(2)
    else:
        match = BARE_REPO.search(question or "")
        if not match:
            return None
        owner, name = match.group(1), match.group(2)
    return f"{owner}/{name.rstrip('.,;:!?)]}')}"


def _issue_number(question: str) -> int | None:
    """An issue number from a pasted issue URL (github.com/.../issues/42) or 'issue #42'."""
    match = URL_ISSUE_NUMBER.search(question or "") or ISSUE_NUMBER.search(question or "")
    return int(match.group(1)) if match else None


def _repo_path(repo: str, suffix: str = "") -> str:
    if not REPO_PATTERN.fullmatch(repo or ""):
        raise GitHubNotFound(f"invalid repository reference {repo!r} (expected owner/name)")
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
               labels: list[str] | None = None) -> list[dict[str, Any]]:
    """Issues (pull requests filtered out), newest activity first."""
    if state not in {"open", "closed", "all"}:
        state = "open"
    items = gh_get(_repo_path(repo, "/issues"),
                   {"state": state, "per_page": "50", "sort": "updated", "direction": "desc"})
    wanted = {label.casefold() for label in labels} if labels else None
    issues = []
    for item in items:
        if not isinstance(item, dict) or "pull_request" in item:
            continue
        label_names = _issue_labels(item)
        if wanted and not (wanted & {name.casefold() for name in label_names}):
            continue
        issues.append({
            "number": item.get("number"),
            "title": item.get("title") or "",
            "state": item.get("state") or "open",
            "author": (item.get("user") or {}).get("login") or "unknown",
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
    comments = gh_get(_repo_path(repo, f"/issues/{number}/comments"), {"per_page": "10"})
    if not isinstance(comments, list):
        comments = []
    return {
        "number": issue.get("number") or int(number),
        "title": issue.get("title") or "",
        "state": issue.get("state") or "open",
        "is_pull_request": "pull_request" in issue,
        "author": (issue.get("user") or {}).get("login") or "unknown",
        "created_at": issue.get("created_at"),
        "updated_at": issue.get("updated_at"),
        "labels": _issue_labels(issue),
        "comments_count": int(issue.get("comments") or 0),
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
    lines = [f"**{repo}** has **{total}** commits by **{len(contributors)}** contributor(s):"]
    for contributor in contributors[:10]:
        share = f" ({round(contributor['commits'] * 100 / total)}%)" if total else ""
        lines.append(f"- **{contributor['login']}** — {contributor['commits']} commits{share}")
    if len(contributors) > 10:
        lines.append(f"...and {len(contributors) - 10} more ([all contributors]({_repo_url(repo)}/graphs/contributors))")
    else:
        lines.append(f"[Repository]({_repo_url(repo)})")
    return "\n".join(lines)


def format_issues(repo: str, issues: list[dict[str, Any]], *, kind: str) -> str:
    if not issues:
        return f"There are no {kind} in **{repo}** right now."
    lines = [f"**{kind.capitalize()} in {repo}** ({len(issues)}):"]
    for issue in issues[:10]:
        labels = f" [{', '.join(issue['labels'])}]" if issue["labels"] else ""
        lines.append(
            f"- #{issue['number']} **{issue['title']}**{labels} — opened by **{issue['author']}** "
            f"on {_month_day(issue['created_at'])}, {issue['comments']} comments "
            f"([view]({issue['url']}))"
        )
    if len(issues) > 10:
        lines.append(f"...and {len(issues) - 10} more ([all {kind}]({_repo_url(repo)}/issues))")
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
    "- `what are the open issues?` / `what bugs are open?`\n"
    "- `summarize issue #6`\n"
    "Set GITHUB_REPO to analyze a different repository."
)


def answer_github_question(question: str, repo: str | None = None) -> str | None:
    """Answer a repository question from live GitHub data, or None if it isn't one."""
    lowered = (question or "").casefold()
    if not any(stem in lowered for stem in HINT_STEMS):
        return None
    # A repository named in the question (URL or owner/name) wins over the configured default
    repo = extract_repo(question) or repo or DEFAULT_REPO
    try:
        number = _issue_number(question)
        if number:
            return format_issue_summary(repo, get_issue(repo, number))
        if "bug" in lowered:
            return format_issues(repo, get_issues(repo, labels=["bug"]), kind="open bugs (labeled `bug`)")
        if "issue" in lowered:
            if "summar" in lowered:
                issues = get_issues(repo)
                if not issues:
                    return f"There are no open issues in **{repo}** to summarize."
                return format_issue_summary(repo, get_issue(repo, issues[0]["number"]))
            return format_issues(repo, get_issues(repo), kind="open issues")
        if "commit" in lowered or "contribut" in lowered:
            return format_contributors(repo, get_contributors(repo))
        return GITHUB_HELP_TEXT.format(repo=repo)
    except GitHubNotFound:
        return f"I couldn't find that on GitHub for **{repo}**. Check the repository or issue number."
    except GitHubUnavailable as exc:
        return f"I couldn't reach GitHub just now: {exc}. Nothing was changed; try again in a minute."
