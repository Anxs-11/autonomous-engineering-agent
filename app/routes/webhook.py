import concurrent.futures
import hashlib
import hmac
import json
import logging
import os
from typing import Optional

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.db.database import TicketRecord, get_db
from app.models.ticket import ClassificationLabel, TicketResponse
from app.services.classifier import classify_ticket
from app.services.code_generator import generate_code_changes
from app.services.github_pr import create_pull_request
from app.services.jira_client import get_human_comments, post_comment, remove_label, transition_to_in_progress
from app.services.question_generator import generate_clarifying_questions
from app.services.slack_notifier import notify_pr_created, notify_pr_failed
from app.services.rag.indexer import index_repository, is_indexed
from app.services.rag.retriever import format_context_for_llm, retrieve_relevant_chunks

load_dotenv()

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["Webhook"])

JIRA_WEBHOOK_SECRET: str = os.getenv("JIRA_WEBHOOK_SECRET", "")
GITHUB_REPO: str = os.getenv("GITHUB_REPO", "")
GITHUB_BRANCH: str = os.getenv("GITHUB_BRANCH", "main")
GITHUB_DEV_BRANCH: str = os.getenv("GITHUB_DEV_BRANCH", "dev")

_pr_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="pr")

_rag_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="rag")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _verify_jira_signature(body: bytes, secret: str, signature: Optional[str]) -> bool:
    if not secret:
        return True
    if not signature:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _extract_text_from_adf(node: dict) -> str:
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return node.get("text", "")
    return " ".join(
        _extract_text_from_adf(child)
        for child in node.get("content", [])
        if _extract_text_from_adf(child)
    ).strip()


def _parse_issue_fields(payload: dict) -> tuple[str, str, str, Optional[str], str, str, str]:
    """
    Extract ticket_id, title, description, assignee, repo, base_branch, path_filter
    from payload.

    Repo, base_branch, and path_filter are read from Jira labels:
      - ``repo:owner/reponame``  → overrides GITHUB_REPO env var
      - ``branch:branchname``    → overrides GITHUB_DEV_BRANCH env var
      - ``path:src/billing/``   → scopes file scan to that subfolder
    Falls back to env vars when labels are absent.
    """
    issue: dict = payload.get("issue", {})
    fields: dict = issue.get("fields", {})

    ticket_id: str = issue.get("key") or "UNKNOWN"
    title: str = fields.get("summary") or "No title"

    raw_desc = fields.get("description") or ""
    description: str = (
        _extract_text_from_adf(raw_desc)
        if isinstance(raw_desc, dict)
        else str(raw_desc).strip()
    )

    assignee_field = fields.get("assignee")
    assignee: Optional[str] = (
        assignee_field.get("displayName") if isinstance(assignee_field, dict) else None
    )

    # Parse repo, branch, and path filter overrides from ticket labels
    repo: str = GITHUB_REPO
    base_branch: str = GITHUB_DEV_BRANCH
    path_filter: str = os.getenv("GITHUB_REPO_PATH_FILTER", "")
    labels: list = fields.get("labels") or []
    for label in labels:
        if isinstance(label, str):
            if label.startswith("repo:"):
                repo = label[len("repo:"):].strip()
                logger.info("Ticket %s: repo override from label → %s", ticket_id, repo)
            elif label.startswith("branch:"):
                base_branch = label[len("branch:"):].strip()
                logger.info("Ticket %s: branch override from label → %s", ticket_id, base_branch)
            elif label.startswith("path:"):
                path_filter = label[len("path:"):].strip()
                logger.info("Ticket %s: path filter from label → %s", ticket_id, path_filter)

    return ticket_id, title, description, assignee, repo, base_branch, path_filter


