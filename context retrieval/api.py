"""Read-only Discord context search with a write-only history importer."""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode
from typing import Any

from fastapi import FastAPI, HTTPException, Query

from graph_store import (
    connect_graph, import_messages, latest_message_id, oldest_message_id,
    retrieve_evidence,
)
from models import ContextRequest, DiscordMessage

app = FastAPI(title="Sigma Agent Context Retrieval API", version="0.2.0")
DISCORD_API = "https://discord.com/api/v10"
MAX_MESSAGES = 100
MAX_SYNC_MESSAGES = 10_000
MAX_SYNC_PAGES = MAX_SYNC_MESSAGES // MAX_MESSAGES


def _discord_request(path: str, retries: int = 4) -> Any:
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN is not configured")
    request = urllib.request.Request(
        f"{DISCORD_API}{path}",
        headers={"Authorization": f"Bot {token}", "Accept": "application/json"},
    )
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise PermissionError("Discord rejected the bot token or channel access") from exc
            if exc.code == 404:
                raise LookupError("Discord channel was not found") from exc
            if exc.code == 429 and attempt < retries:
                try:
                    delay = min(float(exc.headers.get("Retry-After", "1")), 30.0)
                except ValueError:
                    delay = 1.0
                time.sleep(max(delay, 0.1))
                continue
            if exc.code >= 500 and attempt < retries:
                time.sleep(min(0.5 * (2 ** attempt), 4.0))
                continue
            raise RuntimeError(f"Discord API returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            if attempt < retries:
                time.sleep(min(0.5 * (2 ** attempt), 4.0))
                continue
            raise RuntimeError("Could not reach the Discord API") from exc


def _fetch_channel(channel_id: str) -> dict[str, Any]:
    channel = _discord_request(f"/channels/{channel_id}")
    if not isinstance(channel, dict):
        raise RuntimeError("Discord returned an unexpected channel response")
    return channel


def _fetch_messages(channel_id: str, limit: int, *, before: str | None = None,
                    after: str | None = None) -> list[dict[str, Any]]:
    params = {"limit": str(limit)}
    if before:
        params["before"] = before
    if after:
        params["after"] = after
    payload = _discord_request(f"/channels/{channel_id}/messages?{urlencode(params)}")
    if not isinstance(payload, list):
        raise RuntimeError("Discord returned an unexpected message response")
    return payload


def _collect_history(channel_id: str, max_messages: int, *, mode: str,
                     latest_id: str | None = None,
                     oldest_id: str | None = None,
                     fetch_page: Any = _fetch_messages) -> tuple[list[dict[str, Any]], int]:
    """Collect a bounded Discord history window, walking backward by snowflake."""
    cursor = oldest_id if mode == "backfill" else None
    after = latest_id if mode == "incremental" else None
    collected: list[dict[str, Any]] = []
    pages = 0
    high_water = int(latest_id) if latest_id and latest_id.isdigit() else None
    while len(collected) < max_messages and pages < MAX_SYNC_PAGES:
        page_size = min(MAX_MESSAGES, max_messages - len(collected))
        page = fetch_page(channel_id, page_size, before=cursor,
                          after=after if pages == 0 else None)
        pages += 1
        if not page:
            break
        # Discord pages are newest-to-oldest. Keep just newer items for incremental sync.
        for item in page:
            message_id = str(item.get("id", ""))
            if mode != "incremental" or high_water is None or not message_id.isdigit() or int(message_id) > high_water:
                collected.append(item)
                if len(collected) >= max_messages:
                    break
        ids = [str(item.get("id", "")) for item in page if item.get("id")]
        if not ids:
            break
        next_cursor = min(ids, key=lambda value: int(value) if value.isdigit() else 0)
        if cursor == next_cursor:
            break
        cursor = next_cursor
        if len(page) < page_size:
            break
        if mode == "incremental" and high_water is not None and any(
            value.isdigit() and int(value) <= high_water for value in ids
        ):
            break
    # Older-page walking can return newest first; import chronologically for deterministic updates.
    collected.sort(key=lambda item: int(item.get("id", "0")) if str(item.get("id", "0")).isdigit() else 0)
    return collected, pages


def _discord_source_url(guild_id: str | None, channel_id: str, message_id: str) -> str:
    return f"https://discord.com/channels/{guild_id or '@me'}/{channel_id}/{message_id}"


def _to_model(raw: dict[str, Any], channel: dict[str, Any], channel_id: str) -> DiscordMessage:
    author = raw.get("author") or {}
    message_id = str(raw.get("id") or "")
    return DiscordMessage(
        id=message_id,
        channel_id=channel_id,
        channel_name=channel.get("name") or "",
        guild_id=channel.get("guild_id"),
        guild_name="",
        author_id=str(author.get("id") or ""),
        author_name=author.get("global_name") or author.get("username") or "Unknown",
        content=raw.get("content") or "",
        timestamp=raw.get("timestamp") or "",
        source_url=_discord_source_url(channel.get("guild_id"), channel_id, message_id),
    )


def _raise_discord_error(exc: Exception) -> None:
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if isinstance(exc, LookupError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, RuntimeError):
        status = 503 if "not configured" in str(exc) or "Could not reach" in str(exc) else 502
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "context-retrieval"}


