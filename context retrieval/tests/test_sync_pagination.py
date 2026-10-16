from __future__ import annotations

import unittest

from api import _collect_history


def page(items: list[int]) -> list[dict[str, str]]:
    return [{"id": str(item), "content": f"message-{item}"} for item in items]


class SyncPaginationTests(unittest.TestCase):
    def test_backfill_pages_older_messages_and_obeys_limit(self) -> None:
        calls = []
        pages = {None: page(list(range(100, 0, -1))), "1": page([])}

        def fetch(_channel, limit, *, before=None, after=None):
            calls.append((limit, before, after))
            return pages.get(before, [])[:limit]

        messages, page_count = _collect_history(
            "channel", 120, mode="backfill", fetch_page=fetch
        )

        self.assertEqual(len(messages), 100)
        self.assertEqual(page_count, 2)
        self.assertEqual(calls[0], (100, None, None))
        self.assertEqual(calls[1][1], "1")
        self.assertEqual(messages[0]["id"], "1")

    def test_incremental_sync_stops_at_saved_watermark(self) -> None:
        calls = []
        response = page([110, 109, 108, 107, 106, 105, 104, 103, 102, 101, 100, 99])

        def fetch(_channel, limit, *, before=None, after=None):
            calls.append((limit, before, after))
            return response[:limit]

        messages, page_count = _collect_history(
            "channel", 100, mode="incremental", latest_id="100", fetch_page=fetch
        )

        self.assertEqual([item["id"] for item in messages], ["101", "102", "103", "104", "105", "106", "107", "108", "109", "110"])
        self.assertEqual(page_count, 1)
        self.assertEqual(calls[0][2], "100")

    def test_history_window_is_capped_by_requested_message_count(self) -> None:
        calls = []

        def fetch(_channel, limit, *, before=None, after=None):
            calls.append(limit)
            start = 1000 if before is None else int(before) - 1
            return page(list(range(start, start - limit, -1)))

        messages, _ = _collect_history("channel", 175, mode="backfill", fetch_page=fetch)
        self.assertEqual(len(messages), 175)
        self.assertEqual(calls, [100, 75])


if __name__ == "__main__":
    unittest.main()
