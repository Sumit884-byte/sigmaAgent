# Person 3: context and Discord history

This service imports Discord conversation history into Person 3's configured
FalkorDB graph, extracts explicitly signaled blocker and decision statements,
and returns conversation evidence with its original Discord URL. It reads
Person 2's existing `Task` nodes and links evidence to a task only when a
message explicitly names that task by ID or full title. Person 2's root files
and graph credentials are not used or modified.

## Shared graph schema

- `(:Task {id, title})` and `(:Developer {name})` are Person 2's existing nodes.
- `(:DiscordChannel {id, name, guild_id, guild_name})`
- `(:DiscordMessage {id, content, timestamp, source_url})`
- `(:ContextEvidence {id, kind, excerpt, source_url})`
- `(:Developer)-[:AUTHORED]->(:DiscordMessage)`
- `(:DiscordMessage)-[:IN_CHANNEL]->(:DiscordChannel)`
- `(:DiscordMessage)-[:SUPPORTS]->(:ContextEvidence)`
- `(:ContextEvidence)-[:ABOUT]->(:Task)` only when the source message explicitly
  references that task.

Task and evidence identity use stable IDs, and imports use `MERGE`, so syncing
the same message repeatedly is idempotent. History writes are grouped into
batches of 250 messages to limit query overhead and memory use. Range indexes
support channel watermarks and full-text search supports message retrieval.
The importer does not create or modify Person 2's `BLOCKED_BY` or `ASSIGNED_TO`
relationships.

## Configure and run

Use an isolated FalkorDB instance. For local development, the defaults connect
to the local Docker service; for cloud testing, provide your own instance
settings. Set `DISCORD_BOT_TOKEN` only when syncing live history. Do not commit
real credentials.

```powershell
$env:FALKORDB_HOST = "127.0.0.1"
$env:FALKORDB_PORT = "6379"
$env:FALKORDB_GRAPH = "SigmaPerson3Test"
python import_history.py                 # imports mock_discord_history.json
python -m uvicorn api:app --host 127.0.0.1 --port 8001
```

The mock importer also copies only `Task` IDs and titles from Person 2's
`tasks.json` into this configured graph. This supplies mock shared-schema
records for local evidence-link testing; it does not call or modify Person 2's
API or cloud database.

For live history, set `DISCORD_BOT_TOKEN`, then call
`POST /context/sync-channel?channel_id=<id>&max_messages=500&mode=incremental`.
Incremental sync fetches messages after the newest stored message; `mode=backfill`
fetches older messages before the oldest stored message. Each call is capped at
10,000 messages and Discord's 100-message page size. The bot requires View
Channel and Read Message History permissions. This writes imported records only
to the configured FalkorDB graph; it never sends Discord messages. Discord
rate limits are retried using its `Retry-After` delay, with bounded retries for
temporary server/network errors.

## API

### Import channel history

`POST /context/sync-channel?channel_id=<id>&max_messages=500&mode=incremental`

Reads a bounded page window from Discord, stores it in batches, extracts
candidate evidence, and returns import counts and pages fetched. `max_messages`
can be from 1 to 10,000; `mode` is `incremental` or `backfill`.

### Retrieve evidence

`POST /context/retrieve`

Person 4 (or the bot caller) passes relevant fields returned by Person 2:

```json
{
  "feature_title": "Build LLM Agent",
  "task_id": "T2",
  "blocker": "missing API key",
  "assignee": "Person 4",
  "channel_id": "234567890123456789",
  "limit": 20
}
```

The response includes the source message, extracted evidence excerpt, author,
timestamp, task reference when explicit, and original Discord link.

### Direct channel search

`GET /context/channel-history?channel_id=<id>&query=<text>&limit=50` reads and
ranks recent Discord messages without saving them. It does not post to Discord.

## Evidence extraction

The MVP marks a sentence as a blocker candidate only when it explicitly says
“blocked by/on,” “waiting for,” or “cannot proceed until.” It marks decisions
when a sentence says “Decision:”, “decided to,” or “agreed to.” The exact
sentence is stored as evidence and linked to its source message. An unrelated
message is not attached to a task by semantic guesswork.

## Tests

From this directory, run `python -m unittest discover -s tests`. Tests cover
explicit extraction, unsupported statements, task-link evidence, idempotent
imports, bounded bulk writes for 1,001 mock messages, full-text retrieval, and
incremental/backfill pagination. To exercise FalkorDB with the included fake
history, run `python import_history.py` and query `/context/retrieve`.

For an opt-in local database load test, run
`python load_test_local.py --messages 10000`. It uses the separate
`SigmaPerson3LoadTest` graph by default (or the graph named by
`FALKORDB_GRAPH`) and generates synthetic records only. The run is repeatable
because it uses stable IDs; use a fresh graph name for a clean import timing.
