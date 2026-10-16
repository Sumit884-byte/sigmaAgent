import logging
import os
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
            "message_url": message.jump_url,
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

    # Only respond when directly mentioned; avoids replying to every channel message.
    if client.user not in message.mentions:
        return

    question = message.content
    # Remove the bot mention(s) from the question.
    if client.user:
        question = question.replace(f"<@{client.user.id}>", "")
        question = question.replace(f"<@!{client.user.id}>", "")
    question = question.strip()

    if not question:
        await message.reply(
            "Ask me a project question, for example: `@SigmaAgent Why is checkout delayed?`",
            mention_author=False,
        )
        return

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


client.run(DISCORD_TOKEN)