@app.get("/context/channel-history")
async def retrieve_channel_history(
    channel_id: str = Query(min_length=1),
    query: str = Query(min_length=1, max_length=500),
    limit: int = Query(default=50, ge=1, le=MAX_MESSAGES),
) -> dict[str, Any]:
    """Read and rank recent Discord messages without writing to Discord."""
    try:
        channel, messages = await asyncio.gather(
            asyncio.to_thread(_fetch_channel, channel_id),
            asyncio.to_thread(_fetch_messages, channel_id, limit),
        )
    except (RuntimeError, PermissionError, LookupError) as exc:
        _raise_discord_error(exc)

    terms = {word.casefold() for word in query.split() if len(word) > 1}
    ranked = []
    for raw in messages:
        content = raw.get("content") or ""
        lowered = content.casefold()
        score = sum(lowered.count(term) for term in terms)
        if not content.strip() or not score:
            continue
        author = raw.get("author") or {}
        message_id = str(raw.get("id") or "")
        ranked.append(
            {
                "message_id": message_id,
                "channel_id": channel_id,
                "author": author.get("global_name") or author.get("username"),
                "timestamp": raw.get("timestamp"),
                "content": content,
                "score": score,
                "source_url": _discord_source_url(channel.get("guild_id"), channel_id, message_id),
            }
        )
    ranked.sort(key=lambda item: (item["score"], item["timestamp"] or ""), reverse=True)
    return {"query": query, "channel_id": channel_id, "results": ranked[:10]}


@app.post("/context/sync-channel")
async def sync_channel_history(
    channel_id: str = Query(min_length=1),
    max_messages: int = Query(default=500, ge=1, le=MAX_SYNC_MESSAGES),
    mode: str = Query(default="incremental", pattern="^(incremental|backfill)$"),
) -> dict[str, Any]:
    """Incrementally sync or bounded-backfill Discord history into FalkorDB."""
    try:
        channel = await asyncio.to_thread(_fetch_channel, channel_id)
        graph = await asyncio.to_thread(connect_graph)
        latest_id = await asyncio.to_thread(latest_message_id, graph, channel_id)
        oldest_id = await asyncio.to_thread(oldest_message_id, graph, channel_id)
        raw_messages, pages = await asyncio.to_thread(
            _collect_history, channel_id, max_messages, mode=mode,
            latest_id=latest_id, oldest_id=oldest_id,
        )
    except (RuntimeError, PermissionError, LookupError) as exc:
        _raise_discord_error(exc)
    messages = [_to_model(raw, channel, channel_id) for raw in raw_messages if raw.get("id")]
    try:
        counts = await asyncio.to_thread(import_messages, graph, messages)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Could not import history into FalkorDB") from exc
    return {"channel_id": channel_id, "mode": mode, "pages_fetched": pages, **counts}


@app.post("/context/retrieve")
def retrieve_context(request: ContextRequest) -> dict[str, Any]:
    """Search imported Discord evidence using relevant Jira/Linear fields."""
    terms = []
    for value in (request.feature_title, request.blocker, request.assignee):
        if value:
            terms.extend(word for word in value.split() if len(word) > 1)
    try:
        evidence = retrieve_evidence(
            connect_graph(),
            terms=terms,
            task_id=request.task_id,
            channel_id=request.channel_id,
            limit=request.limit,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Could not retrieve evidence from FalkorDB") from exc
    return {
        "feature_title": request.feature_title,
        "task_id": request.task_id,
        "blocker": request.blocker,
        "assignee": request.assignee,
        "channel_id": request.channel_id,
        "evidence": evidence,
    }
