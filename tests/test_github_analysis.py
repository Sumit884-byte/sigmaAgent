from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import falkordb
from fastapi.testclient import TestClient

import github_analysis
from github_analysis import (
    GitHubNotFound,
    GitHubUnavailable,
    answer_github_question,
    extract_repo,
    format_contributors,
    format_issue_summary,
    format_issues,
)


def contributor(login: str, count: int) -> dict:
    return {"login": login, "contributions": count,
            "html_url": f"https://github.com/{login}", "type": "User"}


def issue(number: int, title: str, labels: list[str] | None = None,
          author: str = "Sumit884-byte", assignees: list[str] | None = None, **extra) -> dict:
    payload = {
        "number": number,
        "title": title,
        "state": "open",
        "user": {"login": author, "html_url": f"https://github.com/{author}"},
        "assignees": [{"login": name} for name in (assignees or [])],
        "created_at": "2026-10-16T10:00:00Z",
        "updated_at": "2026-10-17T09:00:00Z",
        "labels": [{"name": label} for label in (labels or [])],
        "comments": 2,
        "html_url": f"https://github.com/Sumit884-byte/sigmaAgent/issues/{number}",
        "body": f"Body of issue {number}",
    }
    payload.update(extra)
    return payload


class GitHubAnswerTests(unittest.TestCase):
    def setUp(self):
        github_analysis._CACHE.clear()
        self.repo = "Sumit884-byte/sigmaAgent"

    def test_non_github_questions_are_not_intercepted(self):
        """Task-bot messages must NEVER be claimed by answer_github_question."""
        for question in (
            # original smoke-test cases
            "Why is T5 delayed?",
            "explain T5",
            "what is blocked?",
            "What is Person 4 working on?",
            "list tasks",
            # Critical-1: exact messages from the PR review
            "create task Fix login bug for Alice",
            "T5 is blocked by T3, there's an issue with tokens",
            "compare T3 and T4 progress",
            "set up the repo structure as a new task for Bob",
            "frontend/backend split: who should own which issue?",
        ):
            with self.subTest(question=question):
                self.assertIsNone(answer_github_question(question, self.repo), question)

    def test_path_traversal_rejected(self):
        """A literal '..' in a parsed github.com URL owner or repo name must be rejected and the
        GitHub API must never receive a traversal path like /repos/../user/issues."""
        # Direct traversal: the URL regex requires alphanumeric owner starts, so
        # github.com/../user won't be extracted. But if somehow a '..'-containing string
        # passes extract_repo, is_safe_repo must catch it.
        from github_analysis import is_safe_repo
        traversal_repos = [
            "../user",
            "owner/../etc",
            "..%2F../etc/passwd",
            ".hidden/repo",
            "owner/.hidden",
        ]
        for repo in traversal_repos:
            with self.subTest(repo=repo):
                self.assertFalse(is_safe_repo(repo), f"should be rejected: {repo!r}")

        # Verify answer_github_question passes the traversal repo through is_safe_repo and
        # returns None without calling the API, when the caller explicitly passes a bad repo.
        with patch.object(github_analysis, "_http_get_json") as fetch:
            result = answer_github_question("who are the contributors?", repo="../user")
        fetch.assert_not_called()
        self.assertIsNone(result)

    def test_apostrophe_not_treated_as_label_quote(self):
        """Apostrophes like "bob's" must not be captured as a quoted label filter."""
        with patch.object(github_analysis, "_http_get_json",
                          return_value=[issue(1, "Login crash"), issue(2, "Docs update")]) as fetch:
            answer = answer_github_question("what's the status of bob's open issues in github?", self.repo)
        # The apostrophe must not be parsed as a label; both issues should appear
        self.assertIsNotNone(answer)
        self.assertIn("Login crash", answer)
        self.assertIn("Docs update", answer)

    def test_word_boundary_label_matching(self):
        """'debugging' must not match the 'bug' label; 'docstring' must not match 'docs'."""
        with patch.object(github_analysis, "_http_get_json",
                          return_value=[issue(1, "Fix login crash"), issue(2, "Docs update")]):
            answer = answer_github_question(
                "list open issues about debugging and docstring handling on github", self.repo
            )
        self.assertIsNotNone(answer)
        # Should show both issues (no label filter applied)
        self.assertIn("Fix login crash", answer)
        self.assertIn("Docs update", answer)
        # Should NOT have applied a 'bug' label filter
        self.assertNotIn("labeled `bug`", answer)

    def test_no_explicit_trigger_without_github_intent_returns_none(self):
        """Generic messages with no GitHub trigger word must return None when no repo is passed."""
        for question in (
            "how is the project going?",
            "what did we decide about the auth module?",
            "update me on the sprint",
        ):
            with self.subTest(question=question):
                self.assertIsNone(answer_github_question(question), question)

    def test_contributors_and_commit_counts(self):
        with patch.object(github_analysis, "_http_get_json",
                          return_value=[contributor("Sumit884-byte", 34), contributor("riya-dev", 21)]):
            answer = answer_github_question("who committed how much on github?", self.repo)
        self.assertIn("**Sumit884-byte** — 34 commits (62%)", answer)
        self.assertIn("**riya-dev** — 21 commits (38%)", answer)
        self.assertIn("**55** commits by **2** contributor(s)", answer)
        self.assertIn("https://github.com/Sumit884-byte/sigmaAgent", answer)

    def test_contributor_question_lists_contributors(self):
        with patch.object(github_analysis, "_http_get_json",
                          return_value=[contributor("Sumit884-byte", 34)]):
            answer = answer_github_question("who are the contributors to the repo?", self.repo)
        self.assertIn("**Sumit884-byte** — 34 commits", answer)

    def test_open_issues_exclude_pull_requests_and_link_sources(self):
        with patch.object(github_analysis, "_http_get_json", return_value=[
            issue(7, "Fix sync pagination", ["bug"]),
            issue(6, "Add docs", [], pull_request={"url": "x"}),  # PRs are not issues
        ]):
            answer = answer_github_question("what are the open issues?", self.repo)
        self.assertIn("#7 **Fix sync pagination** [bug]", answer)
        self.assertIn("opened by **Sumit884-byte** on Oct 16", answer)
        self.assertIn("[view](https://github.com/Sumit884-byte/sigmaAgent/issues/7)", answer)
        self.assertNotIn("Add docs", answer)

    def test_bug_question_filters_by_bug_label_case_insensitively(self):
        with patch.object(github_analysis, "_http_get_json", return_value=[
            issue(7, "Crash on sync", ["Bug"]),
            issue(8, "Add docs", ["enhancement"]),
        ]):
            answer = answer_github_question("what bugs are open?", self.repo)
        self.assertIn("Crash on sync", answer)
        self.assertNotIn("Add docs", answer)
        self.assertIn("labeled `bug`", answer)

    def test_issue_number_question_summarizes_that_issue(self):
        comments = [
            {"user": {"login": "riya-dev"}, "created_at": "2026-10-16T11:00:00Z",
             "body": "I can reproduce this on the staging graph.",
             "html_url": "https://github.com/Sumit884-byte/sigmaAgent/issues/7#issuecomment-1"},
        ]
        with patch.object(github_analysis, "_http_get_json",
                          side_effect=[issue(7, "Fix sync pagination", ["bug"]), comments]):
            answer = answer_github_question("summarize issue #7", self.repo)
        self.assertIn("**Issue #7: Fix sync pagination** — open", answer)
        self.assertIn("Opened by **Sumit884-byte** on Oct 16; 2 comments", answer)
        self.assertIn("`bug`", answer)
        self.assertIn("> Body of issue 7", answer)
        self.assertIn("**riya-dev**", answer)
        self.assertIn("[View on GitHub](https://github.com/Sumit884-byte/sigmaAgent/issues/7)", answer)

    def test_summarize_without_number_uses_most_recent_open_issue(self):
        with patch.object(github_analysis, "_http_get_json", side_effect=[
            [issue(9, "Latest problem", ["bug"])],
            issue(9, "Latest problem", ["bug"]),
            [],
        ]):
            answer = answer_github_question("summarize the latest issue", self.repo)
        self.assertIn("**Issue #9: Latest problem**", answer)

    def test_missing_issue_gives_friendly_answer(self):
        with patch.object(github_analysis, "_http_get_json", side_effect=GitHubNotFound("gone")):
            answer = answer_github_question("summarize issue #999", self.repo)
        self.assertIn("couldn't find that on GitHub", answer)

    def test_rate_limit_gives_friendly_answer_with_token_hint(self):
        with patch.object(github_analysis, "_http_get_json",
                          side_effect=GitHubUnavailable("rate limit used up")):
            answer = answer_github_question("open issues?", self.repo)
        self.assertIn("couldn't reach GitHub", answer)
        self.assertIn("Nothing was changed", answer)

    def test_github_hint_without_action_shows_help(self):
        with patch.object(github_analysis, "_http_get_json") as fetch:
            answer = answer_github_question("tell me about the github", self.repo)
        self.assertIn("who are the contributors?", answer)
        self.assertIn("summarize issue #6", answer)
        fetch.assert_not_called()

    def test_repeated_answers_use_the_cache(self):
        with patch.object(github_analysis, "_http_get_json",
                          return_value=[contributor("Sumit884-byte", 34)]) as fetch:
            first = answer_github_question("who are the contributors?", self.repo)
            second = answer_github_question("who committed how much?", self.repo)
        self.assertEqual(fetch.call_count, 1)
        self.assertIn("Sumit884-byte", first)
        self.assertIn("Sumit884-byte", second)

    def test_empty_states(self):
        self.assertIn("no open issues", format_issues(self.repo, [], kind="open issues"))
        self.assertIn("**0** commits by **0** contributor(s)", format_contributors(self.repo, []))

    def test_repo_url_in_question_wins_over_default(self):
        asked = []

        def fetch(path, params=None):
            asked.append(path)
            return [contributor("barchart-dev", 9)]

        with patch.object(github_analysis, "_http_get_json", side_effect=fetch):
            answer = answer_github_question(
                "for the repository, https://github.com/barchart/marketdata-api-js "
                "who are the contributors?"
            )
        self.assertEqual(asked, ["/repos/barchart/marketdata-api-js/contributors"])
        self.assertIn("**barchart/marketdata-api-js**", answer)
        self.assertIn("**barchart-dev** — 9 commits", answer)
        self.assertNotIn(self.repo, answer)

    def test_bare_owner_name_in_question_wins_over_default(self):
        asked = []

        def fetch(path, params=None):
            asked.append(path)
            return [contributor("barchart-dev", 9)]

        with patch.object(github_analysis, "_http_get_json", side_effect=fetch):
            answer = answer_github_question("who are the contributors to barchart/marketdata-api-js?")
        self.assertEqual(asked, ["/repos/barchart/marketdata-api-js/contributors"])
        self.assertIn("**barchart/marketdata-api-js**", answer)

    def test_url_with_trailing_punctuation_is_cleaned(self):
        self.assertEqual(extract_repo("see https://github.com/barchart/marketdata-api-js."),
                         "barchart/marketdata-api-js")
        self.assertEqual(extract_repo("https://github.com/sumit884-byte/sigmaAgent/issues/6 who committed?"),
                         "sumit884-byte/sigmaAgent")

    def test_issue_url_summarizes_that_issue_on_that_repo(self):
        asked = []

        def fetch(path, params=None):
            asked.append(path)
            if path.endswith("/issues/42"):
                return issue(42, "WebSocket drops frames", ["bug"])
            return []

        with patch.object(github_analysis, "_http_get_json", side_effect=fetch):
            answer = answer_github_question(
                "summarize https://github.com/barchart/marketdata-api-js/issues/42"
            )
        self.assertIn("/repos/barchart/marketdata-api-js/issues/42", asked)
        self.assertIn("**Issue #42: WebSocket drops frames**", answer)

    def test_numbers_like_dates_are_not_repos(self):
        asked = []

        def fetch(path, params=None):
            asked.append(path)
            return [contributor("Sumit884-byte", 34)]

        with patch.object(github_analysis, "_http_get_json", side_effect=fetch):
            answer_github_question("who are the contributors? uptime is 24/7")
        self.assertEqual(asked, [f"/repos/{self.repo}/contributors"])

    def test_unknown_repo_in_url_is_named_in_the_answer(self):
        with patch.object(github_analysis, "_http_get_json", side_effect=GitHubNotFound("gone")):
            answer = answer_github_question(
                "who are the contributors to https://github.com/nobody/nothing-here?"
            )
        self.assertIn("couldn't find that on GitHub for **nobody/nothing-here**", answer)

    def test_quoted_label_filter(self):
        asked = []

        def fetch(path, params=None):
            asked.append((path, params))
            return [issue(1, "Fix kernel crash", ["Good First Issue"]),
                    issue(2, "Refactor scheduler", ["enhancement"])]

        with patch.object(github_analysis, "_http_get_json", side_effect=fetch):
            answer = answer_github_question(
                'what issues in vllm-project/vllm are labeled "good first issue"?'
            )
        self.assertEqual(asked[0][0], "/repos/vllm-project/vllm/issues")
        self.assertIn("Fix kernel crash", answer)
        self.assertNotIn("Refactor scheduler", answer)
        self.assertIn("labeled `good first issue`", answer)

    def test_assignee_and_author_filters(self):
        with patch.object(github_analysis, "_http_get_json", return_value=[
            issue(1, "Mine", assignees=["octocat"], author="riya-dev"),
            issue(2, "Theirs", assignees=["other-user"], author="octocat"),
        ]):
            by_assignee = answer_github_question("what issues are assigned to @octocat?")
            by_author = answer_github_question("what issues were opened by octocat?")
        self.assertIn("Mine", by_assignee)
        self.assertNotIn("Theirs", by_assignee)
        self.assertIn("assigned to **octocat**", by_assignee)
        self.assertIn("Theirs", by_author)
        self.assertNotIn("Mine", by_author)
        self.assertIn("opened by **octocat**", by_author)

    def test_assigned_to_me_asks_for_a_github_username(self):
        with patch.object(github_analysis, "_http_get_json") as fetch:
            answer = answer_github_question("what issues are assigned to me?")
        fetch.assert_not_called()
        self.assertIn("GitHub username", answer)

    def test_leaderboard_ranks_issue_authors_over_all_states(self):
        asked = []

        def fetch(path, params=None):
            asked.append((path, params))
            return [issue(1, "a", author="riya-dev"), issue(2, "b", author="riya-dev"),
                    issue(3, "c", author="octocat")]

        with patch.object(github_analysis, "_http_get_json", side_effect=fetch):
            answer = answer_github_question("who opened the most issues in vllm-project/vllm?")
        self.assertEqual(asked[0][1]["state"], "all")
        self.assertIn("**riya-dev** — 2 issue(s)", answer)
        self.assertIn("**octocat** — 1 issue(s)", answer)
        self.assertIn("fetched window", answer)

    def test_closed_state_is_passed_through(self):
        asked = []

        def fetch(path, params=None):
            asked.append((path, params))
            return [issue(1, "Done long ago")]

        with patch.object(github_analysis, "_http_get_json", side_effect=fetch):
            answer = answer_github_question("what closed issues are there?")
        self.assertEqual(asked[0][1]["state"], "closed")
        self.assertIn("closed issues", answer.casefold())

    def test_url_containing_issues_with_commit_question_answers_contributors(self):
        asked = []

        def fetch(path, params=None):
            asked.append(path)
            return [contributor("coder1", 50), contributor("coder2", 30)]

        with patch.object(github_analysis, "_http_get_json", side_effect=fetch):
            answer = answer_github_question(
                "https://github.com/vllm-project/vllm/issues for this repo. how many of the people have commited?"
            )
        self.assertEqual(asked, ["/repos/vllm-project/vllm/contributors"])
        self.assertIn("**vllm-project/vllm** has **80** commits by **2** contributor(s)", answer)
        self.assertIn("**coder1** — 50 commits", answer)

    def test_friend_competition_comparison(self):
        asked = []

        def fetch(path, params=None):
            asked.append(path)
            if "contributors" in path:
                return [contributor("alice", 25), contributor("bob", 10), contributor("charlie", 50)]
            if "issues" in path:
                return [
                    issue(1, "Fix memory leak", author="alice", assignees=["alice"], state="closed"),
                    issue(2, "Add feature", author="alice", assignees=["alice"], state="open"),
                    issue(3, "Update docs", author="bob", assignees=["bob"], state="open"),
                    issue(4, "Refactor engine", author="charlie", assignees=["charlie"], state="open"),
                ]
            return []

        with patch.object(github_analysis, "_http_get_json", side_effect=fetch):
            answer = answer_github_question("compare alice and bob in vllm-project/vllm")

        self.assertIn("Open-Source Showdown in vllm-project/vllm", answer)
        self.assertIn("**alice**", answer)
        self.assertIn("Commits: **25** (#2 overall)", answer)
        self.assertIn("Assigned Issues: **2** (1 completed, 1 in progress)", answer)
        self.assertIn("**bob**", answer)
        self.assertIn("Commits: **10** (#3 overall)", answer)
        self.assertIn("**Leader**: **alice** leads the competition", answer)

    def test_unassigned_issues_filter(self):
        asked = []

        def fetch(path, params=None):
            asked.append((path, params))
            return [
                issue(1, "Assigned issue", assignees=["alice"]),
                issue(2, "Unclaimed issue", assignees=[]),
            ]

        with patch.object(github_analysis, "_http_get_json", side_effect=fetch):
            answer = answer_github_question("what unassigned issues are open in vllm-project/vllm?")

        self.assertIn("unassigned issues in vllm-project/vllm", answer)
        self.assertIn("Unclaimed issue", answer)
        self.assertNotIn("Assigned issue", answer)

    def test_assignee_leaderboard(self):
        asked = []

        def fetch(path, params=None):
            asked.append((path, params))
            return [
                issue(1, "Task 1", assignees=["alice", "bob"]),
                issue(2, "Task 2", assignees=["alice"]),
                issue(3, "Task 3", assignees=["charlie"]),
            ]

        with patch.object(github_analysis, "_http_get_json", side_effect=fetch):
            answer = answer_github_question("who has the most assigned issues in vllm-project/vllm?")

        self.assertIn("Top issue assignees in vllm-project/vllm", answer)
        self.assertIn("**alice** — 2 assigned issue(s)", answer)
        self.assertIn("**bob** — 1 assigned issue(s)", answer)


