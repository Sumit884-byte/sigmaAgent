"""Batched FalkorDB persistence and indexed retrieval for Discord context."""

from __future__ import annotations

import os
import re
import threading
from collections import Counter
from typing import Any, Iterable

from falkordb import FalkorDB

from evidence import explicit_task_reference, extract_evidence
from models import DiscordMessage

BATCH_SIZE = 250
RANGE_INDEXES = {
    "DiscordChannel": ("id",),
    "DiscordMessage": ("id", "channel_id", "timestamp"),
    "Developer": ("discord_id",),
    "ContextEvidence": ("id",),
    "Task": ("id",),
    "DiscordSyncState": ("channel_id",),
}
_GRAPH_CACHE: dict[tuple[str, int, str | None, str | None, str], Any] = {}
_GRAPH_CACHE_LOCK = threading.Lock()


def connect_graph() -> Any:
    options: dict[str, Any] = {
        "host": os.getenv("FALKORDB_HOST", "127.0.0.1"),
        "port": int(os.getenv("FALKORDB_PORT", "6379")),
    }
    username = os.getenv("FALKORDB_USERNAME")
    password = os.getenv("FALKORDB_PASSWORD")
    if username:
        options["username"] = username
    if password:
        options["password"] = password
    graph_name = os.getenv("FALKORDB_GRAPH", "SigmaPerson3Test")
    key = (options["host"], options["port"], username, password, graph_name)
    with _GRAPH_CACHE_LOCK:
        if key not in _GRAPH_CACHE:
            graph = FalkorDB(**options).select_graph(graph_name)
            ensure_indexes(graph)
            _GRAPH_CACHE[key] = graph
        return _GRAPH_CACHE[key]


