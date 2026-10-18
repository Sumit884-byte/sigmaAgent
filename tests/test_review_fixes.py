"""Regression tests for bugs found in the code review (auth, crashes, routing, data integrity)."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import falkordb
from fastapi.testclient import TestClient

from test_change_provenance import RecordingGraph

with patch.object(falkordb, "FalkorDB", return_value=SimpleNamespace(select_graph=lambda _name: RecordingGraph())):
    import api

KEY = "test-investigation-key"
GUILD = "123456789012345678"
DISCORD = {
    "guild_id": GUILD, "channel_id": "234567890123456789", "message_id": "345678901234567890",
    "author_id": "456789012345678901", "author_name": "Sumit", "timestamp": "2026-10-18T12:00:00+00:00",
}


class Base(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"INVESTIGATION_API_KEY": KEY})
        env.start()
        self.addCleanup(env.stop)
        self.graph = RecordingGraph()
        graph = patch.object(api, "tenant_graph", return_value=self.graph)
        graph.start()
        self.addCleanup(graph.stop)
        self.client = TestClient(api.app)

    def post(self, question, discord=DISCORD, key=KEY):
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        return self.client.post("/investigate", json={"question": question, "discord": discord}, headers=headers)


class AuthTests(Base):
    def test_investigate_requires_the_key(self):
        self.assertEqual(self.post("help", key=None).status_code, 401)
        self.assertEqual(self.post("help", key="wrong").status_code, 401)
        self.assertEqual(self.post("help").status_code, 200)

    def test_dependencies_requires_the_key(self):
        self.assertEqual(self.client.get("/dependencies", params={"guild_id": GUILD}).status_code, 401)


class ModelReplyTests(Base):
    def test_unparseable_model_reply_is_not_a_500(self):
        with patch.object(api, "_gemini_client", return_value=object()), \
             patch.object(api, "_channel_history", return_value=[]), \
             patch.object(api, "_ask_gemini", return_value=None):
            response = self.post("how is the project going overall")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["answer"])


class TaskCommandTests(Base):
    def test_list_tasks_shows_status_not_blockers(self):
        self.graph.tasks = [["T1", "Auth", "Person 1", None, "done"], ["T2", "Frontend", None, "Auth", "in_progress"]]
        answer = api._direct_task_command_answer("list tasks", GUILD, DISCORD)
        self.assertIn("**T1**: Auth (Person 1) [done]", answer)
        self.assertIn("**T2**: Frontend (unassigned) [in_progress]", answer)
        self.assertNotIn("['Auth']", answer)

    def test_assign_to_me_assigns_the_sender(self):
        answer = api._direct_task_command_answer("assign T5 to me", GUILD, DISCORD)
        self.assertIn("Assigned **T5** to **Sumit**", answer)
        self.assertEqual(self.graph.tasks[0][2], "Sumit")

    def test_compound_messages_are_left_to_the_model(self):
        for question in ["assign T5 to Priya and T4 to Sam", "mark T5 as done and T4 as in progress"]:
            self.assertIsNone(api._direct_task_command_answer(question, GUILD, DISCORD), question)

    def test_unresolvable_assignment_is_left_to_the_model(self):
        self.assertIsNone(api._direct_task_command_answer("assign Nonexistent Thing to Priya", GUILD, DISCORD))


class ResolutionTests(unittest.TestCase):
    def test_delete_needs_an_exact_task(self):
        graph = SimpleNamespace(query=lambda q, p=None: SimpleNamespace(result_set=[["T1"]] if "CONTAINS" in q else []))
        with patch.object(api, "tenant_graph", return_value=graph), patch.object(api, "_task_exists", return_value=False):
            self.assertEqual(api._resolve_task(GUILD, "Frontend Auth Integration"), "T1")
            self.assertIsNone(api._resolve_task(GUILD, "Frontend Auth Integration", exact=True))

    def test_unmapped_repo_webhooks_do_not_fall_back_to_the_default_server(self):
        with patch.dict(os.environ, {"DISCORD_GUILD_ID": GUILD, "REPO_TO_GUILD_MAP": "{}"}):
            self.assertEqual(api._resolve_guild_for_repo(api.GITHUB_REPO), GUILD)
            with self.assertRaises(ValueError):
                api._resolve_guild_for_repo("someone-else/other-repo")

    def test_no_default_graph_without_a_configured_server(self):
        with patch.dict(os.environ, {"DISCORD_GUILD_ID": ""}):
            self.assertIsNone(api._default_graph_provider())


class KnowledgeQueryTests(unittest.TestCase):
    def test_issue_upsert_replaces_assignees(self):
        query = api.knowledge.ISSUE_UPSERT
        self.assertLess(query.index("DELETE old"), query.index("UNWIND row.assignees"))


if __name__ == "__main__":
    unittest.main()
