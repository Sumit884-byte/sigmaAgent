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

    def test_no_secret_configured_rejects_by_default(self):
        """Without a secret configured the endpoint must be closed off by default."""
        body = b'{"foo": "bar"}'
        self.assertFalse(verify_github_signature(body, None, ""))

    @patch.dict(os.environ, {"ALLOW_INSECURE_WEBHOOKS": "true"})
    def test_no_secret_with_dev_flag_is_permissive(self):
        """With ALLOW_INSECURE_WEBHOOKS=true the endpoint accepts unsigned requests in dev/CI."""
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

    @patch.dict(os.environ, {"ALLOW_INSECURE_WEBHOOKS": "true"})
    @patch("api.tenant_graph")
    def test_webhook_endpoint_success(self, mock_tenant_graph):
        """A correctly formed webhook request is accepted and processed (dev flag bypasses HMAC)."""
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

    def test_knowledge_sync_status_endpoint_requires_auth(self):
        # Unconfigured key fails closed with 503
        with patch.dict(os.environ, {"INVESTIGATION_API_KEY": ""}):
            response = self.client.get("/knowledge/sync/status")
            self.assertEqual(response.status_code, 503)

        # Configured key rejects unauthenticated requests with 401
        with patch.dict(os.environ, {"INVESTIGATION_API_KEY": "valid-secret-key"}):
            res_no_auth = self.client.get("/knowledge/sync/status")
            self.assertEqual(res_no_auth.status_code, 401)

            # Authenticated request succeeds with 200
            res_auth = self.client.get("/knowledge/sync/status", headers={"Authorization": "Bearer valid-secret-key"})
            self.assertEqual(res_auth.status_code, 200)
            data = res_auth.json()
            self.assertIn("running", data)
            self.assertIn("total_sync_count", data)
            self.assertIn("webhook_events_count", data)

    @patch("knowledge.sync_repository", return_value={"commits": 5, "issues": 10, "docs": 2})
    @patch("knowledge.refresh_expertise")
    def test_knowledge_sync_run_now_endpoint(self, mock_refresh, mock_sync):
        with patch.object(api.background_syncer, "graph_provider", return_value=self.mock_graph), \
             patch.dict(os.environ, {"INVESTIGATION_API_KEY": "valid-secret-key"}):
            headers = {"Authorization": "Bearer valid-secret-key"}
            response = self.client.post("/knowledge/sync/run-now", headers=headers)
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


class DiscordStreamingTests(unittest.TestCase):
    def setUp(self):
        self.graph = MockGraph()

    def test_handle_discord_stream_message_creates_nodes_and_links(self):
        payload = {
            "guild_id": "12345",
            "guild_name": "Acme Corp",
            "channel_id": "chan-99",
            "channel_name": "engineering",
            "message_id": "msg-101",
            "author_id": "user-55",
            "author_name": "Priya",
            "content": "Hey team, I started working on T1 Auth Service today!",
            "source_url": "https://discord.com/channels/12345/chan-99/msg-101",
        }
        res = handle_discord_stream_message(self.graph, payload)
        self.assertEqual(res["status"], "streamed")
        self.assertEqual(res["message_id"], "msg-101")
        self.assertIn("T1", res["mentioned_tasks"])

        queries_text = " ".join(q for q, _ in self.graph.queries)
        self.assertIn("DiscordChannel", queries_text)
        self.assertIn("DiscordMessage", queries_text)
        self.assertIn("DiscordAuthor", queries_text)
        self.assertIn("MENTIONS", queries_text)

    def test_handle_discord_message_edit(self):
        res = handle_discord_message_edit(self.graph, "msg-101", "Actually I am working on T2 Billing API now.")
        self.assertEqual(res["status"], "updated")
        self.assertIn("T2", res["mentioned_tasks"])

    def test_handle_discord_message_delete_soft_and_hard(self):
        res_soft = handle_discord_message_delete(self.graph, "msg-101", soft_delete=True)
        self.assertEqual(res_soft["status"], "soft_deleted")

        res_hard = handle_discord_message_delete(self.graph, "msg-101", soft_delete=False)
        self.assertEqual(res_hard["status"], "deleted")


