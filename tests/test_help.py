"""The help text must describe what SigmaAgent really does: every example has to reach its handler."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import falkordb

import github_analysis

with patch.object(falkordb, "FalkorDB", return_value=SimpleNamespace(select_graph=lambda _name: None)):
    import api

GUILD = "123456789012345678"
EXAMPLES = {example for _, items in api.CAPABILITIES for example, _ in items}


def answered_by_command(question):
    """True when a deterministic command (not Gemini) answers this question."""
    empty_graph = SimpleNamespace(ro_query=lambda *args, **kwargs: SimpleNamespace(result_set=[]))
    with patch.object(api, "tenant_graph", return_value=empty_graph), \
         patch.object(api, "_resolve_task", return_value=None), \
         patch.object(api, "_bottlenecks", return_value=([], [])), \
         patch.object(api.knowledge, "teams", return_value=[]), \
         patch.object(api.knowledge, "expertise", return_value=[]), \
         patch.object(api.knowledge, "refresh_expertise", return_value=0), \
         patch.object(api.knowledge, "experts_overview", return_value=[]), \
         patch.object(api.knowledge, "sync_repository", side_effect=github_analysis.GitHubUnavailable("offline")):
        handlers = [
            lambda q: api._explain_dependency_path(q, GUILD, []),
            lambda q: api._impact_answer(q, GUILD),
            lambda q: api._bottleneck_answer(q, GUILD),
            lambda q: api._expertise_answer(q, GUILD, {}),
            lambda q: api._teams_answer(q, GUILD),
            lambda q: api._sync_answer(q, GUILD),
        ]
        return any(handler(question) is not None for handler in handlers)


def is_github_question(question):
    return bool(github_analysis.GITHUB_QUERY_INTENT.search(question) or github_analysis._issue_number(question))


class HelpTests(unittest.TestCase):
    def test_help_questions_get_the_help_text(self):
        for question in ["help", "what can you do?", "Commands", "how do I use you?", "what can I ask you"]:
            self.assertTrue(api.HELP_RE.match(question), question)
        self.assertIsNone(api.HELP_RE.match("can you help me with billing?"))

    def test_help_fits_in_one_discord_message(self):
        # The bot splits replies at 1,900 characters; help shouldn't break mid-list
        self.assertLess(len("**SigmaAgent investigation**\n" + api.HELP_TEXT), 1900)

    def test_every_example_appears_in_help_and_prompt_text(self):
        plain = api._render_capabilities(markdown=False)
        for example in EXAMPLES:
            self.assertIn(f"`{example}`", api.HELP_TEXT)
            self.assertIn(f"'{example}'", plain)

    def test_command_examples_reach_their_command(self):
        for example in ["explain T5", "impact Backend", "bottlenecks", "who knows about billing?", "experts",
                        "teams", "sync github owner/repo"]:
            self.assertIn(example, EXAMPLES)
            self.assertTrue(answered_by_command(example), example)

    def test_github_examples_reach_the_github_router(self):
        for example in ["who are the contributors?", "what bugs are open?", "summarize issue #6"]:
            self.assertIn(example, EXAMPLES)
            self.assertTrue(is_github_question(example), example)

    def test_conversational_examples_reach_gemini(self):
        for example in ["what is blocked?", "what is Person 2 working on?", "Priya will do the frontend"]:
            self.assertIn(example, EXAMPLES)
            self.assertFalse(answered_by_command(example), example)
            self.assertFalse(is_github_question(example), example)
            self.assertIsNone(api.HELP_RE.match(example), example)

    def test_people_and_why_examples(self):
        self.assertTrue(api.PEOPLE_STATEMENT_RE.search("Naseem77 is Person 1 on github"))
        self.assertIn("why is T2 assigned to Priya?", EXAMPLES)
        self.assertRegex("why is T2 assigned to Priya?", r"(?i)\bwhy\b.*\bassigned\b")

    def test_bare_mention_asks_for_help(self):
        source = open("discord-bot/bot.py", encoding="utf-8").read()
        self.assertIn('question = "help"', source)


if __name__ == "__main__":
    unittest.main()
