# Autonomous Engineering Agent (AEA) — Week 1

> **Scope:** Jira Ticket Ingestion & Classification only.

## Overview

The AEA receives Jira issue events via webhook, classifies each ticket using
OpenAI GPT-4o into one of four labels, and persists the result to a PostgreSQL
database. Future weeks will add RAG-based codebase understanding, multi-agent
code generation, GitHub PR creation, and Slack notifications.

---

## Project Structure

```
aea/
├── app/
│   ├── main.py               # FastAPI application & lifespan
│   ├── routes/
│   │   └── webhook.py        # POST /webhook/jira
│   ├── services/
│   │   └── classifier.py     # GPT-4o classification logic
│   ├── models/
│   │   └── ticket.py         # Pydantic models
│   └── db/
│       └── database.py       # SQLAlchemy engine, session, ORM model
├── .env                      # Environment variables (never commit real values)
├── requirements.txt
└── README.md
```

---

## Classification Labels

| Label           | Meaning |
|-----------------|---------|
| `AUTOMATABLE`   | Clear, specific, small-scope — agent handles autonomously |
| `CLARIFICATION` | Vague or missing requirements — needs human input first |
| `COMPLEX`       | Large feature / multi-service — requires architecture review |
| `NONCODE`       | Documentation, HR, process, design — no code change needed |

---

## Prerequisites

- Python 3.11+
- PostgreSQL 14+ running locally (or any reachable instance)
- An OpenAI API key with access to `gpt-4o`

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

Copy `.env` and fill in your real values:

```bash
# .env
OPENAI_API_KEY=sk-...
DATABASE_URL=postgresql://aea_user:aea_password@localhost:5432/aea
JIRA_WEBHOOK_SECRET=your-secret-here   # leave blank to skip sig verification
```

### 4. Create the PostgreSQL database

```sql
CREATE DATABASE aea;
CREATE USER aea_user WITH PASSWORD 'aea_password';
GRANT ALL PRIVILEGES ON DATABASE aea TO aea_user;
```

Tables are created automatically on first startup via `init_db()`.

### 5. Run the server

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

---

## API Endpoints

### `GET /health`

Liveness probe.

```json
{ "status": "ok", "service": "AEA", "week": 1 }
```

### `POST /webhook/jira`

Receives Jira issue webhook events.

**Expected Jira payload (simplified):**

```json
{
  "webhookEvent": "jira:issue_created",
  "issue": {
    "key": "ENG-42",
    "fields": {
      "summary": "Fix NullPointerException in UserService",
      "description": "Occurs when user.email is null during login.",
      "assignee": { "displayName": "Jane Doe" }
    }
  }
}
```

**Response:**

```json
{
  "id": 1,
  "ticket_id": "ENG-42",
  "title": "Fix NullPointerException in UserService",
  "description": "Occurs when user.email is null during login.",
  "assignee": "Jane Doe",
  "classification": "AUTOMATABLE",
  "reason": "Clear bug with reproduction steps and small, isolated scope.",
  "created_at": "2026-03-01T10:00:00"
}
```

---

## Testing Locally with curl

```bash
curl -X POST http://localhost:8000/webhook/jira \
  -H "Content-Type: application/json" \
  -d '{
    "webhookEvent": "jira:issue_created",
    "issue": {
      "key": "ENG-42",
      "fields": {
        "summary": "Fix NullPointerException in UserService",
        "description": "Occurs when user.email is null during login.",
        "assignee": { "displayName": "Jane Doe" }
      }
    }
  }'
```

---

## Interactive Docs

Once the server is running, visit:

- Swagger UI: <http://localhost:8000/docs>
- ReDoc:       <http://localhost:8000/redoc>
