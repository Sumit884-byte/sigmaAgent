# SigmaAgent Discord bot

A small Discord bot that responds when directly mentioned and forwards the question to the team's existing investigation API. It does not query FalkorDB directly and does not modify Jira/Linear tasks.

## 1. Requirements

- Python 3.10+
- A Discord application/bot token
- The backend investigation endpoint URL from the backend teammate

## 2. Install

Windows PowerShell:
```powershell
cd discord-bot
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Open `.env` and set `DISCORD_TOKEN` and the **real** `INVESTIGATION_API_URL`. The example `/investigate` path is only a placeholder until the team confirms the endpoint.

## 3. Configure Discord

1. Open https://discord.com/developers/applications and select/create the SigmaAgent application.
2. Under **Bot**, reset/copy the bot token and put it only in `.env`.
3. Under **Bot → Privileged Gateway Intents**, enable **Message Content Intent**.
4. Under **OAuth2 → URL Generator**, select scopes `bot` and `applications.commands` (the bot currently uses mention messages; `applications.commands` is optional).
5. Give it View Channels, Send Messages, Read Message History permissions, then use the generated URL to invite it to the test server.
6. Do not commit `.env` or paste the token into Discord/GitHub.

## 4. Run

```powershell
python bot.py
```

In a server channel, try:
`@SigmaAgent Why is checkout delayed?`

The bot POSTs JSON with `question`, `query`, and Discord message metadata to `INVESTIGATION_API_URL`. Confirm the backend contract with the API owner and adjust `investigate()` in `bot.py` if their route or request schema differs.

## 5. Request/response contract to confirm with backend teammate

Request currently sent:
```json
{
  "question": "Why is checkout delayed?",
  "query": "Why is checkout delayed?",
  "source": "discord",
  "discord": {
    "guild_id": "…",
    "channel_id": "…",
    "message_id": "…",
    "author_id": "…",
    "author_name": "…",
    "message_url": "…"
  }
}
```

The bot understands a plain-text response or common JSON keys: `answer`, `summary`, `response`, `result`, `message`, or `analysis`. If your API uses another shape, add that key in `extract_answer()`.

## Troubleshooting

- **Bot online but ignores messages:** enable Message Content Intent in the Discord Developer Portal and check that the bot has channel permissions.
- **401/403:** check backend authentication and `INVESTIGATION_API_KEY`.
- **404:** the endpoint path is wrong; ask the API owner for the exact route.
- **Connection refused:** start the backend or use its reachable URL. `localhost` works only when bot and API run on the same machine.
