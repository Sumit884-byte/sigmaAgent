from __future__ import annotations

import os
import asyncio
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx

from evidence import explicit_task_reference
from graph_store import (
    connect_graph, import_messages, latest_message_id, retrieve_evidence,
    save_sync_progress, sync_progress,
)
from models import DiscordMessage
from api import app


@unittest.skipUnless(os.getenv("RUN_FALKORDB_INTEGRATION") == "1", "opt-in local FalkorDB integration")
class FalkorDbIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        graph_name = os.getenv("FALKORDB_INTEGRATION_GRAPH", "SigmaPerson3IntegrationTest")
        if not graph_name.startswith("SigmaPerson3IntegrationTest"):
            raise RuntimeError("integration graph must start with SigmaPerson3IntegrationTest")
        os.environ["FALKORDB_GRAPH"] = graph_name
        cls.graph = connect_graph()
        cls.suffix = uuid.uuid4().hex[:12]
        cls.channel_id = f"integration-{cls.suffix}"
        cls.task_id = f"IT-T2-{cls.suffix}"
        cls.stale_message_id = f"stale-{cls.suffix}"
        cls.graph.query(
            "MERGE (t:Task {id: $id}) SET t.title = $title",
            {"id": cls.task_id, "title": f"Build Integration Agent {cls.suffix}"},
        )

    def test_evidence_identity_cleanup_authors_and_sync_progress(self) -> None:
        source_url = f"https://discord.com/channels/g/{self.channel_id}/{self.stale_message_id}"
        blocker = DiscordMessage(
            id=self.stale_message_id, channel_id=self.channel_id, author_id=f"user-a-{self.suffix}",
            author_name="Shared Display Name", channel_name="integration",
            content=f"{self.task_id} is blocked by a missing key.", source_url=source_url,
        )
        same_name_other_user = DiscordMessage(
            id=f"author-b-{self.suffix}", channel_id=self.channel_id,
            author_id=f"user-b-{self.suffix}", author_name="Shared Display Name",
            channel_name="integration", content="The author is blocked by a deadline.",
        )
        import_messages(self.graph, [blocker, same_name_other_user])

        task_results = retrieve_evidence(self.graph, terms=[], task_id=self.task_id, limit=10)
        self.assertTrue(any(row["message_id"] == self.stale_message_id for row in task_results))
        self.assertIsNone(explicit_task_reference(
            same_name_other_user,
            [{"id": f"IT-AUTH-{self.suffix}", "title": "Auth"}],
            "The author is blocked by a deadline.",
        ))
        author_count = self.graph.query(
            "MATCH (a:Developer) WHERE a.discord_id IN $ids RETURN count(DISTINCT a.discord_id)",
            {"ids": [f"user-a-{self.suffix}", f"user-b-{self.suffix}"]},
        ).result_set[0][0]
        self.assertEqual(author_count, 2)

        blocker.content = "The key is available now."
        import_messages(self.graph, [blocker])
        stale_count = self.graph.query(
            "MATCH (:DiscordMessage {id: $id})-[:SUPPORTS]->(e:ContextEvidence) "
            "RETURN count(e)",
            {"id": self.stale_message_id},
        ).result_set[0][0]
        self.assertEqual(stale_count, 0)

        save_sync_progress(self.graph, self.channel_id, "100", "251")
        self.assertEqual(sync_progress(self.graph, self.channel_id), ("100", "251"))
        save_sync_progress(self.graph, self.channel_id, None, None)
        self.assertIsNone(sync_progress(self.graph, self.channel_id))

    def test_unique_legacy_developer_is_reused_for_discord_author(self) -> None:
        legacy_name = f"Legacy Assignee {self.suffix}"
        discord_id = f"legacy-discord-user-{self.suffix}"
        self.graph.query(
            "MERGE (d:Developer {name: $name}) REMOVE d.discord_id",
            {"name": legacy_name},
        )
        message_id = f"legacy-author-message-{self.suffix}"
        import_messages(self.graph, [DiscordMessage(
            id=message_id, channel_id=self.channel_id, author_id=discord_id,
            author_name=legacy_name, content="Waiting for review.",
        )])

        result = self.graph.query(
            "MATCH (d:Developer {name: $name}) RETURN count(d), "
            "sum(CASE WHEN d.discord_id = $discord_id THEN 1 ELSE 0 END)",
            {"name": legacy_name, "discord_id": discord_id},
        ).result_set[0]
        authored = self.graph.query(
            "MATCH (d:Developer {discord_id: $discord_id})-[:AUTHORED]->"
            "(:DiscordMessage {id: $message_id}) RETURN count(d)",
            {"discord_id": discord_id, "message_id": message_id},
        ).result_set[0][0]
        self.assertEqual(result, [1, 1])
        self.assertEqual(authored, 1)

    def test_authenticated_sync_and_task_retrieval_through_http_with_real_graph(self) -> None:
        channel_id = str(100_000_000_000_000_000 + uuid.uuid4().int % 800_000_000_000_000_000)
        base_id = 300_000_000_000_000_000 + uuid.uuid4().int % 600_000_000_000_000_000
        task_id = f"HTTP-T2-{self.suffix}"
        self.graph.query(
            "MERGE (t:Task {id: $id}) SET t.title = $title",
            {"id": task_id, "title": f"Unmatched Retrieval Title {self.suffix}"},
        )
        base_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
        import_messages(self.graph, [DiscordMessage(
            id=str(base_id), channel_id=channel_id, author_id=f"seed-{self.suffix}",
            author_name="Seed", timestamp=base_time.isoformat(),
            content="Existing high-water message.",
            source_url=f"https://discord.com/channels/g/{channel_id}/{base_id}",
        )])

        all_ids = list(range(base_id, base_id + 26))

        def fetch_page(_channel, limit, *, before=None, after=None):
            selected = all_ids
            if before is not None:
                selected = [value for value in selected if value < int(before)]
            if after is not None:
                selected = [value for value in selected if value > int(after)]
            return [{
                "id": str(value),
                "content": f"{task_id} is blocked by synthetic API access dependency.",
                "timestamp": (base_time + timedelta(seconds=value - base_id)).isoformat(),
                "author": {"id": f"author-{self.suffix}", "username": "Synthetic Author"},
            } for value in sorted(selected, reverse=after is None)[:limit]]

        async def exercise_api():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                sync_responses = []
                for _ in range(3):
                    sync_responses.append(await client.post(
                        "/context/sync-channel",
                        params={"channel_id": channel_id, "max_messages": 10},
                        headers={"X-Context-API-Key": test_key},
                    ))
                retrieval = await client.post(
                    "/context/retrieve",
                    json={
                        "feature_title": f"Unmatched Retrieval Title {self.suffix}",
                        "task_id": task_id,
                        "channel_id": channel_id,
                        "limit": 5,
                    },
                    headers={"X-Context-API-Key": test_key},
                )
                return sync_responses, retrieval

        test_key = "integration-secret-key-for-context-api-0123456789"
        with patch.dict(os.environ, {
            "CONTEXT_API_KEY": test_key,
            "DISCORD_ALLOWED_CHANNEL_IDS": channel_id,
        }), patch("api._fetch_channel", return_value={"id": channel_id, "guild_id": "333333333333333333"}), \
             patch("api._fetch_messages", side_effect=fetch_page):
            sync_responses, retrieval = asyncio.run(exercise_api())

        self.assertEqual([response.status_code for response in sync_responses], [200, 200, 200])
        self.assertEqual([response.json()["messages"] for response in sync_responses], [10, 10, 5])
        self.assertEqual([response.json()["sync_in_progress"] for response in sync_responses], [True, True, False])
        self.assertEqual(retrieval.status_code, 200, retrieval.text)
        evidence = retrieval.json()["evidence"]
        self.assertEqual(len(evidence), 5)
        self.assertTrue(all(item["task_id"] == task_id for item in evidence))
        self.assertTrue(all(item["source_url"].startswith("https://discord.com/") for item in evidence))
        self.assertEqual(latest_message_id(self.graph, channel_id), str(base_id + 25))


if __name__ == "__main__":
    unittest.main()
