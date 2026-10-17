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


def issue(number: int, title: str, labels: list[str] | None = None, **extra) -> dict:
    payload = {
        "number": number,
        "title": title,
        "state": "open",
        "user": {"login": "Sumit884-byte"},
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
        for question in (
            "Why is T5 delayed?",
            "explain T5",
            "what is blocked?",
            "What is Person 4 working on?",
            "list tasks",
        ):
            self.assertIsNone(answer_github_question(question, self.repo), question)

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

    def test_github_unavailable_maps_to_503(self):
        with patch.object(github_analysis, "_http_get_json",
                          side_effect=GitHubUnavailable("rate limited")):
            response = self.client.get("/github/contributors")
        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
