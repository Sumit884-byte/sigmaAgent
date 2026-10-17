# SigmaAgent Hackathon Handoff

This document summarizes the hackathon idea, the team split, Person 3's work, and the current test state discussed in the project chat. It is a handoff note, not a replacement for the code or the live Jira/Linear project.

## Hackathon idea: Company Brain

The project targets the FalkorDB Company Brain track. The goal is to answer project questions with both the current task graph and the conversation context that explains it. Example: when someone asks why a feature is delayed, SigmaAgent should find its ticket, owner, blocker, relevant meeting or Discord evidence, and downstream work at risk.

Two product requirements came up repeatedly:

1. **Trace decisions to their source.** A task assignment or blocker should be connected to the Discord message, speaker, and time that caused the change, with a clickable source link.
2. **Show the dependency path.** An `explain` request should render the task chain and owners, and reveal the read-only graph query used to build the answer.

The original MVP proposal called for a read-only assistant that recommends a next step and drafts a message for human review. The current task-change implementation can apply recognized task updates and record an audit event. The team should confirm whether those write actions are in scope for the final MVP or should be disabled behind a human approval step.

## Proposed shared technology

The team proposed FalkorDB, Python/FastAPI, LangGraph, OpenAI API, LlamaIndex, Next.js/TypeScript, PostgreSQL, and Docker. Person 3's implemented services use Python/FastAPI, FalkorDB, Discord history APIs, and the Gemini API in the main investigation backend. The repository does not currently show LangGraph, LlamaIndex, PostgreSQL, or OpenAI API usage in Person 3's implementation; treat those as proposed stack items unless another teammate's code adds them.

## Team responsibilities

| Person | Responsibility | Relationship to the other work |
| --- | --- | --- |
| 1 | Discord bot integration, landing page/UI, and repository initialization | Sends Discord questions and metadata to the backend; provides the user-facing entry point. |
| 2 | Jira/Linear task API and task/dependency graph | Supplies task IDs, titles, assignees, and blocker relationships in FalkorDB. |
| 3 | Context retrieval and Discord channel history | Syncs conversation history and returns evidence linked to tasks, people, and original Discord messages. |
| 4 | Agent orchestration, efficiency, and final verification | Combines task data and retrieved context into answers and checks the integrated workflow. |

The work is interdependent: Person 3 needs Person 2's task IDs and graph schema; Person 4 needs both task data and retrieved evidence; Person 1 needs a stable investigation API contract.

## Person 3 implementation

Person 3's service lives in [`context retrieval/`](context%20retrieval/). It has its own FastAPI entry point, models, graph store, requirements, README, mock Discord history, and tests. Its design keeps Discord identity separate from Person 2's `Developer` records and does not create or modify Person 2's `BLOCKED_BY` or `ASSIGNED_TO` task relationships.

Implemented capabilities include:

- Authenticated sync, direct channel-history, and retrieval endpoints. Channel IDs are validated as numeric Discord IDs and checked against the configured allowlist.
- Incremental sync pagination using Discord's forward `after` cursor, a continuation cursor when a per-call cap is reached, and backfill support for older messages.
- Batched graph writes, channel watermarks and indexes, retry handling for Discord rate limits and temporary failures, and stable message IDs for idempotent imports.
- Evidence extraction and retrieval that returns the original Discord message, author, timestamp, excerpt, task reference when explicit, and message URL.
- Task-aware search: the task ID and title participate in retrieval, so evidence such as “T2 is blocked by…” can be found for T2.
- Safer author mapping: Discord user IDs are stable identities; explicit `DISCORD_DEVELOPER_MAP` mappings link an author to one unambiguous developer without renaming that developer.
- Re-import handling that replaces stale evidence when a Discord message is edited, and full-text handling for hyphenated terms such as `front-end` and `log-in`.
- Isolated load testing so synthetic high-volume data does not accidentally use the normal development graph.

The project also records task changes as source-linked `TaskChange` audit events in the main investigation API. Repeated message delivery is intended to be idempotent. Explain mode deduplicates task rows, returns the longest blocker chain and each task's owner, and includes the read-only Cypher queries and parameters.

## Testing and verification history

Test files in the repository cover context API security, configuration examples, evidence extraction, FalkorDB integration, graph connection caching, import behavior, load-test safety, and sync pagination. The main `tests/test_change_provenance.py` covers task-change provenance and dependency explanations.

In the project chat, earlier test runs were reported as passing, with six optional tests skipped. The local end-to-end setup was also exercised: the main API and context API started on ports 8000 and 8001, connected to the configured FalkorDB graph, and the Discord bot logged in as `sigmaagent#4659`. A user-authored mention in the server produced a bot response, confirming the Discord-to-investigation path was reachable.

The latest dependency-chain verification queried the main `/investigate` endpoint and received HTTP 200. This verified the backend answer after the live graph update; it was a direct API check, not a second human-authored Discord message.

## Current live graph state

The local `.env` is configured for the server guild `1560690396361658490`. It is ignored by Git and must not be committed. No credentials are included in this document.

The server's graph already contained these tasks and owners:

| Task | Title | Owner | Status |
| --- | --- | --- | --- |
| T1 | Auth | Person 1 | todo |
| T2 | Frontend | Person 2 | todo |
| T3 | Backend | Person 3 | todo |
| T4 | Testing | Person 4 | todo |
| T5 | CI/CD Pipeline | Person 4 | todo |

The chat specified the dependency chain `T5 <- T4 <- T3`. The two corresponding `BLOCKED_BY` edges were added to the live graph and read back successfully:

- T5 is blocked by T4.
- T4 is blocked by T3.

These were added with `MERGE`, so repeating the operation does not duplicate the edges. The graph contained no `TaskChange` assignment events or imported Discord message evidence for this guild at the time of verification. Therefore, explain mode can show the dependency path and current owners, but it cannot truthfully say who assigned those owners or link an assignment to a source message yet.

## How to try it

In a server channel where SigmaAgent can read messages and send replies, mention it with:

```text
@SigmaAgent explain T5
```

Expected path portion of the answer:

```text
Dependency path: T5: CI/CD Pipeline (Person 4; todo) <- T4: Testing (Person 4; todo) <- T3: Backend (Person 3; todo)
```

The response also includes the Cypher dependency and assignment-source queries. Until source-backed assignment events exist, it should state that T5, T4, and T3 have no recorded assignment source. That is an honest provenance gap, not evidence that the current owners are unknown.

For local development, start the main API, context API, and Discord bot using their respective project READMEs. The root `.env` contains local credentials; never copy it into Git, a PR, a screenshot, or a shared document. Credentials that were pasted into chat should be rotated.

## Pull request and remaining follow-up

The Person 3 implementation branch is covered by [PR #5](https://github.com/Sumit884-byte/sigmaAgent/pull/5). The latest live graph edge changes are database data, not repository code, so they did not create a separate code commit or PR.

Recommended follow-up:

1. Capture real assignment and blocker changes through Discord so each event has a speaker, timestamp, message ID, and clickable URL.
2. Decide whether task mutations remain enabled or require explicit human approval to match the original read-only MVP proposal.
3. Have Person 2 provide the authoritative task/dependency source and confirm its guild mapping before replacing or extending any task data.
4. Run the documented test suites and a real server mention after integration changes, and report skipped optional tests separately from passing tests.
