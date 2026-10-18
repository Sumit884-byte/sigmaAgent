"""Read-only Discord context search with a write-only history importer."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from dotenv import load_dotenv

from graph_store import (
    connect_graph, import_messages, oldest_message_id,
    retrieve_evidence, save_sync_progress, save_sync_watermark, sync_progress, sync_watermark,
)
from models import DISCORD_CHANNEL_ID_PATTERN, ContextRequest, DiscordMessage

load_dotenv()

app = FastAPI(title="Sigma Agent Context Retrieval API", version="0.2.0")
DISCORD_API = "https://discord.com/api/v10"
MAX_MESSAGES = 100
MAX_SYNC_MESSAGES = 10_000
MAX_SYNC_PAGES = MAX_SYNC_MESSAGES // MAX_MESSAGES
logger = logging.getLogger(__name__)
CHANNEL_ID_PATTERN = DISCORD_CHANNEL_ID_PATTERN


def require_context_api_key(x_context_api_key: str | None = Header(default=None)) -> None:
    expected = os.getenv("CONTEXT_API_KEY")
    if not expected or len(expected) < 32:
        raise HTTPException(status_code=503, detail="Context API authentication is not configured")
    # Compare bytes: compare_digest raises TypeError on non-ASCII str (header values are latin-1)
    if not x_context_api_key or not secrets.compare_digest(
            x_context_api_key.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def allowed_channel_ids() -> list[str] | None:
    """Allowlisted channel IDs, or None when set to "*" (every channel the bot can read)."""
    if os.getenv("DISCORD_ALLOWED_CHANNEL_IDS", "").strip() == "*":
        return None
    allowed = {
        value.strip()
        for value in os.getenv("DISCORD_ALLOWED_CHANNEL_IDS", "").split(",")
        if value.strip()
    }
    if not allowed:
        raise HTTPException(status_code=503, detail="Discord channel allowlist is not configured")
    if any(not re.fullmatch(CHANNEL_ID_PATTERN, channel_id) for channel_id in allowed):
        raise HTTPException(status_code=503, detail="Discord channel allowlist is invalid")
    return sorted(allowed)


def authorize_channel(channel_id: str) -> None:
    allowed = allowed_channel_ids()
    if allowed is not None and channel_id not in allowed:
        raise HTTPException(status_code=403, detail="Channel is not allowed")


def _discord_request(path: str, retries: int = 4) -> Any:
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN is not configured")
    request = urllib.request.Request(
        f"{DISCORD_API}{path}",
        # Discord's CDN rejects urllib's default User-Agent with HTTP 403 (error 1010)
        headers={"Authorization": f"Bot {token}", "Accept": "application/json",
                 "User-Agent": "DiscordBot (https://github.com/Sumit884-byte/sigmaAgent, 0.2.0)"},
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
                     progress: tuple[str | None, str | None] | None = None,
                     fetch_page: Any = None
                     ) -> tuple[list[dict[str, Any]], int, str | None, str | None]:
    """Collect bounded pages and return the persisted incremental continuation."""
    if fetch_page is None:
        fetch_page = _fetch_messages
    baseline = progress[0] if progress else latest_id
    cursor = progress[1] if progress else (
        baseline if mode == "incremental" and baseline is not None
        else oldest_id if mode == "backfill" else None
    )
    collected: list[dict[str, Any]] = []
    pages = 0
    high_water = int(baseline) if baseline and baseline.isdigit() else None
    continuation: str | None = None
    while len(collected) < max_messages and pages < MAX_SYNC_PAGES:
        page_size = min(MAX_MESSAGES, max_messages - len(collected))
        if mode == "incremental" and baseline is not None:
            # Discord returns the oldest messages after this snowflake first.
            # Advance the lower bound with each page; using `before` here walks
            # backward into history already stored in the graph.
            page = fetch_page(channel_id, page_size, after=cursor or baseline)
        else:
            page = fetch_page(
                channel_id,
                page_size,
                before=cursor if cursor is not None or pages > 0 else None,
            )
        pages += 1
        if not page:
            break
        for item in page:
            message_id = str(item.get("id", ""))
            is_new = mode != "incremental" or high_water is None or (
                message_id.isdigit() and int(message_id) > high_water
            )
            if is_new:
                collected.append(item)
        ids = [str(item.get("id", "")) for item in page if item.get("id")]
        if not ids:
            break
        next_cursor = (
            max(ids, key=lambda value: int(value) if value.isdigit() else 0)
            if mode == "incremental" and baseline is not None
            else min(ids, key=lambda value: int(value) if value.isdigit() else 0)
        )
        if cursor == next_cursor:
            break
        if len(page) < page_size:
            break
        cursor = next_cursor
        if len(collected) >= max_messages:
            # First sync is a bounded recent-history bootstrap. During catch-up,
            # persist the cursor and original watermark to avoid skipping a gap.
            if mode == "incremental" and high_water is not None:
                continuation = next_cursor
            break
    # A full last page is ambiguous at the exact cap; resume once more to confirm completion.
    if pages >= MAX_SYNC_PAGES and cursor is not None and high_water is not None:
        continuation = cursor
    collected.sort(key=lambda item: int(item.get("id", "0")) if str(item.get("id", "0")).isdigit() else 0)
    return collected, pages, baseline if continuation is not None else None, continuation


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
    logger.exception("Discord history request failed", exc_info=exc)
    raise HTTPException(status_code=503, detail="Could not access Discord history") from exc


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "context-retrieval"}


@app.get("/context/channel-history")
async def retrieve_channel_history(
    channel_id: str = Query(min_length=17, max_length=20, pattern=CHANNEL_ID_PATTERN),
    query: str = Query(min_length=1, max_length=500),
    limit: int = Query(default=50, ge=1, le=MAX_MESSAGES),
    _authenticated: None = Depends(require_context_api_key),
) -> dict[str, Any]:
    """Read and rank recent Discord messages without writing to Discord."""
    authorize_channel(channel_id)
    try:
        channel, messages = await asyncio.gather(
            asyncio.to_thread(_fetch_channel, channel_id),
            asyncio.to_thread(_fetch_messages, channel_id, limit),
        )
    except (RuntimeError, PermissionError, LookupError) as exc:
        _raise_discord_error(exc)
    except Exception as exc:
        logger.exception("Discord channel history failed")
        raise HTTPException(status_code=503, detail="Could not retrieve channel history") from exc

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
    channel_id: str = Query(min_length=17, max_length=20, pattern=CHANNEL_ID_PATTERN),
    max_messages: int = Query(default=500, ge=1, le=MAX_SYNC_MESSAGES),
    mode: str = Query(default="incremental", pattern="^(incremental|backfill)$"),
    _authenticated: None = Depends(require_context_api_key),
) -> dict[str, Any]:
    """Incrementally sync or bounded-backfill Discord history into FalkorDB."""
    authorize_channel(channel_id)
    try:
        channel = await asyncio.to_thread(_fetch_channel, channel_id)
    except (RuntimeError, PermissionError, LookupError) as exc:
        _raise_discord_error(exc)
    except Exception as exc:
        logger.exception("Could not read Discord channel")
        raise HTTPException(status_code=503, detail="Could not read Discord channel") from exc
    try:
        graph = await asyncio.to_thread(connect_graph, channel.get("guild_id"))
        # Resume from what sync itself has covered, not the newest (possibly live-streamed) message
        latest_id = await asyncio.to_thread(sync_watermark, graph, channel_id)
        oldest_id = await asyncio.to_thread(oldest_message_id, graph, channel_id)
        progress = (
            await asyncio.to_thread(sync_progress, graph, channel_id)
            if mode == "incremental" else None
        )
        raw_messages, pages, baseline_id, cursor_id = await asyncio.to_thread(
            _collect_history, channel_id, max_messages, mode=mode,
            latest_id=latest_id, oldest_id=oldest_id, progress=progress,
        )
    except Exception as exc:
        logger.exception("Could not prepare Discord history sync")
        raise HTTPException(status_code=503, detail="Could not prepare history sync") from exc
    messages = [_to_model(raw, channel, channel_id) for raw in raw_messages if raw.get("id")]
    try:
        counts = await asyncio.to_thread(import_messages, graph, messages)
        if mode == "incremental":
            await asyncio.to_thread(save_sync_progress, graph, channel_id, baseline_id, cursor_id)
            if cursor_id is None:
                # Caught up: everything through the newest fetched message is now in the graph
                ids = [int(m.id) for m in messages if str(m.id).isdigit()]
                if latest_id and latest_id.isdigit():
                    ids.append(int(latest_id))
                if ids:
                    await asyncio.to_thread(save_sync_watermark, graph, channel_id, str(max(ids)))
    except Exception as exc:
        logger.exception("Could not import Discord history")
        raise HTTPException(status_code=503, detail="Could not import history into FalkorDB") from exc
    return {
        "channel_id": channel_id, "mode": mode, "pages_fetched": pages,
        "sync_in_progress": cursor_id is not None, **counts,
    }


@app.post("/context/retrieve")
def retrieve_context(
    request: ContextRequest,
    _authenticated: None = Depends(require_context_api_key),
) -> dict[str, Any]:
    """Search imported Discord evidence using relevant Jira/Linear fields."""
    if request.channel_id is not None:
        authorize_channel(request.channel_id)
    allowed_channels = allowed_channel_ids()
    terms = []
    for value in (request.feature_title, request.blocker, request.assignee):
        if value:
            terms.extend(word for word in value.split() if len(word) > 1)
    try:
        evidence = retrieve_evidence(
            connect_graph(request.guild_id),
            terms=terms,
            task_id=request.task_id,
            channel_id=request.channel_id,
            channel_ids=allowed_channels,
            limit=request.limit,
        )
    except Exception as exc:
        logger.exception("Could not retrieve evidence from FalkorDB")
        raise HTTPException(status_code=503, detail="Could not retrieve evidence from FalkorDB") from exc
    return {
        "feature_title": request.feature_title,
        "task_id": request.task_id,
        "blocker": request.blocker,
        "assignee": request.assignee,
        "channel_id": request.channel_id,
        "evidence": evidence,
    }
