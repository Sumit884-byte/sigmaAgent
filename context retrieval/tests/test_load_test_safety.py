from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from load_test_local import configure_load_graph


class LoadTestSafetyTests(unittest.TestCase):
    def test_default_load_graph_overrides_dev_graph_environment(self) -> None:
        with patch.dict(os.environ, {"FALKORDB_GRAPH": "SigmaPerson3Test"}):
            configure_load_graph("SigmaPerson3LoadTest")
            self.assertEqual(os.environ["FALKORDB_GRAPH"], "SigmaPerson3LoadTest")

    def test_development_graph_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            configure_load_graph("SigmaPerson3Test")


if __name__ == "__main__":
    unittest.main()
