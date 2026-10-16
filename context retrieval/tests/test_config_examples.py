from __future__ import annotations

import json
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