def _trigger_code_generation(
    ticket_id: str,
    title: str,
    description: str,
    repo: str,
    base_branch: str,
    path_filter: str = "",
) -> None:
    """
    Two-pass code generation in a background thread:
      1. AEA reads full repo contents → selects relevant files
      2. AEA generates code changes from selected files
    Opens a GitHub PR on *base_branch* and posts the URL to Jira.

    Feature branch: {ticket_id}-dev  (e.g. aea-123-dev)
    PR targets:     *base_branch*     (e.g. dev)
    """
    if not repo:
        logger.warning("No GitHub repo configured — code generation skipped.")
        return

    def _run() -> None:
        import time as _time
        _t_run_start = _time.time()
        logger.info("[%s] ================================================", ticket_id)
        logger.info("[%s] 🎤 AEA pipeline STARTED — '%s'", ticket_id, title)
        logger.info("[%s] ================================================", ticket_id)
        try:
            result = generate_code_changes(
                ticket_id=ticket_id,
                title=title,
                description=description,
                repo=repo,
                branch=base_branch,
                path_filter=path_filter,
            )
            changes = result.get("changes", [])
            explanation = result.get("explanation", "")
            testing_checklist = result.get("testing_checklist", [])

            if not changes:
                msg = (
                    f"AEA could not generate code changes for this ticket.\n\n"
                    f"Reason: {explanation}"
                )
                post_comment(ticket_id, msg)
                logger.warning("No code changes generated for %s: %s", ticket_id, explanation)
                return

            pr_url = create_pull_request(
                ticket_id=ticket_id,
                title=title,
                changes=changes,
                explanation=explanation,
                repo=repo,
                base_branch=base_branch,
                testing_checklist=testing_checklist,
            )
            msg = (
                f"AEA has generated a Pull Request for this ticket:\n\n"
                f"{pr_url}\n\n"
                f"*{len(changes)} file(s) changed. Please review before merging.*"
            )
            post_comment(ticket_id, msg)
            transition_to_in_progress(ticket_id)
            logger.info("PR created for %s: %s", ticket_id, pr_url)
            notify_pr_created(
                ticket_id=ticket_id,
                title=title,
                pr_url=pr_url,
                repo=repo,
                num_files=len(changes),
            )
            _total = _time.time() - _t_run_start
            logger.info("[%s] ================================================", ticket_id)
            logger.info("[%s] ⭐ PIPELINE COMPLETE in %.1fs", ticket_id, _total)
            logger.info("[%s] ================================================", ticket_id)

        except Exception as exc:  # noqa: BLE001
            _total = _time.time() - _t_run_start
            logger.error("[%s] 💥 PIPELINE FAILED after %.1fs: %s", ticket_id, _total, exc)
            post_comment(
                ticket_id,
                f"AEA encountered an error while generating code:\n\n`{exc}`",
            )
            notify_pr_failed(ticket_id=ticket_id, title=title, error=str(exc))

    _pr_executor.submit(_run)


