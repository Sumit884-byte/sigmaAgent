from __future__ import annotations

import unittest

from graph_store import BATCH_SIZE, import_messages, retrieve_evidence
from models import DiscordMessage


class QueryResult:
    def __init__(self, result_set):
        self.result_set = result_set


class RecordingGraph:
    def __init__(self):
        self.queries = []

    def query(self, query, params=None):
        self.queries.append((query, params or {}))
        if "MATCH (t:Task) RETURN t.id, t.title" in query:
            return QueryResult([["T2", "Build LLM Agent"]])
        if "CALL db.indexes()" in query:
            return QueryResult([])
        return QueryResult([])


class ImporterTests(unittest.TestCase):
    def test_import_uses_merge_and_only_links_explicitly_named_task(self) -> None:
        graph = RecordingGraph()
        messages = [
            DiscordMessage(
                id="m1", channel_id="c1", author_name="Dev",
                content="T2 is blocked by a missing API key.",
                source_url="https://discord.com/channels/g1/c1/m1",
            ),
            DiscordMessage(
                id="m2", channel_id="c1", author_name="Dev",
                content="The release is waiting for approval.",
                source_url="https://discord.com/channels/g1/c1/m2",
            ),
        ]

        counts = import_messages(graph, messages)

        self.assertEqual(counts, {"messages": 2, "evidence": 2, "task_links": 1})
        writes = [(query, params) for query, params in graph.queries if "UNWIND $items AS item" in query]
        self.assertEqual(len(writes), 5)
        self.assertTrue(any("FOREACH" in query and "item.unambiguous" in query for query, _ in writes))
        self.assertTrue(any("MERGE (a:Developer {discord_id: item.author_id})" in query
                            for query, _ in writes))

    def test_reimport_is_safe_to_repeat_via_stable_merge_keys(self) -> None:
        graph = RecordingGraph()
        message = DiscordMessage(
            id="m1", channel_id="c1", author_name="Dev",
            content="T2 is blocked by a missing API key.",
            source_url="https://discord.com/channels/g1/c1/m1",
        )

        first = import_messages(graph, [message])
        second = import_messages(graph, [message])

        self.assertEqual(first, second)

    def test_large_import_uses_bounded_batches(self) -> None:
        graph = RecordingGraph()
        messages = [
            DiscordMessage(id=f"m{i}", channel_id="c1", author_name="Dev", content="ordinary update")
            for i in range(1001)
        ]
        counts = import_messages(graph, messages)
        write_batches = [
            params["items"] for query, params in graph.queries
            if "MERGE (m:DiscordMessage" in query
        ]

        self.assertEqual(counts["messages"], 1001)
        self.assertEqual(len(write_batches), 5)  # ceil(1001 / 250) message batches; no evidence
        self.assertTrue(all(len(batch) <= BATCH_SIZE for batch in write_batches))

    def test_retrieval_returns_original_discord_source(self) -> None:
        class EvidenceGraph:
            def query(self, query, params=None):
                self.query_text = query
                self.params = params or {}
                if "CALL db.indexes()" in query:
                    return QueryResult([])
                return QueryResult(
                    [[
                        "m1", "T2 is blocked by a missing API key.", "2026-10-15T10:30:00Z",
                        "https://discord.com/channels/g1/c1/m1", "Srushti", "c1",
                        "project-updates", "blocker_candidate",
                        "T2 is blocked by a missing API key.", "T2", "Build LLM Agent", 1.25,
                    ]]
                )

        graph = EvidenceGraph()
        results = retrieve_evidence(graph, terms=["T2", "API", "key"], task_id="T2", limit=5)

        self.assertEqual(results[0]["source_url"], "https://discord.com/channels/g1/c1/m1")
        self.assertEqual(results[0]["evidence_kind"], "blocker_candidate")
        self.assertEqual(graph.params["task_id"], "T2")
        self.assertIn("db.idx.fulltext.queryNodes", graph.query_text)
        self.assertEqual(graph.params["search"], "T2|API|key")

    def test_task_id_alone_queries_linked_evidence(self) -> None:
        class TaskGraph:
            def query(self, query, params=None):
                self.query_text = query
                self.params = params or {}
                return QueryResult([[
                    "m-task", "T2 is blocked by a key.", "2026-10-15T10:30:00Z",
                    "https://discord.com/channels/g1/c1/m-task", "Dev", "c1",
                    "project", "blocker_candidate", "T2 is blocked by a key.",
                    "T2", "Build LLM Agent", 1.0,
                ]])

        graph = TaskGraph()
        results = retrieve_evidence(graph, terms=[], task_id="T2", limit=10)
        self.assertEqual(results[0]["task_id"], "T2")
        self.assertIn("ABOUT", graph.query_text)
        self.assertEqual(graph.params["task_id"], "T2")

    def test_import_keys_authors_by_discord_id(self) -> None:
        graph = RecordingGraph()
        messages = [
            DiscordMessage(id="m-a", channel_id="c1", author_id="user-a", author_name="Same Name"),
            DiscordMessage(id="m-b", channel_id="c1", author_id="user-b", author_name="Same Name"),
        ]
        import_messages(graph, messages)
        write_queries = [query for query, _ in graph.queries if "UNWIND $items AS item" in query]
        self.assertTrue(any("MERGE (a:Developer {discord_id: item.author_id})" in query
                            for query in write_queries))
        self.assertTrue(any("item.unambiguous" in query for query in write_queries))

    def test_reimport_prunes_evidence_from_edited_message(self) -> None:
        graph = RecordingGraph()
        message = DiscordMessage(
            id="edited", channel_id="c1", author_id="user1", author_name="Dev",
            content="T2 is blocked by a missing key.",
        )
        import_messages(graph, [message])
        message.content = "The key is available now."
        import_messages(graph, [message])
        cleanup_batches = [
            params["items"] for query, params in graph.queries
            if "DETACH DELETE e" in query
        ]
        self.assertEqual(cleanup_batches[-1], [{"message_id": "edited", "evidence_ids": []}])


if __name__ == "__main__":
    unittest.main()
