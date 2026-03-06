import logging
import os
from datetime import datetime

from dotenv import load_dotenv
from sqlalchemy import Column, DateTime, Integer, String, Text, create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

load_dotenv()

logger = logging.getLogger(__name__)

# Default to SQLite for local development — set DATABASE_URL in .env for PostgreSQL.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./aea.db")

# SQLite requires check_same_thread=False; PostgreSQL/other DBs don't need it.
_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args=_connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


class TicketRecord(Base):
    """SQLAlchemy ORM model for the `tickets` table."""

    __tablename__ = "tickets"

    id = Column(Integer, primary_key=True, index=True)
    ticket_id = Column(String(50), unique=True, index=True, nullable=False)
    title = Column(String(500), nullable=False)
    description = Column(Text, nullable=True)
    assignee = Column(String(255), nullable=True)
    classification = Column(String(50), nullable=False)
    reason = Column(Text, nullable=False)
    status = Column(String(50), nullable=False, default="AWAITING_CLARIFICATION")
    rag_context = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


def get_db():
    """FastAPI dependency that yields a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create all tables and run safe column migrations."""
    try:
        Base.metadata.create_all(bind=engine)
        _migrate_add_status_column()
        _migrate_add_rag_context_column()
        logger.info("Database tables verified/created against: %s", DATABASE_URL)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not connect to database at startup: %s. "
            "Requests that require DB access will fail until the DB is reachable.",
            exc,
        )


def _migrate_add_rag_context_column() -> None:
    """Safely add the 'rag_context' TEXT column to existing tickets tables."""
    try:
        with engine.connect() as conn:
            if DATABASE_URL.startswith("sqlite"):
                result = conn.execute(text("PRAGMA table_info(tickets)"))
                columns = [row[1] for row in result.fetchall()]
            else:
                result = conn.execute(text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='tickets'"
                ))
                columns = [row[0] for row in result.fetchall()]

            if "rag_context" not in columns:
                conn.execute(text("ALTER TABLE tickets ADD COLUMN rag_context TEXT"))
                conn.commit()
                logger.info("Migrated: added 'rag_context' column to tickets table.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not run rag_context column migration: %s", exc)


def _migrate_add_status_column() -> None:
    """Safely add the 'status' column to existing tickets tables (SQLite compatible)."""
    try:
        with engine.connect() as conn:
            if DATABASE_URL.startswith("sqlite"):
                result = conn.execute(text("PRAGMA table_info(tickets)"))
                columns = [row[1] for row in result.fetchall()]
            else:
                result = conn.execute(text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='tickets'"
                ))
                columns = [row[0] for row in result.fetchall()]

            if "status" not in columns:
                conn.execute(text(
                    "ALTER TABLE tickets ADD COLUMN status VARCHAR(50) "
                    "DEFAULT 'AWAITING_CLARIFICATION'"
                ))
                conn.commit()
                logger.info("Migrated: added 'status' column to tickets table.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not run status column migration: %s", exc)