_original_falkordb = falkordb.FalkorDB
_import_graph = SimpleNamespace(query=lambda *_args, **_kwargs: SimpleNamespace(result_set=[]))
with patch.object(falkordb, "FalkorDB", return_value=SimpleNamespace(select_graph=lambda _name: _import_graph)):
    import api


class GitHubApiRouteTests(unittest.TestCase):
    """The /investigate and /github endpoints wired to the GitHub module."""

    def setUp(self):
        github_analysis._CACHE.clear()
        self.client = TestClient(api.app)

    def test_investigate_answers_github_question_without_a_guild(self):
        with patch.object(github_analysis, "_http_get_json",
                          return_value=[contributor("Sumit884-byte", 34)]):
            response = self.client.post("/investigate", json={"question": "who are the contributors?"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("**Sumit884-byte** — 34 commits", response.json()["answer"])

    def test_contributors_endpoint_returns_data(self):
        with patch.object(github_analysis, "_http_get_json",
                          return_value=[contributor("Sumit884-byte", 34)]):
            response = self.client.get("/github/contributors")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["data"][0]["commits"], 34)
        self.assertEqual(body["repo"], "Sumit884-byte/sigmaAgent")

    def test_issues_endpoint_filters_label_and_pull_requests(self):
        with patch.object(github_analysis, "_http_get_json", return_value=[
            issue(7, "Crash on sync", ["bug"]),
            issue(6, "Add docs", [], pull_request={"url": "x"}),
        ]):
            response = self.client.get("/github/issues", params={"label": "bug"})
        self.assertEqual(response.status_code, 200, response.text)
        numbers = [row["number"] for row in response.json()["data"]]
        self.assertEqual(numbers, [7])

    def test_missing_issue_maps_to_404(self):
        with patch.object(github_analysis, "_http_get_json", side_effect=GitHubNotFound("gone")):
            response = self.client.get("/github/issue/999")
        self.assertEqual(response.status_code, 404)

    def test_issues_endpoint_unassigned_filter(self):
        with patch.object(github_analysis, "_http_get_json", return_value=[
            issue(1, "Assigned", assignees=["alice"]),
            issue(2, "Unassigned", assignees=[]),
        ]):
            response = self.client.get("/github/issues", params={"unassigned": "true"})
        self.assertEqual(response.status_code, 200, response.text)
        numbers = [row["number"] for row in response.json()["data"]]
        self.assertEqual(numbers, [2])

    def test_compare_endpoint_returns_showdown(self):
        def fetch(path, params=None):
            if "contributors" in path:
                return [contributor("alice", 20), contributor("bob", 15)]
            return [issue(1, "Task", author="alice", assignees=["bob"])]

        with patch.object(github_analysis, "_http_get_json", side_effect=fetch):
            response = self.client.get("/github/compare", params={"users": "alice,bob", "repo": "vllm-project/vllm"})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "success")
        self.assertIn("Open-Source Showdown in vllm-project/vllm", body["summary"])

    def test_compare_endpoint_requires_two_users(self):
        response = self.client.get("/github/compare", params={"users": "alice"})
        self.assertEqual(response.status_code, 400)

    def test_github_unavailable_maps_to_503(self):
        with patch.object(github_analysis, "_http_get_json",
                          side_effect=GitHubUnavailable("rate limited")):
            response = self.client.get("/github/contributors")
        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
