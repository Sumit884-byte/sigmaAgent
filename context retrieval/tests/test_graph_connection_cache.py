from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import graph_store


class QueryResult:
    def __init__(self):
        self.result_set = []


class FakeGraph:
    def __init__(self):
        self.queries = []

    def query(self, query, params=None):
        self.queries.append(query)
        return QueryResult()


class GraphConnectionCacheTests(unittest.TestCase):
    def test_connection_and_index_discovery_are_cached_per_configuration(self) -> None:
        graph = FakeGraph()
        graph_store._GRAPH_CACHE.clear()
        with patch.dict(os.environ, {
            "FALKORDB_HOST": "cache-test-host",
            "FALKORDB_PORT": "6380",
            "FALKORDB_GRAPH": "SigmaPerson3CacheTest",
            "FALKORDB_USERNAME": "",
            "FALKORDB_PASSWORD": "",
        }), patch("graph_store.FalkorDB") as driver:
            driver.return_value.select_graph.return_value = graph
            first = graph_store.connect_graph()
            second = graph_store.connect_graph()

        self.assertIs(first, second)
        driver.assert_called_once()
        self.assertEqual(graph.queries.count("CALL db.indexes()"), 1)
        self.assertTrue(any(
            "CREATE INDEX FOR (n:Developer) ON (n.name)" in query
            for query in graph.queries
        ))
        graph_store._GRAPH_CACHE.clear()


if __name__ == "__main__":
    unittest.main()
