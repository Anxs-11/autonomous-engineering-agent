from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from enum import Enum


class ClassificationLabel(str, Enum):
    AUTOMATABLE = "AUTOMATABLE"
    CLARIFICATION = "CLARIFICATION"
    COMPLEX = "COMPLEX"
    NONCODE = "NONCODE"


class JiraWebhookPayload(BaseModel):
    """Raw Jira webhook payload (top-level shape)."""
    webhookEvent: str
    issue: dict


class TicketCreate(BaseModel):
    """Internal model for creating a ticket record."""
    ticket_id: str
    title: str
    description: Optional[str] = ""
    assignee: Optional[str] = None


class ClassificationResult(BaseModel):
    """Result returned by the classifier service."""
    label: ClassificationLabel
    reason: str


class TicketResponse(BaseModel):
    """API response shape for a processed ticket."""
    id: int
    ticket_id: str
    title: str
    description: Optional[str]
    assignee: Optional[str]
    classification: str
    reason: str
    created_at: datetime

    model_config = {"from_attributes": True}
