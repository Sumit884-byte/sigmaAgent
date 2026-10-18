<p align="center">
  <img src="https://capsule-render.vercel.app/api?type=waving&color=0:6C63FF,100:3B82F6&height=220&section=header&text=Sigma%20Agent&fontSize=80&fontColor=ffffff&fontAlignY=35&desc=AI-Powered%20Project%20Intelligence%20for%20Discord&descSize=20&descAlignY=55&animation=fadeIn" width="100%" />
</p>

<p align="center">
  <a href="#-quick-start"><img src="https://img.shields.io/badge/Get_Started-6C63FF?style=for-the-badge&logo=rocket&logoColor=white" alt="Get Started" /></a>
  <a href="#-features"><img src="https://img.shields.io/badge/Features-3B82F6?style=for-the-badge&logo=sparkles&logoColor=white" alt="Features" /></a>
  <a href="#-architecture"><img src="https://img.shields.io/badge/Architecture-10B981?style=for-the-badge&logo=diagram&logoColor=white" alt="Architecture" /></a>
  <a href="#-api-reference"><img src="https://img.shields.io/badge/API_Docs-F59E0B?style=for-the-badge&logo=swagger&logoColor=white" alt="API Docs" /></a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.11+" />
  <img src="https://img.shields.io/badge/FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/FalkorDB-FF6B35?style=flat-square&logo=graphql&logoColor=white" alt="FalkorDB" />
  <img src="https://img.shields.io/badge/Gemini_AI-8E75B2?style=flat-square&logo=google&logoColor=white" alt="Gemini AI" />
  <img src="https://img.shields.io/badge/Discord.py-5865F2?style=flat-square&logo=discord&logoColor=white" alt="Discord.py" />
  <img src="https://img.shields.io/badge/Docker-2496ED?style=flat-square&logo=docker&logoColor=white" alt="Docker" />
  <img src="https://img.shields.io/badge/license-MIT-22C55E?style=flat-square" alt="MIT License" />
</p>

<br />

<p align="center">
  <strong>SigmaAgent</strong> is an AI-powered Discord bot that acts as your team's <em>project brain</em>.<br/>
  It builds a live <b>knowledge graph</b> from your tasks, GitHub activity, documentation, and chat —<br/>
  then answers questions, tracks dependencies, identifies bottlenecks, and finds experts instantly.
</p>

<br />

---

<br />

## ✨ Features

<table>
<tr>
<td width="50%">

### 🧠 Knowledge Graph Brain

Every Discord server gets its own isolated FalkorDB graph. Tasks, developers, commits, issues, docs, and chat messages form an interconnected web of project knowledge.

```
(:Developer)-[:ASSIGNED_TO]->(:Task)
(:Commit)-[:TOUCHES]->(:File)
(:Issue)-[:MENTIONS]->(:Task)
(:DiscordMessage)-[:SUPPORTS]->(:Evidence)
```

</td>
<td width="50%">

### 🤖 Natural Language Interface

Just mention `@SigmaAgent` in Discord. No slash commands needed — it understands plain language:

> *"What is blocked?"*
> *"Who knows about billing?"*
> *"Priya will do the frontend"*
> *"Compare Alice and Bob"*

</td>
</tr>
<tr>
<td width="50%">

### 📊 Graph Analytics (Built-In)

Instant insights powered by FalkorDB's graph algorithms:

- **PageRank** — find bottleneck tasks and people
- **Critical Path** — longest chain of unfinished work
- **Impact Analysis** — what slips if a task slips
- **WCC** — independent workstreams that can run in parallel

</td>
<td width="50%">

### 🔄 Real-Time Sync

Knowledge stays current automatically:

- **GitHub Webhooks** — commits, issues, PRs ingested in real time
- **Background Syncer** — periodic pulls with exponential backoff
- **Discord Streaming** — every message is evidence-extracted on the fly
- **Garbage Collection** — prunes orphaned nodes and expired data

</td>
</tr>
<tr>
<td width="50%">

### 👥 Expertise Discovery

> *"Who knows about authentication?"*

