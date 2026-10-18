"""Unit tests for knowledge.py and the knowledge commands in api.py (no database or network)."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import falkordb

import knowledge

with patch.object(falkordb, "FalkorDB", return_value=SimpleNamespace(select_graph=lambda _name: None)):
    import api


class Result:
    def __init__(self, rows):
        self.result_set = rows


class EvidenceGraph:
    """Returns canned evidence rows per search term, like EVIDENCE_QUERY would."""

    def __init__(self, rows_by_term):
        self.rows_by_term = rows_by_term
        self.terms = []

    def ro_query(self, query, params=None):
        self.terms.append(params["term"])
        return Result(self.rows_by_term.get(params["term"], []))


class MarkdownTests(unittest.TestCase):
    def test_sections_split_at_headings_and_ignore_code_fences(self):
        text = "Intro text\n# Setup\nInstall it\n```\n# not a heading\n```\n## Billing API\nCharges cards"
        sections = knowledge.split_markdown(text)
        self.assertEqual([s["heading"] for s in sections], ["", "Setup", "Billing API"])
        self.assertIn("# not a heading", sections[1]["content"])
        self.assertEqual(sections[2]["content"], "Charges cards")

    def test_badges_and_images_are_removed(self):
        text = "# Title\n[![build](https://x/badge.svg)](https://ci) ![logo](logo.png) Real text <b>bold</b>"
        self.assertEqual(knowledge.split_markdown(text)[0]["content"], "Real text bold")

    def test_github_anchor(self):
        self.assertEqual(knowledge._github_anchor("Sync / Async Parity"), "sync--async-parity")


class TextTests(unittest.TestCase):
    def test_search_terms_drop_stopwords_and_duplicates(self):
        self.assertEqual(knowledge.search_terms("Who knows about the Billing billing API?"), ["billing", "api"])

    def test_fulltext_query_has_no_operators(self):
        query = knowledge.fulltext_query("front-end -auth @billing (payments)")
        self.assertNotIn("-", query.replace("|", ""))
        self.assertEqual(query, "frontend|auth|billing|payments")

    def test_mentioned_tasks_by_id_and_whole_title(self):
        tasks = [("T1", "Auth"), ("T2", "Billing API"), ("T3", "UI")]
        self.assertEqual(knowledge.mentioned_tasks("Fixes T1 and the billing api; author tests", tasks), ["T1", "T2"])
        self.assertEqual(knowledge.mentioned_tasks("T9 is unrelated", tasks), [])

    def test_mentioned_people_whole_words(self):
        self.assertEqual(knowledge.mentioned_people("Ask Priya, not Priyanka", ["Priya", "Sam"]), ["Priya"])

    def test_path_topics_skip_generic_parts_and_formats(self):
        self.assertEqual(knowledge.path_topics("services/billing/invoice_api.py"), ["services", "billing", "invoice", "api"])
        self.assertEqual(knowledge.path_topics("src/uv.lock"), [])


class ExpertiseTests(unittest.TestCase):
    def test_phrase_counts_fully_and_single_words_half(self):
        graph = EvidenceGraph({
            "billing api": [[1, "Priya", "owns task", "T2: Billing API", "", 3]],
            "billing": [[2, "Sam", "opened issue", "#4 billing bug", "https://gh/4", 2]],
        })
        ranked = knowledge.expertise(graph, "the Billing API")
        self.assertEqual(graph.terms, ["billing api", "billing", "api"])
        self.assertEqual([(p["person"], p["score"]) for p in ranked], [("Priya", 3), ("Sam", 1)])

    def test_commit_evidence_is_capped_per_person(self):
        commits = [[7, "@dev", "commit", f"fix billing {i}", f"https://gh/c{i}", 1] for i in range(20)]
        ranked = knowledge.expertise(EvidenceGraph({"billing": commits}), "billing")
        self.assertEqual(ranked[0]["score"], knowledge.COMMIT_EVIDENCE_CAP)

    def test_same_evidence_found_by_two_terms_counts_once(self):
        row = [1, "Priya", "owns task", "T2: Billing API", "", 3]
        ranked = knowledge.expertise(EvidenceGraph({"billing api": [row], "billing": [row], "api": [row]}), "billing api")
        self.assertEqual(ranked[0]["score"], 3)
        self.assertEqual(len(ranked[0]["evidence"]), 1)


class CommandPatternTests(unittest.TestCase):
    def test_expertise_questions(self):
        for question, topic in [
            ("who knows about billing?", "billing"),
            ("Who is the expert on the payments service", "the payments service"),
            ("who should I ask about auth", "auth"),
            ("who's the go-to person for CI/CD?", "CI/CD"),
            ("experts on graph schema", "graph schema"),
        ]:
            match = api.EXPERTISE_RE.match(question)
            self.assertIsNotNone(match, question)
            self.assertEqual(match.group("topic"), topic)
        self.assertIsNone(api.EXPERTISE_RE.match("what is blocked?"))

    def test_overview_teams_and_sync(self):
        self.assertTrue(api.EXPERTS_OVERVIEW_RE.match("experts"))
        self.assertTrue(api.EXPERTS_OVERVIEW_RE.match("who knows what?"))
        self.assertEqual(api.TEAMS_RE.match("who is on the payments team?").group("team"), "payments")
        self.assertTrue(api.TEAMS_RE.match("teams"))
        self.assertEqual(api.SYNC_RE.match("sync github FalkorDB/falkordb-py").group("repo"), "FalkorDB/falkordb-py")
        self.assertIsNone(api.SYNC_RE.match("sync github").group("repo"))
        self.assertIsNone(api.SYNC_RE.match("please sync my calendar"))

    def test_team_membership_questions(self):
        match = api.TEAM_MEMBER_RE.match("is Sam on the payments team?")
        self.assertEqual((match.group("person"), match.group("team")), ("Sam", "payments"))
        self.assertTrue(api.TEAM_MEMBER_RE.match("are Sam and Priya in the platform team"))
        self.assertIsNone(api.TEAM_MEMBER_RE.match("Sam is on the payments team"))

    def test_people_statements_skip_the_github_router(self):
        for statement in ["Naseem77 is Person 1 on github", "my github username is sumit884",
                          "Meera will handle invoices and her github is meera-dev", "Sam is on github as sam-k",
                          "Priya joined the payments team"]:
            self.assertTrue(api.PEOPLE_STATEMENT_RE.search(statement), statement)
        self.assertFalse(api.PEOPLE_STATEMENT_RE.search("who are the contributors?"))

    def test_people_audit_clause_has_no_task(self):
        self.assertNotIn("t.id", api.PEOPLE_CHANGE_AUDIT_CLAUSE)
        self.assertNotIn("[:AFFECTS]->(t)", api.PEOPLE_CHANGE_AUDIT_CLAUSE)
        self.assertIn("MERGE (event:TaskChange", api.PEOPLE_CHANGE_AUDIT_CLAUSE)


if __name__ == "__main__":
    unittest.main()
