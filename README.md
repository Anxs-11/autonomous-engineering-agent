# Autonomous Engineering Agent (AEA)

> An AI-powered agent that reads Jira tickets and autonomously writes code and opens GitHub Pull Requests — with zero human intervention.

## Overview

AEA connects your Jira project management workflow directly to your GitHub codebase. When a developer creates a Jira ticket describing a feature or bug fix, AEA automatically classifies the ticket, asks clarifying questions if needed, reads the relevant parts of the codebase, generates code changes, and opens a Pull Request on GitHub — all without a developer writing a single line of code.

---

## Progress

| Week | Feature | Status |
|------|---------|--------|
| Week 1 | Jira webhook ingestion + Claude classification + SQLite storage | ✅ Done |
| Week 2 | Clarification loop — post questions to Jira, re-classify on reply | ✅ Done |
| Week 3 | GitHub fetcher + ChromaDB RAG infrastructure | ✅ Done |
| Week 4 | Two-pass code generation + GitHub PR creation | ✅ Done |
| Week 5 | Slack notifications | 🔲 Upcoming |
| Week 6 | PR review loop | 🔲 Upcoming |

---

## How It Works

### 1. Ticket Created in Jira
Jira fires a webhook to AEA. The agent classifies the ticket into one of four labels:

| Label | Meaning |
|---|---|
| `AUTOMATABLE` | Clear, specific, small-scope — agent handles it autonomously |
| `CLARIFICATION` | Vague or missing requirements — agent asks questions in Jira first |
| `COMPLEX` | Large feature / multi-service — flags for human architecture review |
| `NONCODE` | Documentation, HR, process — no code change needed |

### 2. Clarification Loop (if needed)
If the ticket is vague, AEA posts targeted clarifying questions directly as a Jira comment. When the developer replies, the webhook fires again and AEA re-classifies with the full conversation context. Once clear, it proceeds automatically.

### 3. Repo Selection via Jira Labels
The target GitHub repository is read from a `repo:owner/name` label on the Jira ticket — no hardcoded config needed. Optionally, a `branch:branchname` label overrides the default target branch.

### 4. Two-Pass Code Generation
**Pass 1 — File Selection:** Claude receives the full repository file tree and the ticket description. It returns a list of the specific files it needs to read to solve the ticket.

**Pass 2 — Code Generation:** Claude receives the full content of those selected files and generates a JSON diff of changes: which files to create or modify, and exactly what the new content should be.

### 5. GitHub PR Creation
AEA creates a feature branch (`{ticket-id}-dev`), commits every changed file, and opens a Pull Request targeting the `dev` branch — with a description explaining every change made.

### 6. Retry Without a New Ticket
Adding the `aea-retry` label to any existing ticket re-triggers the full code generation flow — no need to create a new ticket.

---

## Project Structure

```
aea/
├── app/
│   ├── main.py                        # FastAPI application & lifespan
│   ├── routes/
│   │   └── webhook.py                 # POST /webhook/jira — full state machine
│   ├── services/
│   │   ├── classifier.py              # Claude ticket classification
│   │   ├── question_generator.py      # Claude clarifying question generation
│   │   ├── code_generator.py          # Two-pass code generation (Pass 1 + Pass 2)
│   │   ├── github_fetcher.py          # GitHub API: file tree + file content
│   │   ├── github_pr.py               # GitHub API: branch, commits, PR creation
│   │   ├── jira_client.py             # Jira API: post comments, read comments
│   │   └── rag/
│   │       ├── indexer.py             # ChromaDB vector indexing (fault-tolerant)
│   │       └── retriever.py           # ChromaDB semantic retrieval
│   ├── models/
│   │   └── ticket.py                  # Pydantic request/response models
│   └── db/
│       └── database.py                # SQLAlchemy engine, session, ORM model
├── .env                               # Environment variables (never commit)
├── .env.example                       # Template — copy and fill in your values
├── requirements.txt
└── README.md
```

---

## Tech Stack

- **FastAPI + uvicorn** — async webhook server
- **Claude (Anthropic / Azure)** — ticket classification, question generation, code generation
- **GitHub REST API** — file tree reading, branch creation, file commits, PR management
- **Jira REST API v3** — comment posting (ADF format), label reading, comment reading
- **SQLite + SQLAlchemy** — ticket state persistence
- **ChromaDB** — RAG vector store (built, ready for activation)
- **ngrok** — local tunnel for Jira webhook delivery during development

---

## Setup

### 1. Create and activate a virtual environment

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment variables

Copy `.env.example` to `.env` and fill in your real values:

```env
ANTHROPIC_API_KEY=your-key-here
ANTHROPIC_BASE_URL=https://your-azure-endpoint.services.ai.azure.com/anthropic

DATABASE_URL=sqlite:///./aea.db

JIRA_WEBHOOK_SECRET=           # leave blank to skip signature verification
JIRA_BASE_URL=https://your-org.atlassian.net/
JIRA_EMAIL=you@example.com
JIRA_API_TOKEN=your-jira-token
JIRA_BOT_ACCOUNT_ID=your-bot-account-id

GITHUB_TOKEN=ghp_your-token-here
GITHUB_REPO=                   # leave blank — repo comes from Jira labels
GITHUB_DEV_BRANCH=dev          # default PR target branch
GITHUB_BRANCH=main
GITHUB_REPO_PATH_FILTER=       # optional subfolder filter e.g. src/
```

### 4. Run the server

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### 5. Expose locally with ngrok (for Jira webhook delivery)

```bash
ngrok http 8000
```

Register the ngrok URL (`https://xxxx.ngrok.io/webhook/jira`) as a webhook in your Jira project settings.

---

## Usage

### Triggering the Agent

1. Create a Jira ticket with a clear description
2. Add a label `repo:owner/repository-name` to tell AEA which GitHub repo to work on
3. AEA classifies the ticket — if automatable, it reads the codebase and opens a PR automatically

### Retrying Code Generation

Add the label `aea-retry` to any existing ticket to re-trigger code generation without creating a new ticket.

### Targeting a Specific Branch

Add a label `branch:branchname` to override the default `dev` target branch for the PR.

---

## Ticket Lifecycle

```
jira:issue_created
       │
       ▼
  [Classify]
       │
       ├── NONCODE / COMPLEX → stored, no action
       │
       ├── CLARIFICATION → post questions to Jira → AWAITING_CLARIFICATION
       │                        │
       │                   human replies
       │                        │
       │                   re-classify with context
       │                        │
       ├── AWAITING_REPO → post comment asking for repo: label
       │                        │
       │                   label added → aea-retry → re-trigger
       │
       └── AUTOMATABLE + repo label
                   │
                   ▼
            [Pass 1] Claude selects files from tree
                   │
                   ▼
            [Pass 2] Claude generates code changes
                   │
                   ▼
            Create branch → commit files → open PR
                   │
                   ▼
            Post PR link as Jira comment
```

---

## API Endpoints

### `GET /health`

```json
{ "status": "ok", "service": "AEA" }
```

### `POST /webhook/jira`

Receives `jira:issue_created`, `jira:issue_updated`, and `comment_created` events.

---

## Interactive Docs

Once the server is running, visit:

- Swagger UI: <http://localhost:8000/docs>
- ReDoc: <http://localhost:8000/redoc>