SigmaAgent walks six types of evidence — task ownership, commits, issues, docs, chat mentions, and team membership — to rank developers by topic expertise.

</td>
<td width="50%">

### 🏆 Friend Competition

Track open-source contributions with friends:

> *"Compare @alice and @bob"*

Scores are computed from commits (×3), closed assigned issues (×2), open assigned issues, and issues opened — with leaderboards and profile links.

</td>
</tr>
</table>

<br />

---

<br />

## 🏗 Architecture

Requests share an efficient zero-token-first path: **clients → deterministic router → graph algorithms or Gemini LLM failover → isolated company brain**. Most queries never need an LLM call.

<p align="center">
  <img src="docs/architecture.svg" alt="SigmaAgent Architecture" width="100%" />
</p>

| Layer | What it does |
|:---|:---|
| **Clients & Ingress** | Discord Bot (`@SigmaAgent`), GitHub Webhooks (`push`, `issues`, `PRs`), FastAPI REST on `:8000`, and Next.js Web on `:3000` |
| **Route (Zero-Token)** | Deterministic intent router matching regex patterns to answer task, blocker, contributor, and issue queries with **0 LLM tokens** |
| **Decide & Reason** | **Direct Graph Engine** (FalkorDB Cypher, PageRank, WCC, multi-hop traversals) or **Gemini AI LLM failover** for synthesis & reasoning |
| **Company Brain** | Multi-tenant isolated FalkorDB graph per Discord server (`Sigma_<guild_id>`) storing tasks, code commits, issues, docs & chat evidence |
| **Lifecycle & Sync** | Continuous GitHub webhook ingestion, background sync with backoff, garbage collection & 6-way developer expertise ranking |


<br />

---

<br />

## 🚀 Quick Start

### Prerequisites

<table>
<tr>
<td><img src="https://img.shields.io/badge/Python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white" /></td>
<td>Python 3.11 or higher</td>
</tr>
<tr>
<td><img src="https://img.shields.io/badge/FalkorDB-Cloud_or_Local-FF6B35?style=flat-square&logo=graphql&logoColor=white" /></td>
<td>FalkorDB instance (cloud or local)</td>
</tr>
<tr>
<td><img src="https://img.shields.io/badge/Discord-Bot_Token-5865F2?style=flat-square&logo=discord&logoColor=white" /></td>
<td>Discord bot application token</td>
</tr>
<tr>
<td><img src="https://img.shields.io/badge/Gemini-API_Key-8E75B2?style=flat-square&logo=google&logoColor=white" /></td>
<td>Google Gemini API key</td>
</tr>
</table>

### 1️⃣ Clone & Install

```bash
git clone https://github.com/Sumit884-byte/sigmaAgent.git
cd sigmaAgent

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
pip install -r "context retrieval/requirements.txt"
pip install -r discord-bot/requirements.txt
```

### 2️⃣ Configure Environment

```bash
cp .env.example .env
```

Edit `.env` with your credentials. A local FalkorDB uses port `6379`. A FalkorDB Cloud instance uses the host and port from the cloud dashboard (often not `6379`).

```env
# FalkorDB — local defaults shown; replace host and port for cloud
FALKORDB_HOST=127.0.0.1
FALKORDB_PORT=6379
FALKORDB_USERNAME=falkordb
FALKORDB_PASSWORD=your-password
FALKORDB_GRAPH=SigmaCompanyBrain
FALKORDB_GRAPH_PREFIX=Sigma_

# Discord
DISCORD_TOKEN=your-bot-token
DISCORD_GUILD_ID=your-server-id

# Gemini AI
GEMINI_API_KEY=your-gemini-key

# Internal API Security
INVESTIGATION_API_KEY=generate-a-strong-random-key
INVESTIGATION_API_URL=http://localhost:8000/investigate
CONTEXT_API_URL=http://127.0.0.1:8001
CONTEXT_API_KEY=generate-a-strong-random-key
```

### 3️⃣ Start the Backend

```bash
uvicorn api:app --host 0.0.0.0 --port 8000
```

