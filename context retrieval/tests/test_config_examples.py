from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from graph_store import _discord_developer_map


class ExampleConfigurationTests(unittest.TestCase):
    def test_example_allowlist_uses_mock_channel_not_guild_id(self) -> None:
        root = Path(__file__).resolve().parents[1]
        mock_messages = json.loads((root / "mock_discord_history.json").read_text(encoding="utf-8"))
        example = (root / ".env.example").read_text(encoding="utf-8")
        allowed_line = next(
            line for line in example.splitlines()
            if line.startswith("DISCORD_ALLOWED_CHANNEL_IDS=")
        )
        allowed_ids = allowed_line.split("=", 1)[1].split(",")

        self.assertTrue(mock_messages)
        self.assertIn(mock_messages[0]["channel_id"], allowed_ids)
        self.assertNotIn(mock_messages[0]["guild_id"], allowed_ids)

    def test_explicit_discord_developer_map_parses_id_to_canonical_name(self) -> None:
        with patch.dict("os.environ", {
            "DISCORD_DEVELOPER_MAP": '{"123456789012345678":"Person Two"}',
        }):
            self.assertEqual(
                _discord_developer_map(), {"123456789012345678": "Person Two"}
            )

    def test_explicit_map_rejects_duplicate_developer_names(self) -> None:
        with patch.dict("os.environ", {
            "DISCORD_DEVELOPER_MAP": (
                '{"123456789012345678":"Person Two",'
                '"123456789012345679":"Person Two"}'
            ),
        }):
            with self.assertRaisesRegex(ValueError, "at most one Discord user"):
                _discord_developer_map()


if __name__ == "__main__":
    unittest.main()
