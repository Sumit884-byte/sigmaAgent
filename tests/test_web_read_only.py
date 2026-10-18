import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import api

TEST_INVESTIGATION_KEY = "test-investigation-key"
AUTHORIZED = SimpleNamespace(headers={"Authorization": f"Bearer {TEST_INVESTIGATION_KEY}"})


GUILD_ID = "123456789012345678"


class WebReadOnlyTests(unittest.TestCase):
    def test_sync_command_is_rejected_without_running_repository_sync(self):
        with patch.dict(os.environ, {"DISCORD_GUILD_ID": GUILD_ID, "INVESTIGATION_API_KEY": TEST_INVESTIGATION_KEY}), \
             patch.object(api.knowledge, "sync_repository") as sync_repository:
            result = api.investigate(api.InvestigateRequest(
                question="sync github evil-org/poisoned-repo",
                read_only=True,
            ), AUTHORIZED)

        self.assertIn("read-only", result["answer"].lower())
        sync_repository.assert_not_called()

    def test_non_discord_request_cannot_apply_gemini_changes(self):
        reply = api.AgentReply(
            answer="I will delete T5.",
            changes=[api.TaskChange(action="delete", task_id="T5")],
        )
        with patch.dict(os.environ, {"DISCORD_GUILD_ID": GUILD_ID, "INVESTIGATION_API_KEY": TEST_INVESTIGATION_KEY}), \
             patch.object(api, "_all_tasks", return_value=[["T5", "Build API", "Person 3", None, "todo"]]), \
             patch.object(api, "_explain_dependency_path", return_value=None), \
             patch.object(api, "_impact_answer", return_value=None), \
             patch.object(api, "_bottleneck_answer", return_value=None), \
             patch.object(api, "_ownership_provenance_answer", return_value=None), \
             patch.object(api, "_gemini_client", return_value=object()), \
             patch.object(api, "_channel_history", return_value=[]), \
             patch.object(api, "_ask_gemini", return_value=reply), \
             patch.object(api, "_apply_change") as apply_change:
            result = api.investigate(api.InvestigateRequest(question="go ahead and delete task T5"), AUTHORIZED)

        self.assertIn("read-only", result["answer"].lower())
        self.assertIn("didn't make any changes", result["answer"].lower())
        apply_change.assert_not_called()

    def test_experts_overview_does_not_refresh_graph_in_read_only_mode(self):
        graph = SimpleNamespace()
        with patch.object(api, "tenant_graph", return_value=graph), \
             patch.object(api.knowledge, "refresh_expertise") as refresh_expertise, \
             patch.object(api.knowledge, "experts_overview", return_value=[]):
            api._expertise_answer("experts", GUILD_ID, {}, read_only=True)

        refresh_expertise.assert_not_called()


if __name__ == "__main__":
    unittest.main()