In a second terminal, start the context retrieval API:

```bash
uvicorn api:app --host 127.0.0.1 --port 8001 --app-dir "context retrieval"
```

### 4️⃣ Start the Discord Bot

```bash
cd discord-bot
python bot.py
```

### 5️⃣ Start the Website (optional)

```bash
cd website
npm install
npm run dev
```

The Next.js app listens on [http://localhost:3000](http://localhost:3000).

### 🐳 Docker (Alternative)

```bash
docker build -t sigma-agent .
docker run -p 8000:8000 --env-file .env sigma-agent
```

<br />

---

<br />

## 💬 Usage Examples

### Task Management (Natural Language)

| You say | SigmaAgent does |
|---------|-----------------|
| `Priya will do the frontend` | Creates/assigns task |
| `T3 is blocked by T1` | Adds dependency edge |
| `mark T2 as done` | Updates status |
| `what is blocked?` | Shows blocked tasks with reasons |
| `explain T5` | Full dependency chain with owners |

### GitHub Intelligence

| You say | SigmaAgent does |
|---------|-----------------|
| `who are the contributors?` | Commit counts, ranked |
| `what bugs are open?` | Filtered issue list with labels |
| `summarize issue #6` | Summary + latest discussion |
| `compare alice and bob` | Open-source contribution showdown |
| `sync github owner/repo` | Pulls commits, issues, docs into graph |

### Expertise & Teams

| You say | SigmaAgent does |
|---------|-----------------|
| `who knows about billing?` | Ranks people by 6 types of evidence |
| `experts` | All topics and their top people |
| `Priya joined the payments team` | Updates team membership |
| `bottlenecks` | PageRank over dependency graph |

<br />

---

<br />

## 📡 API Reference

The FastAPI backend exposes a rich REST API (all authenticated with `INVESTIGATION_API_KEY`):

<details>
<summary><b>🔍 Investigation</b></summary>

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/investigate` | Ask a question — returns answer, sources, Cypher |

</details>

<details>
<summary><b>🐙 GitHub</b></summary>

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/github/contributors` | Contributors with commit counts |
| `GET` | `/github/issues` | Issues filtered by state/label/author/assignee |
| `GET` | `/github/issue/{number}` | Single issue with body + comments |
| `GET` | `/github/compare?users=a,b` | Friend competition scoreboard |
| `POST` | `/webhooks/github` | Receive GitHub webhook events |

</details>

<details>
<summary><b>📊 Knowledge Sync</b></summary>

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/knowledge/sync` | Pull repo into server graph |
| `GET` | `/knowledge/sync/status` | Background syncer health |
| `POST` | `/knowledge/sync/run-now` | Trigger immediate sync |
| `POST` | `/knowledge/gc` | Run garbage collection |
| `GET` | `/knowledge/gc/status` | Latest GC metrics |

</details>

<details>
<summary><b>💬 Discord Streaming</b></summary>

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/discord/stream-message` | Ingest live message |
| `PUT` | `/discord/stream-message/{id}` | Update edited message |
| `DELETE` | `/discord/stream-message/{id}` | Soft-delete message |

</details>

<details>
<summary><b>📋 Dependencies</b></summary>

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/dependencies` | All blocking relationships |

</details>

<br />

---

<br />

## 📁 Project Structure

```
sigmaAgent/
├── api.py                    # 🚀 FastAPI backend — investigation, tasks, GitHub, streaming
├── knowledge.py              # 🧠 Knowledge graph: sync, expertise, teams, search
├── github_analysis.py        # 🐙 Live GitHub REST API: contributors, issues, compare
├── continuous_sync.py        # 🔄 Webhooks, background sync, garbage collection
├── ingest_tasks.py           # 📥 Seed tasks from tasks.json into the graph
├── migrate_to_server_graphs.py # 🔀 Split shared graph into per-server graphs
├── test_connection.py        # 🔌 Verify FalkorDB connectivity
├── tasks.json                # 📋 Initial task data
├── Dockerfile                # 🐳 Container build
├── requirements.txt          # 📦 Python dependencies
│
├── discord-bot/
│   └── bot.py                # 🤖 Discord bot — message handling, API calls
│
├── context retrieval/        # 🔍 Evidence extraction pipeline
│   ├── api.py                # Context retrieval API (port 8001)
│   ├── models.py             # Data models (DiscordMessage, etc.)
│   ├── evidence.py           # Evidence extraction from messages
│   ├── graph_store.py        # Graph storage for context data
│   └── import_history.py     # Historical Discord import
│
├── website/                  # 🌐 Next.js dashboard
│   ├── app/
│   │   ├── page.tsx
│   │   ├── layout.tsx
│   │   └── discord/          # Discord OAuth setup flow
│   └── package.json
│
└── tests/                    # ✅ Test suite
    ├── test_github_analysis.py
    ├── test_knowledge.py
    ├── test_continuous_sync.py
    └── ...
```

<br />

---

<br />

## 🧪 Running Tests

```bash
pip install pytest

# Run the full test suite
python -m pytest tests/ -v

# Run a specific test file
python -m pytest tests/test_github_analysis.py -v

# Verify database connection
python test_connection.py
```

<br />

---

<br />

## 🔒 Security

<table>
<tr>
<td>🔑</td>
<td><strong>API Authentication</strong></td>
<td>All internal endpoints require <code>INVESTIGATION_API_KEY</code> via <code>Authorization: Bearer</code> or <code>X-API-Key</code> header</td>
</tr>
<tr>
<td>🛡️</td>
<td><strong>Webhook Verification</strong></td>
<td>GitHub webhooks verified with HMAC-SHA256 (<code>GITHUB_WEBHOOK_SECRET</code>)</td>
</tr>
<tr>
<td>🏠</td>
<td><strong>Multi-Tenant Isolation</strong></td>
<td>Each Discord server gets its own FalkorDB graph — data never leaks between servers</td>
</tr>
<tr>
<td>🧹</td>
<td><strong>Input Validation</strong></td>
<td>Repository names, task IDs, and user inputs are strictly validated against regex patterns</td>
</tr>
<tr>
<td>⏱️</td>
<td><strong>Rate Limit Handling</strong></td>
<td>GitHub API responses are cached (120s TTL) with automatic backoff on rate limits</td>
</tr>
</table>

<br />

---

<br />

## 🤝 Contributing

Contributions are welcome! Here's how to get started:

1. **Fork** the repository
2. **Create** a feature branch (`git checkout -b feature/amazing-feature`)
3. **Commit** your changes (`git commit -m 'Add amazing feature'`)
4. **Push** to the branch (`git push origin feature/amazing-feature`)
5. **Open** a Pull Request

<br />

---
<br />

🤖 AI Disclosure

This project was built with the assistance of AI coding tools.

| Area | AI Assistance Used |
|:-----|:-------------------|
| **Code generation** | Claude (Anthropic) assisted with boilerplate, regex patterns, Cypher query construction, and test scaffolding |
| **PR review & debugging** | Claude was used to review pull requests, identify edge cases, and suggest fixes (e.g. PR #15 structured source extraction) |
| **Documentation** | This README and inline docstrings were drafted and refined with AI assistance |
| **Architecture decisions** | Core architecture — graph schema, zero-token routing, multi-tenant isolation — **was designed and validated by the team**; AI assisted with implementation details |

**What the team is responsible for:**
Every line of code, architectural choice, graph schema design, and the overall system concept was understood, reviewed, and owned by the team. AI tools were used to accelerate development, not to replace engineering judgment. All team members can explain what was built and why.

<br />

---
<br />

## 📄 License

This project is licensed under the **MIT License** — see the [LICENSE](LICENSE) file for details.

<br />

---

<p align="center">
  <img src="https://capsule-render.vercel.app/api?type=waving&color=0:6C63FF,100:3B82F6&height=120&section=footer" width="100%" />
</p>

<p align="center">
  <sub>Powered by <b>FalkorDB</b> · <b>Gemini AI</b> · <b>FastAPI</b> · <b>Discord.py</b></sub>
</p>


