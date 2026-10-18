"""Arka-over-MCP client tests against a fake MCP server (no Arka install needed)."""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import falkordb

import arka_llm

with patch.object(falkordb, "FalkorDB", return_value=SimpleNamespace(select_graph=lambda _name: None)):
    import api

# A tiny stdio MCP server: the prompt's MODE:<x> marker chooses how it behaves
FAKE_SERVER = r'''
import json, sys, time
bad_seen = False
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    if msg["method"] == "initialize":
        result = {"protocolVersion": "2024-11-05", "serverInfo": {"name": "fake-arka"}, "capabilities": {}}
    else:
        prompt = msg["params"]["arguments"]["prompt"]
        if "MODE:crash" in prompt:
            sys.exit(1)
        if "MODE:slow" in prompt:
            time.sleep(5)
        if "MODE:env" in prompt:
            import os
            text = "compact=" + os.environ.get("ARKA_PROMPT_COMPACT", "unset")
        elif "MODE:fenced" in prompt:
            text = '```json\n{"answer": "fenced ok", "changes": []}\n```'
        elif "MODE:bad" in prompt and not bad_seen:
            bad_seen = True
            text = "Sure! Here you go."
        elif "MODE:bad" in prompt or "MODE:json" in prompt:
            text = 'Here: {"answer": "parsed", "changes": [{"action": "assign", "task_id": "T1", "developer": "Sam"}]}'
        else:
            text = prompt
        result = {"content": [{"type": "text", "text": text}]}
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
'''


def fake_llm():
    return arka_llm.ArkaLLM(arka_llm.ArkaMCPClient([sys.executable, "-c", FAKE_SERVER]))


class ArkaClientTests(unittest.TestCase):
    def setUp(self):
        self.llm = fake_llm()

    def tearDown(self):
        self.llm.client.close()

    def test_prompts_go_to_arkas_model_and_avoid_follow_up_detection(self):
        echoed = self.llm.complete("List the follow-ups and follow up items")
        self.assertTrue(echoed.startswith("### Task:\n"))
        self.assertNotIn("follow", echoed.casefold())

    def test_structured_reply_tolerates_prose_and_code_fences(self):
        reply = self.llm.structured("MODE:json", api.AgentReply, api.AGENT_REPLY_FORMAT)
        self.assertEqual(reply.answer, "parsed")
        self.assertEqual(reply.changes[0].developer, "Sam")
        self.assertEqual(self.llm.structured("MODE:fenced", api.AgentReply, "{}").answer, "fenced ok")

    def test_invalid_reply_is_retried_once(self):
        self.assertEqual(self.llm.structured("MODE:bad", api.AgentReply, "{}").answer, "parsed")

    def test_timeout_raises_and_the_next_call_starts_fresh(self):
        with self.assertRaises(arka_llm.ArkaUnavailable):
            self.llm.client.call_tool("arka_ask", {"prompt": "MODE:slow"}, timeout=1)
        self.assertIn("### Task:", self.llm.complete("hello"))

    def test_crashed_server_raises_and_restarts(self):
        with self.assertRaises(arka_llm.ArkaUnavailable):
            self.llm.complete("MODE:crash")
        self.assertIn("again", self.llm.complete("again"))

    def test_arka_prompt_compaction_is_off_for_the_bot(self):
        # Arka's "Task IR" compaction rewrites long prompts and drops the bot's instructions
        env = {k: v for k, v in os.environ.items() if k != "ARKA_PROMPT_COMPACT"}
        with patch.dict(os.environ, env, clear=True):
            llm = fake_llm()
            self.addCleanup(llm.client.close)
            self.assertEqual(llm.complete("MODE:env"), "compact=0")

    def test_missing_arka_command_is_reported(self):
        broken = arka_llm.ArkaLLM(arka_llm.ArkaMCPClient(["definitely-not-arka-12345", "mcp", "serve"]))
        with self.assertRaises(arka_llm.ArkaUnavailable):
            broken.complete("hi")

    def test_parse_json_reply_rejects_text_without_json(self):
        with self.assertRaises(ValueError):
            arka_llm.parse_json_reply("no json here", api.AgentReply)


class ProviderSelectionTests(unittest.TestCase):
    def test_llm_provider_arka_selects_arka(self):
        with patch.object(api, "load_dotenv"), patch.dict(os.environ, {"LLM_PROVIDER": "arka"}):
            self.assertIsInstance(api._gemini_client(), arka_llm.ArkaLLM)

    def test_without_provider_or_key_there_is_no_model(self):
        env = {k: v for k, v in os.environ.items() if k not in ("LLM_PROVIDER", "GEMINI_API_KEY")}
        with patch.object(api, "load_dotenv"), patch.dict(os.environ, env, clear=True):
            self.assertIsNone(api._gemini_client())


if __name__ == "__main__":
    unittest.main()
