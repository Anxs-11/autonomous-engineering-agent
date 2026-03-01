import hashlib
import hmac
import json
import os
from typing import Optional

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.db.database import TicketRecord, get_db
from app.models.ticket import TicketResponse
from app.services.classifier import classify_ticket

load_dotenv()

router = APIRouter(prefix="/webhook", tags=["Webhook"])

JIRA_WEBHOOK_SECRET: str = os.getenv("JIRA_WEBHOOK_SECRET", "")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _verify_jira_signature(body: bytes, secret: str, signature: Optional[str]) -> bool:
    """
    Validate the HMAC-SHA256 signature Jira attaches to webhook requests.

    If no secret is configured the check is skipped (useful for local dev).
    """
    if not secret:
        return True
    if not signature:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _extract_text_from_adf(node: dict) -> str:
    """
    Recursively walk an Atlassian Document Format (ADF) tree and collect
    all plain-text leaf nodes into a single string.
    """
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return node.get("text", "")
    return " ".join(
        _extract_text_from_adf(child)
        for child in node.get("content", [])
        if _extract_text_from_adf(child)
    ).strip()


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/jira", response_model=TicketResponse, status_code=200)
async def jira_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_hub_signature_256: Optional[str] = Header(None, alias="X-Hub-Signature-256"),
):
    """
    Receive Jira ``issue_created`` / ``issue_updated`` webhook events.

    Flow
    ----
    1. Verify HMAC-SHA256 signature (skipped when ``JIRA_WEBHOOK_SECRET`` is empty).
    2. Parse and validate the payload.
    3. Extract ``ticket_id``, ``title``, ``description``, ``assignee``.
    4. Classify the ticket via GPT-4o.
    5. Upsert the result into PostgreSQL and return the stored record.
    """
    # Read raw body once so we can both verify the signature and parse JSON.
    body: bytes = await request.body()

    # --- Signature verification ---
    if JIRA_WEBHOOK_SECRET and not _verify_jira_signature(
        body, JIRA_WEBHOOK_SECRET, x_hub_signature_256
    ):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")

    # --- Parse JSON ---
    try:
        payload: dict = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=422, detail="Request body is not valid JSON.")

    # --- Event filter ---
    webhook_event: str = payload.get("webhookEvent", "")
    if webhook_event not in ("jira:issue_created", "jira:issue_updated"):
        # Acknowledge receipt but take no action for other events.
        return HTTPException(
            status_code=200,
            detail=f"Event '{webhook_event}' acknowledged but not processed.",
        )

    # --- Field extraction ---
    issue: dict = payload.get("issue", {})
    fields: dict = issue.get("fields", {})

    ticket_id: str = issue.get("key") or "UNKNOWN"
    title: str = fields.get("summary") or "No title"

    raw_description = fields.get("description") or ""
    if isinstance(raw_description, dict):
        # Atlassian Document Format
        description: str = _extract_text_from_adf(raw_description)
    else:
        description = str(raw_description).strip()

    assignee_field = fields.get("assignee")
    assignee: Optional[str] = (
        assignee_field.get("displayName") if isinstance(assignee_field, dict) else None
    )

    # --- Classification ---
    result = classify_ticket(title, description)

    # --- Upsert into database ---
    existing: Optional[TicketRecord] = (
        db.query(TicketRecord).filter(TicketRecord.ticket_id == ticket_id).first()
    )

    if existing:
        existing.title = title
        existing.description = description
        existing.assignee = assignee
        existing.classification = result.label.value
        existing.reason = result.reason
        db.commit()
        db.refresh(existing)
        return existing

    record = TicketRecord(
        ticket_id=ticket_id,
        title=title,
        description=description,
        assignee=assignee,
        classification=result.label.value,
        reason=result.reason,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record
