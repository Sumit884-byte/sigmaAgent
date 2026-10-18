from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import httpx

from api import app

TEST_API_KEY = "test-secret-key-for-context-api-tests-0123456789"


class ApiSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_channel_routes_require_auth_and_allowlisted_snowflake(self) -> None:
        channel_id = "123456789012345678"
        with patch.dict(os.environ, {
            "CONTEXT_API_KEY": TEST_API_KEY,
            "DISCORD_ALLOWED_CHANNEL_IDS": channel_id,
        }):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                unauthenticated = await client.get(
                    "/context/channel-history",
                    params={"channel_id": channel_id, "query": "blocker"},
                )
                self.assertEqual(unauthenticated.status_code, 401)

                invalid_id = await client.get(
                    "/context/channel-history",
                    params={"channel_id": "not-a-snowflake", "query": "blocker"},
                    headers={"X-Context-API-Key": TEST_API_KEY},
                )
                self.assertEqual(invalid_id.status_code, 422)

                disallowed = await client.get(
                    "/context/channel-history",
                    params={"channel_id": "123456789012345679", "query": "blocker"},
                    headers={"X-Context-API-Key": TEST_API_KEY},
                )
                self.assertEqual(disallowed.status_code, 403)

                sync_unauthenticated = await client.post(
                    "/context/sync-channel", params={"channel_id": channel_id}
                )
                self.assertEqual(sync_unauthenticated.status_code, 401)

    async def test_authenticated_allowlisted_channel_history_works(self) -> None:
        channel_id = "123456789012345678"
        message_id = "223456789012345678"
        from unittest.mock import patch as mock_patch

        with patch.dict(os.environ, {
            "CONTEXT_API_KEY": TEST_API_KEY,
            "DISCORD_ALLOWED_CHANNEL_IDS": channel_id,
        }), mock_patch("api._fetch_channel", return_value={"guild_id": "323456789012345678"}), \
             mock_patch("api._fetch_messages", return_value=[{
                 "id": message_id,
                 "content": "A blocker is waiting for API access.",
                 "timestamp": "2026-10-16T00:00:00Z",
                 "author": {"username": "Dev"},
             }]):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get(
                    "/context/channel-history",
                    params={"channel_id": channel_id, "query": "blocker"},
                    headers={"X-Context-API-Key": TEST_API_KEY},
                )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["message_id"], message_id)

    async def test_incremental_http_sync_resumes_across_capped_requests(self) -> None:
        channel_id = "123456789012345678"
        ids = list(range(1, 351))
        imported_ids: list[str] = []
        progress = [("100", "200"), ("100", "300")]

        def fetch_messages(_channel, limit, *, before=None, after=None):
            selected = ids
            if before is not None:
                selected = [value for value in selected if value < int(before)]
            if after is not None:
                selected = [value for value in selected if value > int(after)]
            return [{"id": str(value), "content": "T2 is blocked by API access."}
                    for value in sorted(selected, reverse=after is None)[:limit]]

        def fake_import(_graph, messages):
            imported_ids.extend(message.id for message in messages)
            return {"messages": len(messages), "evidence": len(messages), "task_links": len(messages)}

        with patch.dict(os.environ, {
            "CONTEXT_API_KEY": TEST_API_KEY,
            "DISCORD_ALLOWED_CHANNEL_IDS": channel_id,
        }), patch("api._fetch_channel", return_value={"id": channel_id, "guild_id": "323456789012345678"}), \
             patch("api._fetch_messages", side_effect=fetch_messages), \
             patch("api.connect_graph", return_value=object()), \
             patch("api.sync_watermark", return_value="100"), \
             patch("api.oldest_message_id", return_value="100"), \
             patch("api.sync_progress", side_effect=[None, progress[0], progress[1]]), \
             patch("api.import_messages", side_effect=fake_import), \
             patch("api.save_sync_progress") as save_progress, \
             patch("api.save_sync_watermark") as save_watermark:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                responses = []
                for _ in range(3):
                    responses.append(await client.post(
                        "/context/sync-channel",
                        params={"channel_id": channel_id, "max_messages": 100},
                        headers={"X-Context-API-Key": TEST_API_KEY},
                    ))

        self.assertTrue(
            all(response.status_code == 200 for response in responses),
            [response.text for response in responses],
        )
        self.assertEqual([response.json()["messages"] for response in responses], [100, 100, 50])
        self.assertEqual([response.json()["sync_in_progress"] for response in responses], [True, True, False])
        self.assertEqual(set(imported_ids), {str(value) for value in range(101, 351)})
        self.assertEqual(len(imported_ids), len(set(imported_ids)))
        self.assertEqual(save_progress.call_count, 3)
        # The sync-only watermark advances once, when the catch-up finishes, to the newest synced message
        save_watermark.assert_called_once()
        self.assertEqual(save_watermark.call_args.args[1:], (channel_id, "350"))

    async def test_retrieve_rejects_malformed_channel_id_via_request_model(self) -> None:
        with patch.dict(os.environ, {
            "CONTEXT_API_KEY": TEST_API_KEY,
            "DISCORD_ALLOWED_CHANNEL_IDS": "123456789012345678",
        }):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/context/retrieve",
                    json={"feature_title": "Test", "channel_id": "not-a-channel"},
                    headers={"X-Context-API-Key": TEST_API_KEY},
                )
        self.assertEqual(response.status_code, 422)

    async def test_database_errors_are_sanitized_from_api_responses(self) -> None:
        channel_id = "123456789012345678"
        with patch.dict(os.environ, {
            "CONTEXT_API_KEY": TEST_API_KEY,
            "DISCORD_ALLOWED_CHANNEL_IDS": channel_id,
        }), patch("api.connect_graph", side_effect=RuntimeError("private database credential")), \
             patch("api.logger.exception"):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/context/retrieve",
                    json={"feature_title": "Test feature"},
                    headers={"X-Context-API-Key": TEST_API_KEY},
                )
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private database credential", response.text)

    async def test_sync_database_error_is_sanitized(self) -> None:
        channel_id = "123456789012345678"
        with patch.dict(os.environ, {
            "CONTEXT_API_KEY": TEST_API_KEY,
            "DISCORD_ALLOWED_CHANNEL_IDS": channel_id,
        }), patch("api._fetch_channel", return_value={"id": channel_id}), \
             patch("api.connect_graph", return_value=object()), \
             patch("api.sync_watermark", side_effect=RuntimeError("private database credential")), \
             patch("api.logger.exception"):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/context/sync-channel",
                    params={"channel_id": channel_id},
                    headers={"X-Context-API-Key": TEST_API_KEY},
                )
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private database credential", response.text)


if __name__ == "__main__":
    unittest.main()


class NonAsciiKeyTests(unittest.TestCase):
    def test_non_ascii_api_key_is_rejected_not_a_crash(self):
        from fastapi import HTTPException

        from api import require_context_api_key

        with patch.dict(os.environ, {"CONTEXT_API_KEY": "k" * 40}):
            with self.assertRaises(HTTPException) as raised:
                require_context_api_key("café-" + "k" * 35)
        self.assertEqual(raised.exception.status_code, 401)
