# Autonomous Engineering Agent (AEA)

> An AI-powered agent that reads Jira tickets and autonomously writes code and opens GitHub Pull Requests — with zero human intervention.

## Overview

AEA connects your Jira project management workflow directly to your GitHub codebase. When a developer creates a Jira ticket describing a feature or bug fix, AEA automatically classifies the ticket, asks clarifying questions if needed, reads the relevant parts of the codebase, generates code changes, and opens a Pull Request on GitHub — all without a developer writing a single line of code.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                                                                                 │
│   DEVELOPER                                                                     │
│   Creates / updates Jira ticket                                                 │
│                                                                                 │
└──────────────────────────────┬──────────────────────────────────────────────────┘
                               │  Webhook  (POST /webhook/jira)
                               ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│  JIRA CLOUD                                                                     │
│  ┌──────────────────────────────────────────────────────────────────────────┐   │
│  │  Issue Created / Updated / Comment Added                                 │   │
│  └──────────────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────┬──────────────────────────────────────────────────┘
                               │
                               ▼
╔═════════════════════════════════════════════════════════════════════════════════╗
║  AEA  —  Autonomous Engineering Agent  (FastAPI · uvicorn)                     ║
║                                                                                 ║
║  ┌───────────────────────────────────────────────────────────────────────────┐  ║
║  │  WEBHOOK HANDLER  (webhook.py)                                            │  ║
║  │                                                                           │  ║
║  │   Receives event ──► reads ticket state from SQLite                      │  ║
║  │                                                                           │  ║
║  │         ┌─────────────────────────────────────────────┐                  │  ║
║  │         │          CLASSIFIER  (Claude)               │                  │  ║
║  │         │                                             │                  │  ║
║  │         │   AUTOMATABLE ──► code generation flow      │                  │  ║
║  │         │   CLARIFICATION ──► ask questions in Jira   │                  │  ║
║  │         │   COMPLEX / NONCODE ──► store & skip        │                  │  ║
║  │         └─────────────────────────────────────────────┘                  │  ║
║  └───────────────────────────────────────────────────────────────────────────┘  ║
║                  │                              │                               ║
║                  │ AUTOMATABLE                  │ CLARIFICATION                 ║
║                  ▼                              ▼                               ║
║  ┌──────────────────────────┐    ┌──────────────────────────────────────────┐  ║
║  │  CODE GENERATOR          │    │  QUESTION GENERATOR  (Claude)            │  ║
║  │                          │    │                                          │  ║
║  │  Pass 1 ──► Claude       │    │  Generates targeted questions            │  ║
║  │  reads file tree,        │    │  ──► posts as Jira comment               │  ║
║  │  selects relevant files  │    │  ──► waits for developer reply           │  ║
║  │                          │    └──────────────────────────────────────────┘  ║
║  │  Pass 2 ──► Claude       │                                                  ║
║  │  reads full file content,│                                                  ║
║  │  generates code changes  │                                                  ║
║  └──────────┬───────────────┘                                                  ║
║             │                                                                   ║
║             ▼                                                                   ║
║  ┌──────────────────────────┐    ┌──────────────────────────────────────────┐  ║
║  │  GITHUB FETCHER          │    │  GITHUB PR CREATOR                       │  ║
║  │                          │    │                                          │  ║
║  │  • Fetch full file tree  │    │  • Create feature branch                 │  ║
║  │  • Fetch file contents   │    │  • Commit changed files                  │  ║
║  │    via GitHub REST API   │    │  • Open Pull Request ──► returns PR URL  │  ║
║  └──────────────────────────┘    └──────────────┬───────────────────────────┘  ║
║                                                  │                              ║
║             ┌────────────────────────────────────┘                             ║
║             │  PR URL                                                           ║
║             ▼                                                                   ║
║  ┌──────────────────────────────────────────────────────────────────────────┐  ║
║  │  NOTIFIER                                                                │  ║
║  │                                                                          │  ║
║  │  ──► Post PR link as Jira comment                                        │  ║
║  │  ──► Send rich Slack notification  (ticket · repo · files · PR button)   │  ║
║  │  ──► Transition Jira ticket to "In Progress"                             │  ║
║  └──────────────────────────────────────────────────────────────────────────┘  ║
║                                                                                 ║
╚═════════════════════════════════════════════════════════════════════════════════╝
          │                          │                          │
          ▼                          ▼                          ▼
