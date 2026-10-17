"""Unit and integration tests for continuous knowledge synchronization (webhooks and background sync)."""

import hashlib
import hmac
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import falkordb
from fastapi.testclient import TestClient

import continuous_sync
from continuous_sync import BackgroundKnowledgeSyncer, handle_github_webhook, verify_github_signature

# Mock FalkorDB connection for test environment
with patch.object(falkordb, "FalkorDB", return_value=SimpleNamespace(select_graph=lambda _name: None)):
    import api
    from api import app


class MockQueryResult:
    def __init__(self, result_set=None):
        self.result_set = result_set or []


class MockGraph:
    """Mock FalkorDB graph that tracks queries and returns sensible defaults."""

    def __init__(self):
        self.queries: list[tuple[str, dict]] = []
        self.tasks = [("T1", "Auth Service"), ("T2", "Billing API")]
        self.developers = ["Priya", "Sam", "Alice"]

    def query(self, q: str, params: dict | None = None):
        self.queries.append((q, params or {}))
        return MockQueryResult()

    def ro_query(self, q: str, params: dict | None = None):
        self.queries.append((q, params or {}))
        if "MATCH (t:Task) RETURN t.title" in q:
            return MockQueryResult([(title,) for _, title in self.tasks])
        if "MATCH (t:Task)" in q:
            return MockQueryResult(self.tasks)
        if "MATCH (d:Developer)" in q:
            return MockQueryResult([[d] for d in self.developers])
        return MockQueryResult()


