from __future__ import annotations

import unittest

from api import _collect_history


def page(items: list[int]) -> list[dict[str, str]]:
    return [{"id": str(item), "content": f"message-{item}"} for item in items]


class CursorAwareDiscord:
    """Fake Discord: `after` pages ascend from the oldest matching message."""

    def __init__(self, ids: range):
        self.ids = list(ids)
        self.calls = []

    def fetch(self, _channel, limit, *, before=None, after=None):
        self.calls.append((limit, before, after))
        selected = self.ids
        if before is not None:
            selected = [value for value in selected if value < int(before)]
        if after is not None:
            selected = [value for value in selected if value > int(after)]
        ordered = sorted(selected, reverse=after is None)
        return page(ordered[:limit])


class SyncPaginationTests(unittest.TestCase):
    def test_incremental_sync_resumes_capped_backlog_until_watermark(self) -> None:
        discord = CursorAwareDiscord(range(1, 351))
        saved_progress = None
        imported: list[str] = []
        pages = 0

        # Simulate repeated endpoint calls, each capped at 100 messages.
        for _ in range(5):
            messages, call_pages, next_baseline, next_cursor = _collect_history(
                "123456789012345678", 100, mode="incremental",
                latest_id="100", progress=saved_progress, fetch_page=discord.fetch,
            )
            pages += call_pages
            imported.extend(item["id"] for item in messages)
            saved_progress = (next_baseline, next_cursor) if next_cursor else None
            if saved_progress is None:
                break

        self.assertEqual(len(imported), 250)
        self.assertEqual(set(imported), {str(value) for value in range(101, 351)})
        self.assertEqual(len(imported), len(set(imported)))
        self.assertEqual(pages, 3)  # final page also crosses the saved watermark
        self.assertEqual(discord.calls[0][2], "100")
        self.assertEqual(discord.calls[1][1], None)
        self.assertEqual([call[2] for call in discord.calls], ["100", "200", "300"])
        self.assertIsNone(saved_progress)

    def test_backfill_pages_older_messages_and_obeys_limit(self) -> None:
        discord = CursorAwareDiscord(range(1, 101))
        messages, page_count, _, _ = _collect_history(
            "123456789012345678", 120, mode="backfill", fetch_page=discord.fetch
        )

        self.assertEqual(len(messages), 100)
        self.assertEqual(page_count, 2)
        self.assertEqual(discord.calls, [(100, None, None), (20, "1", None)])
        self.assertEqual(messages[0]["id"], "1")

    def test_initial_incremental_bootstrap_is_bounded(self) -> None:
        discord = CursorAwareDiscord(range(1, 351))
        messages, _, baseline, cursor = _collect_history(
            "123456789012345678", 75, mode="incremental", fetch_page=discord.fetch
        )
        self.assertEqual(len(messages), 75)
        self.assertIsNone(baseline)
        self.assertIsNone(cursor)
        self.assertEqual(discord.calls, [(75, None, None)])


if __name__ == "__main__":
    unittest.main()