┌──────────────────┐    ┌────────────────────┐    ┌────────────────────────────┐
│   GITHUB REPO    │    │    JIRA CLOUD      │    │     SLACK CHANNEL          │
│                  │    │                    │    │                            │
│  Feature branch  │    │  PR link comment   │    │  PR created notification   │
│  File commits    │    │  Status updated    │    │  with direct PR button     │
│  Pull Request    │    │  to In Progress    │    │                            │
└──────────────────┘    └────────────────────┘    └────────────────────────────┘
```

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
**Pass 1 — File Selection:** Claude receives the full repository file tree (fetched live from GitHub) and the ticket description. It returns a list of the specific files it needs to read to solve the ticket.

**Pass 2 — Code Generation:** Claude receives the full content of those selected files (fetched live from GitHub) and generates a JSON diff of changes: which files to create or modify, and exactly what the new content should be.

No vector database is involved — the agent reads the codebase directly via the GitHub REST API, which ensures it always works with the latest code.

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
│   │   ├── webhook.py                 # POST /webhook/jira — full state machine
│   │   └── github_webhook.py          # POST /webhook/github — PR review loop
│   ├── services/
│   │   ├── classifier.py              # Claude ticket classification
│   │   ├── question_generator.py      # Claude clarifying question generation
│   │   ├── code_generator.py          # Two-pass code generation + review revision
│   │   ├── github_fetcher.py          # GitHub API: file tree + file content
│   │   ├── github_pr.py               # GitHub API: branch, commits, PR creation, review helpers
│   │   ├── jira_client.py             # Jira API: post comments, read comments
│   │   └── slack_notifier.py          # Slack Incoming Webhook notifications
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
# Anthropic / Azure AI
ANTHROPIC_API_KEY=your-key-here
ANTHROPIC_BASE_URL=https://your-azure-endpoint.services.ai.azure.com/anthropic
ANTHROPIC_MODEL=claude-sonnet-4-5   # optional — defaults to claude-sonnet-4-5

# Database
DATABASE_URL=sqlite:///./aea.db     # or a PostgreSQL URL for production

# Jira
JIRA_WEBHOOK_SECRET=                # leave blank to skip signature verification
JIRA_BASE_URL=https://your-org.atlassian.net/
JIRA_EMAIL=you@example.com
JIRA_API_TOKEN=your-jira-token
JIRA_BOT_ACCOUNT_ID=your-bot-account-id

# GitHub
GITHUB_TOKEN=ghp_your-token-here
GITHUB_WEBHOOK_SECRET=              # leave blank to skip signature verification
GITHUB_REPO=                        # leave blank — repo comes from Jira labels
GITHUB_DEV_BRANCH=dev               # default PR target branch
GITHUB_BRANCH=main
GITHUB_REPO_PATH_FILTER=            # optional subfolder filter e.g. src/

# Slack
SLACK_WEBHOOK_URL=                  # Slack Incoming Webhook URL — leave blank to disable
```

### 4. Run the server

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### 5. Expose locally with ngrok (for webhook delivery)

```bash
ngrok http 8000
```

Register webhooks in two places:
- **Jira:** `https://xxxx.ngrok.io/webhook/jira` — in your Jira project → Settings → Webhooks
- **GitHub:** `https://xxxx.ngrok.io/webhook/github` — in your GitHub repo → Settings → Webhooks (events: Pull request reviews, Pull request review comments)

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
                   │
                   ▼
        [Reviewer submits feedback on GitHub PR]
                   │
                   ▼
        Read review comments → re-generate code
                   │
                   ▼
        Commit revised files to same feature branch
                   │
                   ▼
        Post confirmation comment on PR
```

---

## API Endpoints

### `GET /health`

```json
{ "status": "ok", "service": "AEA" }
```

### `POST /webhook/jira`

Receives `jira:issue_created`, `jira:issue_updated`, and `comment_created` events.

### `POST /webhook/github`

Receives GitHub `pull_request_review` and `pull_request_review_comment` events. When a reviewer requests changes, AEA reads all feedback, regenerates the affected files, commits the revision to the feature branch, and posts a confirmation comment on the PR.

---

## Interactive Docs

Once the server is running, visit:

- Swagger UI: <http://localhost:8000/docs>
- ReDoc: <http://localhost:8000/redoc>