def _fetch_rag_context(title: str, description: str) -> str:
    """
    Retrieve relevant code from ChromaDB for an AUTOMATABLE ticket.

    - If the repo is not yet indexed: triggers indexing in a background thread
      and returns '' (the next AUTOMATABLE ticket will get full context).
    - If already indexed: returns formatted code context immediately.
    - Silently returns '' when GITHUB_REPO is not configured.
    """
    if not GITHUB_REPO:
        logger.info("GITHUB_REPO not set — RAG skipped.")
        return ""
    try:
        if not is_indexed(GITHUB_REPO):
            logger.info(
                "Repo %s not yet indexed. Launching background indexing — "
                "RAG context will be available for the next AUTOMATABLE ticket.",
                GITHUB_REPO,
            )
            _rag_executor.submit(index_repository, GITHUB_REPO, GITHUB_BRANCH)
            return ""

        query = f"{title}\n{description}"
        chunks = retrieve_relevant_chunks(query, GITHUB_REPO)
        return format_context_for_llm(chunks)
    except Exception as exc:  # noqa: BLE001
        logger.error("RAG retrieval failed: %s", exc)
        return ""


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
    Receive Jira webhook events and drive the clarification loop.

    Supported events
    ----------------
    - ``jira:issue_created``  → classify; if CLARIFICATION, post questions to Jira.
    - ``jira:issue_updated``  → if ticket is AWAITING_CLARIFICATION, re-classify
                                with all human comments as added context.
    - ``comment_created``     → same re-classification logic as issue_updated.
    """
    body: bytes = await request.body()

    # --- Signature check ---
    if JIRA_WEBHOOK_SECRET and not _verify_jira_signature(
        body, JIRA_WEBHOOK_SECRET, x_hub_signature_256
    ):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")

    # --- Parse JSON ---
    try:
        payload: dict = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=422, detail="Request body is not valid JSON.")

    webhook_event: str = payload.get("webhookEvent", "")
    supported = ("jira:issue_created", "jira:issue_updated", "comment_created")
    if webhook_event not in supported:
        return {"detail": f"Event '{webhook_event}' acknowledged but not processed."}

    ticket_id, title, description, assignee, ticket_repo, ticket_base_branch, ticket_path_filter = _parse_issue_fields(payload)

    # -----------------------------------------------------------------------
    # CASE 1 — New ticket: classify and decide whether to ask questions
    # -----------------------------------------------------------------------
    if webhook_event == "jira:issue_created":
        result = classify_ticket(title, description)
        logger.info("New ticket %s classified as %s", ticket_id, result.label)

        rag_context = ""
        if result.label == ClassificationLabel.CLARIFICATION:
            questions = generate_clarifying_questions(title, description)
            post_comment(ticket_id, questions)
            status = "AWAITING_CLARIFICATION"
        elif result.label == ClassificationLabel.AUTOMATABLE:
            if not ticket_repo:
                post_comment(
                    ticket_id,
                    "This ticket looks automatable! \u2705\n\n"
                    "However, I need to know which GitHub repository to work on.\n\n"
                    "Please add a label to this ticket in the format:\n"
                    "`repo:owner/repository-name`\n\n"
                    "For example: `repo:Anxs-11/ecommerce-backend`\n\n"
                    "Once you add the label, I will start working on it automatically.",
                )
                status = "AWAITING_REPO"
            else:
                status = result.label.value
                _trigger_code_generation(
                    ticket_id, title, description,
                    repo=ticket_repo, base_branch=ticket_base_branch,
                    path_filter=ticket_path_filter,
                )
        else:
            status = result.label.value

        existing_record: Optional[TicketRecord] = (
            db.query(TicketRecord).filter(TicketRecord.ticket_id == ticket_id).first()
        )
        if existing_record:
            existing_record.title = title
            existing_record.description = description
            existing_record.assignee = assignee
            existing_record.classification = result.label.value
            existing_record.reason = result.reason
            existing_record.status = status
            existing_record.rag_context = rag_context
            db.commit()
            db.refresh(existing_record)
            return existing_record

        record = TicketRecord(
            ticket_id=ticket_id,
            title=title,
            description=description,
            assignee=assignee,
            classification=result.label.value,
            reason=result.reason,
            status=status,
            rag_context=rag_context,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        return record

    # -----------------------------------------------------------------------
    # CASE 2 — Update or new comment: re-classify if awaiting clarification
    # -----------------------------------------------------------------------
    existing: Optional[TicketRecord] = (
        db.query(TicketRecord).filter(TicketRecord.ticket_id == ticket_id).first()
    )

    if not existing:
        # First time we see this ticket — treat it like a new ticket
        result = classify_ticket(title, description)
        if result.label == ClassificationLabel.CLARIFICATION:
            questions = generate_clarifying_questions(title, description)
            post_comment(ticket_id, questions)
            status = "AWAITING_CLARIFICATION"
        elif result.label == ClassificationLabel.AUTOMATABLE:
            if not ticket_repo:
                post_comment(
                    ticket_id,
                    "This ticket looks automatable! \u2705\n\n"
                    "However, I need to know which GitHub repository to work on.\n\n"
                    "Please add a label to this ticket in the format:\n"
                    "`repo:owner/repository-name`\n\n"
                    "For example: `repo:Anxs-11/ecommerce-backend`\n\n"
                    "Once you add the label, I will start working on it automatically.",
                )
                status = "AWAITING_REPO"
            else:
                status = result.label.value
                _trigger_code_generation(
                    ticket_id, title, description,
                    repo=ticket_repo, base_branch=ticket_base_branch,
                    path_filter=ticket_path_filter,
                )
        else:
            status = result.label.value

        record = TicketRecord(
            ticket_id=ticket_id,
            title=title,
            description=description,
            assignee=assignee,
            classification=result.label.value,
            reason=result.reason,
            status=status,
            rag_context="",
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        return record

    # -----------------------------------------------------------------------
    # aea-retry label — force re-trigger from ANY status
    # -----------------------------------------------------------------------
    labels: list = payload.get("issue", {}).get("fields", {}).get("labels") or []
    if "aea-retry" in labels:
        # Remove the label FIRST so subsequent webhooks don't re-trigger
        remove_label(ticket_id, "aea-retry")
        if not ticket_repo:
            post_comment(
                ticket_id,
                "Retry requested but no `repo:` label found on this ticket.\n\n"
                "Please add a label in the format: `repo:owner/repository-name`",
            )
        else:
            logger.info("aea-retry label detected on %s (status: %s) — re-triggering code generation.", ticket_id, existing.status)
            post_comment(ticket_id, "AEA retry triggered — generating code changes...")
            existing.status = "AUTOMATABLE"
            existing.title = title
            existing.description = description
            existing.assignee = assignee
            db.commit()
            _trigger_code_generation(
                ticket_id, title, description,
                repo=ticket_repo, base_branch=ticket_base_branch,
                path_filter=ticket_path_filter,
            )
            db.refresh(existing)
            return existing
        db.refresh(existing)
        return existing

    if existing.status not in ("AWAITING_CLARIFICATION", "AWAITING_REPO"):
        # Ticket already resolved — update fields and return
        existing.title = title
        existing.description = description
        existing.assignee = assignee
        db.commit()
        db.refresh(existing)
        return existing

    # -----------------------------------------------------------------------
    # AWAITING_REPO — developer added the repo label, now we can proceed
    # -----------------------------------------------------------------------
    if existing.status == "AWAITING_REPO":
        if not ticket_repo:
            # Label still not added — nothing to do yet
            db.refresh(existing)
            return existing
        logger.info("Ticket %s now has repo label %s — triggering code generation.", ticket_id, ticket_repo)
        _trigger_code_generation(
            ticket_id, title, description,
            repo=ticket_repo, base_branch=ticket_base_branch,
            path_filter=ticket_path_filter,
        )
        existing.status = "AUTOMATABLE"
        existing.rag_context = ""
        existing.title = title
        existing.description = description
        existing.assignee = assignee
        db.commit()
        db.refresh(existing)
        return existing

    # --- Re-classify with human comments as extra context ---
    human_comments = get_human_comments(ticket_id)
    if not human_comments:
        # No human replies yet — nothing new to act on
        db.refresh(existing)
        return existing

    enriched_description = (
        description
        + "\n\nDeveloper clarification comments:\n"
        + "\n".join(f"- {c}" for c in human_comments)
    )

    result = classify_ticket(title, enriched_description)
    logger.info(
        "Re-classified %s as %s after %d comment(s)",
        ticket_id, result.label, len(human_comments),
    )

    if result.label == ClassificationLabel.CLARIFICATION:
        # Still not clear — generate targeted follow-up questions
        follow_up = generate_clarifying_questions(title, description, human_comments)
        post_comment(ticket_id, follow_up)
        new_status = "AWAITING_CLARIFICATION"
    else:
        new_status = result.label.value
        if result.label == ClassificationLabel.AUTOMATABLE:
            _trigger_code_generation(
                ticket_id, title, description,
                repo=ticket_repo, base_branch=ticket_base_branch,
                path_filter=ticket_path_filter,
            )
        logger.info("Ticket %s is now %s — ready for next stage.", ticket_id, new_status)
    existing.title = title
    existing.description = description
    existing.assignee = assignee
    existing.classification = result.label.value
    existing.reason = result.reason
    existing.status = new_status
    existing.rag_context = ""
    db.commit()
    db.refresh(existing)
    return existing
