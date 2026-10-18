import asyncio
import logging
import os
import tempfile
from typing import Any

import discord
import httpx
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("sigmaagent-bot")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
INVESTIGATION_API_URL = os.getenv("INVESTIGATION_API_URL")
INVESTIGATION_API_KEY = os.getenv("INVESTIGATION_API_KEY", "")
API_TIMEOUT_SECONDS = float(os.getenv("API_TIMEOUT_SECONDS", "45"))
DISCORD_STREAM_URL = os.getenv(
    "DISCORD_STREAM_URL",
    INVESTIGATION_API_URL.replace("/investigate", "/discord/stream-message")
    if INVESTIGATION_API_URL
    else "",
)

if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN in your .env file.")
if not INVESTIGATION_API_URL:
    raise RuntimeError(
        "Missing INVESTIGATION_API_URL. Ask your backend teammate for the exact investigation endpoint."
    )

intents = discord.Intents.default()
intents.message_content = True
intents.members = False

client = discord.Client(intents=intents)


def extract_answer(data: Any) -> str:
    """Handle common API response shapes without assuming a specific backend schema."""
    if isinstance(data, str):
        return data.strip()

    if isinstance(data, dict):
        for key in ("answer", "summary", "response", "result", "message", "analysis"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                nested = extract_answer(value)
                if nested:
                    return nested

        # Useful fallback while the backend response schema is being finalized.
        import json
        return "```json\n" + json.dumps(data, ensure_ascii=False, indent=2)[:1600] + "\n```"

    return str(data)


async def investigate(question: str, message: discord.Message) -> str:
    payload = {
        "question": question,
        "query": question,
        "source": "discord",
        "discord": {
            "guild_id": str(message.guild.id) if message.guild else None,
            "channel_id": str(message.channel.id),
            "message_id": str(message.id),
            "author_id": str(message.author.id),
            "author_name": str(message.author),
            "timestamp": message.created_at.isoformat(),
            "message_content": message.content,
            "message_url": message.jump_url,
            "bot_name": client.user.name if client.user else None,
        },
    }
    headers = {"Content-Type": "application/json"}
    if INVESTIGATION_API_KEY:
        headers["Authorization"] = f"Bearer {INVESTIGATION_API_KEY}"

    async with httpx.AsyncClient(timeout=API_TIMEOUT_SECONDS) as http:
        response = await http.post(
            INVESTIGATION_API_URL,
            json=payload,
            headers=headers,
        )
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError:
            return response.text.strip() or "The investigation API returned an empty response."
        return extract_answer(data)


def is_channel_allowed(channel_id: str | int | None) -> bool:
    """Respect the channel allowlist configured in context retrieval to honor hackathon data rules."""
    if not channel_id:
        return False
    raw = os.getenv("DISCORD_ALLOWED_CHANNEL_IDS", "").strip()
    if not raw:
        return False
    if raw == "*":
        return True
    allowed = {x.strip() for x in raw.split(",") if x.strip()}
    return str(channel_id).strip() in allowed


async def stream_message_to_graph(message: discord.Message) -> None:
    """Asynchronously stream a Discord message to the company brain graph in real time."""
    if not DISCORD_STREAM_URL or message.author.bot:
        return
    if not is_channel_allowed(message.channel.id):
        log.debug("Channel %s is not allowlisted; skipping real-time message streaming.", message.channel.id)
        return
    payload = {
        "guild_id": str(message.guild.id) if message.guild else None,
        "guild_name": message.guild.name if message.guild else "",
        "channel_id": str(message.channel.id),
        "channel_name": getattr(message.channel, "name", "direct"),
        "message_id": str(message.id),
        "author_id": str(message.author.id),
        "author_name": str(message.author),
        "content": message.content,
        "timestamp": message.created_at.isoformat(),
        "source_url": message.jump_url,
    }
    headers = {"Content-Type": "application/json"}
    if INVESTIGATION_API_KEY:
        headers["Authorization"] = f"Bearer {INVESTIGATION_API_KEY}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as http:
            resp = await http.post(DISCORD_STREAM_URL, json=payload, headers=headers)
            if resp.status_code >= 400:
                log.warning(
                    "Real-time stream rejected for message %s: HTTP %s %s",
                    message.id, resp.status_code, resp.text[:200],
                )
    except Exception as exc:
        log.warning("Real-time stream failed for message %s: %s", message.id, exc)


async def stream_message_edit_to_graph(message_id: str, content: str, guild_id: str | None = None) -> None:
    """Stream a message edit to the company brain graph."""
    if not DISCORD_STREAM_URL:
        return
    edit_url = f"{DISCORD_STREAM_URL.rstrip('/')}/{message_id}"
    payload = {"content": content, "guild_id": guild_id}
    headers = {"Content-Type": "application/json"}
    if INVESTIGATION_API_KEY:
        headers["Authorization"] = f"Bearer {INVESTIGATION_API_KEY}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as http:
            resp = await http.put(edit_url, json=payload, headers=headers)
            if resp.status_code >= 400:
                log.warning(
                    "Real-time stream edit rejected for message %s: HTTP %s %s",
                    message_id, resp.status_code, resp.text[:200],
                )
    except Exception as exc:
        log.warning("Real-time stream edit failed for message %s: %s", message_id, exc)


async def stream_message_delete_to_graph(message_id: str, guild_id: str | None = None) -> None:
    """Stream a message deletion to the company brain graph."""
    if not DISCORD_STREAM_URL:
        return
    delete_url = f"{DISCORD_STREAM_URL.rstrip('/')}/{message_id}"
    headers = {"Content-Type": "application/json"}
    if INVESTIGATION_API_KEY:
        headers["Authorization"] = f"Bearer {INVESTIGATION_API_KEY}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as http:
            resp = await http.delete(delete_url, params={"guild_id": guild_id} if guild_id else None, headers=headers)
            if resp.status_code >= 400:
                log.warning(
                    "Real-time stream delete rejected for message %s: HTTP %s %s",
                    message_id, resp.status_code, resp.text[:200],
                )
    except Exception as exc:
        log.warning("Real-time stream delete failed for message %s: %s", message_id, exc)


async def send_long_message(channel: discord.abc.Messageable, text: str) -> None:
    # Discord messages have a 2,000-character limit.
    text = text.strip() or "The investigation API returned no answer."
    for start in range(0, len(text), 1900):
        await channel.send(text[start : start + 1900])


@client.event
async def on_ready():
    log.info("Logged in as %s (id=%s)", client.user, client.user.id if client.user else "unknown")


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Real-time message streaming: keep the company brain graph continuously current
    asyncio.create_task(stream_message_to_graph(message))

    # Only respond when mentioned; avoids replying to every channel message.
    # Discord's autocomplete offers both the bot user and its same-named managed role,
    # so a mention of the bot's role counts too.
    bot_role = message.guild.self_role if message.guild else None
    mentioned_role = bot_role is not None and bot_role in message.role_mentions
    if client.user not in message.mentions and not mentioned_role:
        return

    question = message.content
    # Remove the bot mention(s) from the question.
    if client.user:
        question = question.replace(f"<@{client.user.id}>", "")
        question = question.replace(f"<@!{client.user.id}>", "")
    if bot_role:
        question = question.replace(f"<@&{bot_role.id}>", "")
    question = question.strip()

    if not question:
        # A bare mention shows what the bot can do; the backend owns that list
        question = "help"

    async with message.channel.typing():
        try:
            answer = await investigate(question, message)
            await send_long_message(message.channel, f"**SigmaAgent investigation**\n{answer}")
        except httpx.HTTPStatusError as exc:
            log.exception("Investigation API returned HTTP %s", exc.response.status_code)
            await message.reply(
                f"The investigation API returned HTTP {exc.response.status_code}. "
                "Please check the configured endpoint and backend logs.",
                mention_author=False,
            )
        except httpx.RequestError:
            log.exception("Could not reach investigation API")
            await message.reply(
                "I couldn't reach the investigation API. Check that the backend is running "
                "and `INVESTIGATION_API_URL` is correct.",
                mention_author=False,
            )
        except Exception:
            log.exception("Unexpected error while handling a mention")
            await message.reply(
                "Something went wrong while investigating. Check the bot logs for details.",
                mention_author=False,
            )


@client.event
async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent):
    """Keep the graph current when a user edits their Discord message."""
    if not is_channel_allowed(payload.channel_id):
        return
    data = payload.data or {}
    new_content = data.get("content")
    if new_content and payload.message_id:
        guild_id = str(payload.guild_id) if payload.guild_id else None
        asyncio.create_task(stream_message_edit_to_graph(str(payload.message_id), new_content, guild_id))


@client.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    """Keep the graph current when a user deletes their Discord message."""
    if not is_channel_allowed(payload.channel_id):
        return
    if payload.message_id:
        guild_id = str(payload.guild_id) if payload.guild_id else None
        asyncio.create_task(stream_message_delete_to_graph(str(payload.message_id), guild_id))


def _acquire_single_instance_lock() -> None:
    """Refuse a second local instance: every live bot session answers each mention."""
    lock_path = os.path.join(tempfile.gettempdir(), "sigmaagent-bot.lock")
    lock = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        try:
            import msvcrt

            msvcrt.locking(lock, msvcrt.LK_NBLCK, 1)
        except ImportError:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit(
            "SigmaAgent bot is already running (single-instance lock held); "
            "two live instances answer every mention twice."
        )
    os.write(lock, str(os.getpid()).encode())


_acquire_single_instance_lock()
client.run(DISCORD_TOKEN)