class GarbageCollectionTests(unittest.TestCase):
    def setUp(self):
        self.graph = MockGraph()

    def test_prune_graph_garbage_runs_all_checks(self):
        stats = prune_graph_garbage(self.graph, max_message_age_days=30)
        self.assertIn("pruned_doc_sections", stats)
        self.assertIn("pruned_files", stats)
        self.assertIn("pruned_topics", stats)
        self.assertIn("pruned_deleted_messages", stats)
        self.assertIn("pruned_expired_messages", stats)
        self.assertIn("total_pruned", stats)


class SyncerGCTests(unittest.IsolatedAsyncioTestCase):
    async def test_syncer_runs_gc(self):
        graph = MockGraph()
        syncer = BackgroundKnowledgeSyncer(
            graph_provider=lambda: graph,
            default_repo="Sumit884-byte/sigmaAgent",
            interval_seconds=0,
        )
        res = await syncer.run_gc(max_message_age_days=14)
        self.assertEqual(res["status"], "success")
        self.assertEqual(syncer.total_gc_count, 1)
        self.assertIsNotNone(syncer.last_gc_time)


class DiscordAndGCApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.mock_graph = MockGraph()
        self.headers = {"Authorization": "Bearer test-internal-key"}

    def test_stream_message_endpoint_auth(self):
        # 503 when INVESTIGATION_API_KEY is unset
        with patch.dict(os.environ, {"INVESTIGATION_API_KEY": ""}):
            res = self.client.post("/discord/stream-message", json={"channel_id": "c1", "message_id": "m1", "author_id": "a1", "author_name": "Dev", "content": "hello"})
            self.assertEqual(res.status_code, 503)

        # 401 when INVESTIGATION_API_KEY is configured but request lacks valid key
        with patch.dict(os.environ, {"INVESTIGATION_API_KEY": "test-internal-key"}):
            res = self.client.post("/discord/stream-message", json={"channel_id": "c1", "message_id": "m1", "author_id": "a1", "author_name": "Dev", "content": "hello"})
            self.assertEqual(res.status_code, 401)

            with patch.object(api, "tenant_graph", return_value=self.mock_graph):
                payload = {
                    "guild_id": "12345",
                    "channel_id": "chan-1",
                    "message_id": "msg-888",
                    "author_id": "dev-1",
                    "author_name": "Sam",
                    "content": "Check T1 and T2",
                }
                res = self.client.post("/discord/stream-message", json=payload, headers=self.headers)
                self.assertEqual(res.status_code, 200)
                self.assertEqual(res.json()["status"], "streamed")

    def test_stream_message_edit_and_delete_endpoints(self):
        with patch.object(api, "tenant_graph", return_value=self.mock_graph), \
             patch.dict(os.environ, {"INVESTIGATION_API_KEY": "test-internal-key", "DISCORD_GUILD_ID": "12345"}):
            res_edit = self.client.put("/discord/stream-message/msg-888", json={"content": "new text", "guild_id": "12345"}, headers=self.headers)
            self.assertEqual(res_edit.status_code, 200)
            self.assertEqual(res_edit.json()["status"], "updated")

            res_del = self.client.delete("/discord/stream-message/msg-888", params={"guild_id": "12345"}, headers=self.headers)
            self.assertEqual(res_del.status_code, 200)
            self.assertEqual(res_del.json()["status"], "soft_deleted")

    def test_gc_endpoint_and_status(self):
        with patch.object(api, "tenant_graph", return_value=self.mock_graph), \
             patch.dict(os.environ, {"INVESTIGATION_API_KEY": "test-internal-key", "DISCORD_GUILD_ID": "12345"}):
            res = self.client.post("/knowledge/gc", json={"max_message_age_days": 60}, headers=self.headers)
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["status"], "success")
            self.assertIn("stats", res.json())

            res_status = self.client.get("/knowledge/gc/status", headers=self.headers)
            self.assertEqual(res_status.status_code, 200)
            self.assertGreaterEqual(res_status.json()["total_gc_count"], 1)

    def test_run_gc_chat_command_returns_safety_advisory(self):
        with patch.object(api, "tenant_graph", return_value=self.mock_graph):
            res = self.client.post("/investigate", json={"question": "run gc", "discord": {"guild_id": "12345"}})
            self.assertEqual(res.status_code, 200)
            self.assertIn("Garbage collection modifies graph state", res.json()["answer"])

    def test_unmapped_repo_webhook_returns_400(self):
        with patch.dict(os.environ, {"ALLOW_INSECURE_WEBHOOKS": "true", "REPO_TO_GUILD_MAP": "{}", "DISCORD_GUILD_ID": ""}):
            payload = {"repository": {"full_name": "unknown-org/unknown-repo"}}
            headers = {"X-GitHub-Event": "push", "Content-Type": "application/json"}
            res = self.client.post("/webhooks/github", json=payload, headers=headers)
            self.assertEqual(res.status_code, 400)
            self.assertIn("Repository is not mapped", res.json()["detail"])

    def test_webhook_stores_commits_when_no_tasks(self):
        """Commits, docs and issues must be ingested even when the graph has no tasks yet."""
        empty_graph = MockGraph()
        empty_graph.tasks = []
        payload = {
            "repository": {"full_name": "Sumit884-byte/sigmaAgent", "default_branch": "main"},
            "commits": [{"id": "abc123", "message": "feat: init", "author": {"name": "Dev", "username": "dev"}, "added": [], "modified": [], "removed": []}],
        }
        res = handle_github_webhook(empty_graph, "push", payload)
        self.assertEqual(res["status"], "processed")
        self.assertEqual(res["commits_ingested"], 1)
        commit_upserts = [params for q, params in empty_graph.queries if "MERGE (c:Commit" in q]
        self.assertEqual(len(commit_upserts), 1)

    def test_stream_without_guild_id_returns_400(self):
        """Stream endpoint must reject with 400 when guild_id is missing and DISCORD_GUILD_ID is unset."""
        with patch.dict(os.environ, {"INVESTIGATION_API_KEY": "test-internal-key", "DISCORD_GUILD_ID": ""}):
            res = self.client.post(
                "/discord/stream-message",
                json={"channel_id": "c1", "message_id": "m1", "author_id": "a1", "author_name": "Dev", "content": "hello"},
                headers=self.headers,
            )
            self.assertEqual(res.status_code, 400)
            self.assertIn("guild_id is required", res.json()["detail"])

    def test_developer_mapping_uses_discord_id_only(self):
        # Two users with same name 'Alice' but different discord IDs
        payload1 = {
            "channel_id": "c1", "message_id": "m1",
            "author_id": "alice-id-1", "author_name": "Alice",
            "content": "Working on T1",
        }
        handle_discord_stream_message(self.mock_graph, payload1)
        map_queries = [q for q, _ in self.mock_graph.queries if "MATCH (d:Developer)" in q]
        self.assertTrue(any("WHERE d.discord_id = $author_id" in q for q in map_queries))
        self.assertFalse(any("d.name = $author_name" in q for q in map_queries))

    def test_message_delete_cleans_evidence(self):
        res = handle_discord_message_delete(self.mock_graph, "msg-evidence-1", soft_delete=True)
        self.assertEqual(res["status"], "soft_deleted")
        del_queries = [q for q, _ in self.mock_graph.queries if "DETACH DELETE e" in q]
        self.assertTrue(len(del_queries) > 0)

    def test_mentioned_tasks_matches_jira_style_keys(self):
        tasks = [("ENG-123", "Setup infrastructure"), ("T1", "Auth API")]
        found = knowledge.mentioned_tasks("Fixes ENG-123 and T1 in PR", tasks)
        self.assertIn("ENG-123", found)
        self.assertIn("T1", found)


if __name__ == "__main__":
    unittest.main()
