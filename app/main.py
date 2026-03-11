import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db.database import init_db
from app.routes.webhook import router as webhook_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan handler.

    Startup : Ensure all database tables exist before accepting traffic.
    Shutdown: No teardown needed for now (connection pool is managed by SQLAlchemy).
    """
    init_db()
    yield


app = FastAPI(
    title="Autonomous Engineering Agent (AEA)",
    description=(
        "Week 1 — Jira Ticket Ingestion & Classification.\n\n"
        "Receives Jira webhook events, classifies each ticket with GPT-4o, "
        "and persists the result to PostgreSQL."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(webhook_router)


@app.get("/health", tags=["Health"])
async def health_check():
    """Liveness probe — returns 200 when the service is running."""
    return {"status": "ok", "service": "AEA", "week": 1}