def _compute_sig(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


class SignatureVerificationTests(unittest.TestCase):
    def test_valid_signature_passes(self):
        secret = "super-webhook-secret-123"
        body = b'{"action": "opened"}'
        sig = _compute_sig(secret, body)
        self.assertTrue(verify_github_signature(body, sig, secret))

    def test_tampered_payload_fails(self):
        secret = "super-webhook-secret-123"
        body = b'{"action": "opened"}'
        sig = _compute_sig(secret, body)
        tampered = b'{"action": "closed"}'
        self.assertFalse(verify_github_signature(tampered, sig, secret))

    def test_wrong_secret_fails(self):
        secret = "secret-A"
        body = b'{"action": "opened"}'
        sig = _compute_sig("secret-B", body)
        self.assertFalse(verify_github_signature(body, sig, secret))

    def test_missing_or_malformed_header_fails(self):
        secret = "my-secret"
        body = b"{}"
        self.assertFalse(verify_github_signature(body, None, secret))
        self.assertFalse(verify_github_signature(body, "invalid-sig", secret))
        self.assertFalse(verify_github_signature(body, "md5=12345", secret))

    def test_no_secret_configured_is_permissive(self):
        body = b'{"foo": "bar"}'
        self.assertTrue(verify_github_signature(body, None, ""))


class WebhookHandlingTests(unittest.TestCase):
    def setUp(self):
        self.graph = MockGraph()

    @patch("knowledge.refresh_expertise")
    def test_push_event_ingests_commits_and_links_tasks(self, mock_refresh):
        payload = {
            "repository": {"full_name": "Sumit884-byte/sigmaAgent", "default_branch": "main"},
            "commits": [
                {
                    "id": "c1a2b3c4d5",
                    "message": "Implement T1: add token auth service",
                    "author": {"name": "Priya", "username": "priya-dev"},
                    "timestamp": "2026-10-17T12:00:00Z",
                    "url": "https://github.com/Sumit884-byte/sigmaAgent/commit/c1a2b3c4d5",
                    "added": ["auth/service.py"],
                    "modified": [],
                    "removed": [],
                }
            ],
        }
        res = handle_github_webhook(self.graph, "push", payload)
        self.assertEqual(res["status"], "processed")
        self.assertEqual(res["commits_ingested"], 1)
        mock_refresh.assert_called_once_with(self.graph)

        # Verify COMMIT_UPSERT was executed
        commit_upserts = [params for query, params in self.graph.queries if "MERGE (c:Commit" in query]
        self.assertEqual(len(commit_upserts), 1)
        self.assertEqual(commit_upserts[0]["repo"], "Sumit884-byte/sigmaAgent")
        self.assertEqual(commit_upserts[0]["rows"][0]["sha"], "c1a2b3c4d5")

        # Verify task T1 was linked
        task_links = [params for query, params in self.graph.queries if "MATCH (n:Commit" in query]
        self.assertEqual(len(task_links), 1)
        self.assertEqual(task_links[0]["links"], [{"source": "c1a2b3c4d5", "target": "T1"}])

    @patch("knowledge.refresh_expertise")
    def test_issues_event_creates_and_updates_issue(self, mock_refresh):
        payload = {
            "action": "opened",
            "repository": {"full_name": "Sumit884-byte/sigmaAgent"},
            "issue": {
                "number": 42,
                "title": "Fix bug in T2 Billing API",
                "body": "Needs urgent fix",
                "state": "open",
                "html_url": "https://github.com/Sumit884-byte/sigmaAgent/issues/42",
                "user": {"login": "reporter-alice"},
                "assignees": [{"login": "priya-dev"}],
                "labels": [{"name": "bug"}],
            },
        }
        res = handle_github_webhook(self.graph, "issues", payload)
        self.assertEqual(res["status"], "processed")
        self.assertEqual(res["number"], 42)
        mock_refresh.assert_called_once_with(self.graph)

        issue_upserts = [params for query, params in self.graph.queries if "MERGE (i:Issue" in query]
        self.assertEqual(len(issue_upserts), 1)
        row = issue_upserts[0]["rows"][0]
        self.assertEqual(row["key"], "Sumit884-byte/sigmaAgent#42")
        self.assertEqual(row["author"], "reporter-alice")
        self.assertEqual(row["assignees"], ["priya-dev"])
        self.assertFalse(row["is_pull_request"])

    @patch("knowledge.refresh_expertise")
    def test_pull_request_event_marks_as_pr(self, mock_refresh):
        payload = {
            "action": "closed",
            "repository": {"full_name": "Sumit884-byte/sigmaAgent"},
            "pull_request": {
                "number": 99,
                "title": "Add continuous knowledge sync",
                "body": "Resolves sync lag",
                "state": "closed",
                "html_url": "https://github.com/Sumit884-byte/sigmaAgent/pull/99",
                "user": {"login": "contributor-bob"},
                "assignees": [],
                "labels": [{"name": "enhancement"}],
            },
        }
        res = handle_github_webhook(self.graph, "pull_request", payload)
        self.assertEqual(res["status"], "processed")
        self.assertEqual(res["number"], 99)

        issue_upserts = [params for query, params in self.graph.queries if "MERGE (i:Issue" in query]
        self.assertEqual(len(issue_upserts), 1)
        self.assertTrue(issue_upserts[0]["rows"][0]["is_pull_request"])

    def test_unhandled_event_type_is_ignored(self):
        payload = {"repository": {"full_name": "Sumit884-byte/sigmaAgent"}}
        res = handle_github_webhook(self.graph, "fork", payload)
        self.assertEqual(res["status"], "ignored")


class ApiWebhookEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.mock_graph = MockGraph()

    @patch("api.tenant_graph")
    def test_webhook_endpoint_success(self, mock_tenant_graph):
        mock_tenant_graph.return_value = self.mock_graph
        payload = {
            "repository": {"full_name": "Sumit884-byte/sigmaAgent"},
            "issue": {
                "number": 101,
                "title": "Webhook integration test",
                "state": "open",
            },
        }
        headers = {
            "X-GitHub-Event": "issues",
            "Content-Type": "application/json",
        }
        response = self.client.post("/webhooks/github", json=payload, headers=headers)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "processed")
        self.assertEqual(data["number"], 101)

    def test_webhook_endpoint_missing_event_header_returns_400(self):
        response = self.client.post("/webhooks/github", json={})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Missing X-GitHub-Event", response.json()["detail"])

    @patch.dict(os.environ, {"GITHUB_WEBHOOK_SECRET": "test-secret-456"})
    def test_webhook_endpoint_unauthorized_with_wrong_sig(self):
        headers = {
            "X-GitHub-Event": "push",
            "X-Hub-Signature-256": "sha256=invalidhash",
        }
        response = self.client.post("/webhooks/github", json={}, headers=headers)
        self.assertEqual(response.status_code, 401)

    def test_knowledge_sync_status_endpoint(self):
        response = self.client.get("/knowledge/sync/status")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("running", data)
        self.assertIn("total_sync_count", data)
        self.assertIn("webhook_events_count", data)

    @patch("knowledge.sync_repository", return_value={"commits": 5, "issues": 10, "docs": 2})
    @patch("knowledge.refresh_expertise")
    def test_knowledge_sync_run_now_endpoint(self, mock_refresh, mock_sync):
        with patch.object(api.background_syncer, "graph_provider", return_value=self.mock_graph):
            response = self.client.post("/knowledge/sync/run-now")
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["status"], "success")
            self.assertEqual(data["result"]["commits"], 5)


class BackgroundSyncerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_syncer_start_and_stop(self):
        syncer = BackgroundKnowledgeSyncer(
            graph_provider=lambda: MockGraph(),
            default_repo="Sumit884-byte/sigmaAgent",
            interval_seconds=60,
        )
        syncer.start()
        self.assertTrue(syncer.running)
        self.assertIsNotNone(syncer.task)

        syncer.stop()
        self.assertFalse(syncer.running)
        self.assertIsNone(syncer.task)

    @patch("knowledge.sync_repository", return_value={"commits": 12, "issues": 4, "docs": 1})
    @patch("knowledge.refresh_expertise")
    async def test_sync_now_executes_successfully(self, mock_refresh, mock_sync):
        graph = MockGraph()
        syncer = BackgroundKnowledgeSyncer(
            graph_provider=lambda: graph,
            default_repo="Sumit884-byte/sigmaAgent",
            interval_seconds=0,
        )
        res = await syncer.sync_now()
        self.assertEqual(res["status"], "success")
        self.assertEqual(syncer.total_sync_count, 1)
        self.assertIsNotNone(syncer.last_sync_time)
        mock_sync.assert_called_once_with(graph, "Sumit884-byte/sigmaAgent")
        mock_refresh.assert_called_once_with(graph)


if __name__ == "__main__":
    unittest.main()