def ensure_indexes(graph: Any) -> None:
    """Create the range/full-text indexes this service relies on, if missing."""
    result = graph.query("CALL db.indexes()")
    indexed: set[tuple[str, str, str]] = set()
    for row in result.result_set:
        if len(row) < 3:
            continue
        label = str(row[0])
        props = row[1] if isinstance(row[1], (list, tuple)) else []
        types = row[2] if isinstance(row[2], dict) else {}
        for prop in props:
            raw_type = types.get(prop, ["RANGE"])
            if isinstance(raw_type, (list, tuple)):
                index_types = raw_type
            else:
                index_types = [raw_type]
            for index_type in index_types:
                indexed.add((label, str(prop), str(index_type).upper()))
    for label, properties in RANGE_INDEXES.items():
        for prop in properties:
            if (label, prop, "RANGE") not in indexed:
                _create_index(graph, f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
    if ("DiscordMessage", "content", "FULLTEXT") not in indexed:
        _create_index(graph, "CALL db.idx.fulltext.createNodeIndex('DiscordMessage', 'content')")


def _create_index(graph: Any, query: str) -> None:
    try:
        graph.query(query)
    except Exception as exc:
        if "already indexed" not in str(exc).casefold() and "index already exists" not in str(exc).casefold():
            raise


def task_catalog(graph: Any) -> list[dict[str, str]]:
    """Read Person 2's shared Task nodes; do not create or modify them here."""
    result = graph.query("MATCH (t:Task) RETURN t.id, t.title")
    return [{"id": str(row[0]), "title": str(row[1])} for row in result.result_set if row[0] and row[1]]


def _chunks(items: list[Any], size: int = BATCH_SIZE) -> Iterable[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def import_messages(graph: Any, messages: list[DiscordMessage]) -> dict[str, int]:
    """Idempotently import messages and evidence using bounded bulk queries."""
    if not messages:
        return {"messages": 0, "evidence": 0, "task_links": 0}
    tasks = task_catalog(graph)
    base_items = [
        {
            "channel_id": m.channel_id, "channel_name": m.channel_name,
            "guild_id": m.guild_id, "guild_name": m.guild_name,
            "message_id": m.id, "content": m.content, "timestamp": m.timestamp,
            "source_url": m.source_url, "author_name": m.author_name,
            "author_id": m.author_id or f"unknown:{m.author_name.casefold()}",
        }
        for m in messages
    ]
    author_items_by_id = {
        item["author_id"]: {
            "author_id": item["author_id"],
            "author_name": item["author_name"],
        }
        for item in base_items
    }
    author_name_counts = Counter(item["author_name"] for item in author_items_by_id.values())
    author_items = [
        {
            **item,
            # Reuse a legacy name-only Developer only when the name identifies
            # one Discord user in this import batch.
            "unambiguous": author_name_counts[item["author_name"]] == 1,
        }
        for item in author_items_by_id.values()
    ]
    legacy_author_query = """
    UNWIND $items AS item
    OPTIONAL MATCH (identified:Developer {discord_id: item.author_id})
    WITH item, count(identified) AS exact_matches
    OPTIONAL MATCH (legacy:Developer {name: item.author_name})
    WHERE exact_matches = 0
      AND (legacy.discord_id IS NULL OR legacy.discord_id = item.author_id)
    WITH item, exact_matches, collect(legacy) AS candidates
    FOREACH (legacy IN CASE
        WHEN exact_matches = 0 AND item.unambiguous AND size(candidates) = 1
        THEN candidates ELSE [] END |
        SET legacy.discord_id = item.author_id)
    """
    for batch in _chunks(author_items):
        graph.query(legacy_author_query, {"items": batch})
    base_query = """
    UNWIND $items AS item
    MERGE (c:DiscordChannel {id: item.channel_id})
    SET c.name = item.channel_name, c.guild_id = item.guild_id, c.guild_name = item.guild_name
    MERGE (m:DiscordMessage {id: item.message_id})
    SET m.channel_id = item.channel_id, m.content = item.content,
        m.timestamp = item.timestamp, m.source_url = item.source_url
    MERGE (m)-[:IN_CHANNEL]->(c)
    """
    for batch in _chunks(base_items):
        graph.query(base_query, {"items": batch})
    author_query = """
    UNWIND $items AS item
    MATCH (m:DiscordMessage {id: item.message_id})
    MERGE (a:Developer {discord_id: item.author_id})
    SET a.name = item.author_name
    MERGE (a)-[:AUTHORED]->(m)
    """
    for batch in _chunks(base_items):
        graph.query(author_query, {"items": batch})

    evidence_items: list[dict[str, Any]] = []
    for message in messages:
        for evidence in extract_evidence(message):
            evidence_items.append({
                "message_id": message.id, "evidence_id": evidence.id,
                "kind": evidence.kind, "excerpt": evidence.excerpt,
                "source_url": evidence.source_url,
                "task_id": explicit_task_reference(message, tasks, evidence.excerpt),
            })
    evidence_by_message: dict[str, list[str]] = {message.id: [] for message in messages}
    for item in evidence_items:
        evidence_by_message[item["message_id"]].append(item["evidence_id"])
    cleanup_items = [
        {"message_id": message.id, "evidence_ids": evidence_by_message[message.id]}
        for message in messages
    ]
    cleanup_query = """
    UNWIND $items AS item
    MATCH (m:DiscordMessage {id: item.message_id})-[:SUPPORTS]->(e:ContextEvidence)
    WHERE NOT e.id IN item.evidence_ids
    DETACH DELETE e
    """
    for batch in _chunks(cleanup_items):
        graph.query(cleanup_query, {"items": batch})
    evidence_query = """
    UNWIND $items AS item
    MATCH (m:DiscordMessage {id: item.message_id})
    MERGE (e:ContextEvidence {id: item.evidence_id})
    SET e.kind = item.kind, e.excerpt = item.excerpt, e.source_url = item.source_url
    MERGE (m)-[:SUPPORTS]->(e)
    WITH e, item
    OPTIONAL MATCH (e)-[old:ABOUT]->(old_task:Task)
    WHERE item.task_id IS NULL OR old_task.id <> item.task_id
    DELETE old
    WITH DISTINCT e, item
    OPTIONAL MATCH (task:Task {id: item.task_id})
    FOREACH (_ IN CASE WHEN task IS NULL THEN [] ELSE [1] END |
        MERGE (e)-[:ABOUT]->(task))
    """
    for batch in _chunks(evidence_items):
        graph.query(evidence_query, {"items": batch})
    return {
        "messages": len(messages),
        "evidence": len(evidence_items),
        "task_links": sum(1 for item in evidence_items if item["task_id"]),
    }


def latest_message_id(graph: Any, channel_id: str) -> str | None:
    result = graph.query(
        "MATCH (m:DiscordMessage {channel_id: $channel_id}) "
        "RETURN m.id ORDER BY m.timestamp DESC LIMIT 1",
        {"channel_id": channel_id},
    )
    return str(result.result_set[0][0]) if result.result_set and result.result_set[0][0] else None


def oldest_message_id(graph: Any, channel_id: str) -> str | None:
    result = graph.query(
        "MATCH (m:DiscordMessage {channel_id: $channel_id}) "
        "RETURN m.id ORDER BY m.timestamp ASC LIMIT 1",
        {"channel_id": channel_id},
    )
    return str(result.result_set[0][0]) if result.result_set and result.result_set[0][0] else None


def sync_progress(graph: Any, channel_id: str) -> tuple[str | None, str | None] | None:
    result = graph.query(
        "MATCH (s:DiscordSyncState {channel_id: $channel_id}) "
        "RETURN s.baseline_id, s.cursor_id LIMIT 1",
        {"channel_id": channel_id},
    )
    if not result.result_set:
        return None
    row = result.result_set[0]
    return (str(row[0]) if row[0] else None, str(row[1]) if row[1] else None)


def save_sync_progress(
    graph: Any, channel_id: str, baseline_id: str | None, cursor_id: str | None
) -> None:
    if cursor_id is None:
        graph.query(
            "MATCH (s:DiscordSyncState {channel_id: $channel_id}) DETACH DELETE s",
            {"channel_id": channel_id},
        )
        return
    graph.query(
        "MERGE (s:DiscordSyncState {channel_id: $channel_id}) "
        "SET s.baseline_id = $baseline_id, s.cursor_id = $cursor_id",
        {"channel_id": channel_id, "baseline_id": baseline_id, "cursor_id": cursor_id},
    )


def _fulltext_query(terms: list[str]) -> str:
    tokens: list[str] = []
    for term in terms:
        for token in re.findall(r"\w[\w-]{1,}", term, flags=re.UNICODE):
            if token.casefold() not in {t.casefold() for t in tokens}:
                tokens.append(token)
    return "|".join(tokens[:64])


def retrieve_evidence(
    graph: Any,
    *,
    terms: list[str],
    task_id: str | None = None,
    channel_id: str | None = None,
    channel_ids: list[str] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Search indexed message text and task-linked evidence, preserving sources."""
    rows: list[Any] = []
    if task_id:
        task_result = graph.query(
            """
            MATCH (task:Task {id: $task_id})<-[:ABOUT]-(e:ContextEvidence)
                  <-[:SUPPORTS]-(m:DiscordMessage)
            OPTIONAL MATCH (author:Developer)-[:AUTHORED]->(m)
            OPTIONAL MATCH (m)-[:IN_CHANNEL]->(channel:DiscordChannel)
            WITH task, e, m, author, channel
            WHERE ($channel_id IS NULL OR channel.id = $channel_id)
              AND ($channel_ids IS NULL OR channel.id IN $channel_ids)
            RETURN m.id, m.content, m.timestamp, m.source_url,
                   author.name, channel.id, channel.name,
                   e.kind, e.excerpt, task.id, task.title, 1.0
            ORDER BY m.timestamp DESC
            LIMIT $limit
            """,
            {"task_id": task_id, "channel_id": channel_id,
             "channel_ids": channel_ids, "limit": limit},
        )
        rows.extend(task_result.result_set)

    search = _fulltext_query(terms)
    if search:
        result = graph.query(
            """
            CALL db.idx.fulltext.queryNodes('DiscordMessage', $search) YIELD node, score
            WITH node AS m, score
            OPTIONAL MATCH (m)-[:SUPPORTS]->(e:ContextEvidence)
            OPTIONAL MATCH (author:Developer)-[:AUTHORED]->(m)
            OPTIONAL MATCH (m)-[:IN_CHANNEL]->(channel:DiscordChannel)
            OPTIONAL MATCH (e)-[:ABOUT]->(task:Task)
            WITH m, e, author, channel, task, score
            WHERE ($task_id IS NULL OR task.id = $task_id)
              AND ($channel_id IS NULL OR channel.id = $channel_id)
              AND ($channel_ids IS NULL OR channel.id IN $channel_ids)
            RETURN m.id, m.content, m.timestamp, m.source_url,
                   author.name, channel.id, channel.name,
                   e.kind, e.excerpt, task.id, task.title, score
            ORDER BY score DESC, m.timestamp DESC
            LIMIT $limit
            """,
            {"search": search, "task_id": task_id, "channel_id": channel_id,
             "channel_ids": channel_ids, "limit": limit},
        )
        rows.extend(result.result_set)

    unique_rows: dict[tuple[Any, Any], Any] = {}
    for row in rows:
        unique_rows[(row[0], row[8])] = row
    rows = sorted(
        unique_rows.values(),
        key=lambda row: (
            bool(task_id and row[9] == task_id),
            float(row[11] or 0),
            str(row[2] or ""),
        ),
        reverse=True,
    )[:limit]
    return [
        {
            "message_id": row[0], "content": row[1], "timestamp": row[2],
            "source_url": row[3], "author": row[4], "channel_id": row[5],
            "channel_name": row[6], "evidence_kind": row[7],
            "evidence_excerpt": row[8], "task_id": row[9], "task_title": row[10],
            "relevance_score": row[11],
        }
        for row in rows
    ]
