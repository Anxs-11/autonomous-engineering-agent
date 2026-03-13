"""
Week 6 — GitHub Webhook Handler (PR Review Loop)

Listens for GitHub Pull Request review events. When a reviewer submits
feedback (changes requested or comments), AEA:

  1. Reads all review comments on the PR
  2. Re-generates the code addressing every comment
  3. Commits the updated files to the feature branch (PR auto-updates)
  4. Posts a PR comment confirming the changes

Setup
-----
GitHub repo → Settings → Webhooks → Add webhook
  Payload URL : https://<your-ngrok>.ngrok.io/webhook/github
  Content type: application/json
  Secret      : value of GITHUB_WEBHOOK_SECRET in .env
  Events      : Pull request reviews, Pull request review comments
"""

import concurrent.futures
import hashlib
import hmac
import json
import logging
import os
import re
from typing import Optional

from dotenv import load_dotenv
from fastapi import APIRouter, Header, HTTPException, Request

from app.services.code_generator import generate_revision_from_review
from app.services.github_pr import (
    commit_revision,
    get_pr_files,
    get_pr_review_comments,
    post_pr_comment,
)

load_dotenv()

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["GitHub Webhook"])

GITHUB_WEBHOOK_SECRET: str = os.getenv("GITHUB_WEBHOOK_SECRET", "")

_review_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="review"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _verify_signature(body: bytes, secret: str, signature: Optional[str]) -> bool:
    if not secret:
        return True
    if not signature:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _extract_ticket_id(pr_title: str, head_branch: str) -> str:
    """
    Derive the Jira ticket ID from the PR title (e.g. "[ES-9005] Add ...")
    or fall back to uppercasing the branch prefix (e.g. "es-9005-dev" → "ES-9005").
    """
    match = re.search(r"\[([A-Z]+-\d+)\]", pr_title, re.IGNORECASE)
    if match:
        return match.group(1).upper()
    # Fallback: "es-9005-dev" → "ES-9005"
    branch_match = re.match(r"^([a-z]+-\d+)-dev$", head_branch)
    if branch_match:
        return branch_match.group(1).upper()
    return head_branch.upper()


def _trigger_review_revision(
    ticket_id: str,
    pr_title: str,
    repo: str,
    pr_number: int,
    head_branch: str,
) -> None:
    """Submit a background task to handle the full review-revision cycle."""

    def _run() -> None:
        import time as _time
        _t_start = _time.time()
        logger.info("[%s] ================================================", ticket_id)
        logger.info("[%s] 🔄 PR REVIEW LOOP STARTED — PR #%d", ticket_id, pr_number)
        logger.info("[%s] ================================================", ticket_id)

        try:
            # 1. Collect all review comments
            review_comments = get_pr_review_comments(repo, pr_number)
            if not review_comments:
                logger.info("[%s] No review comments found — skipping revision.", ticket_id)
                return

            logger.info("[%s] 📋 Found %d review comment(s).", ticket_id, len(review_comments))

            # 2. Get list of files changed in the PR
            pr_files = get_pr_files(repo, pr_number)
            if not pr_files:
                logger.warning("[%s] No files found in PR — cannot revise.", ticket_id)
                post_pr_comment(
                    repo, pr_number,
                    "⚠️ AEA could not find any files in this PR to revise."
                )
                return

            logger.info("[%s] 📁 PR contains %d file(s): %s", ticket_id, len(pr_files), pr_files)

            # 3. Post a "working on it" comment so the reviewer knows AEA is responding
            post_pr_comment(
                repo, pr_number,
                f"🤖 **AEA is addressing the review feedback** — revising {len(pr_files)} file(s)...",
            )

            # 4. Generate revised code
            result = generate_revision_from_review(
                ticket_id=ticket_id,
                pr_title=pr_title,
                repo=repo,
                branch=head_branch,
                pr_files=pr_files,
                review_comments=review_comments,
            )
            changes = result.get("changes", [])
            explanation = result.get("explanation", "")

            if not changes:
                post_pr_comment(
                    repo, pr_number,
                    f"🤖 **AEA reviewed the feedback** but determined no code changes are needed.\n\n"
                    f"*Reason:* {explanation}",
                )
                logger.info("[%s] No changes generated from review.", ticket_id)
                return

            # 5. Commit revised files to the same branch (PR auto-updates)
            commit_revision(ticket_id, repo, head_branch, changes)

            # 6. Post summary comment on PR
            file_list = "\n".join(f"- `{c['file_path']}`" for c in changes)
            post_pr_comment(
                repo, pr_number,
                f"✅ **AEA has addressed the review feedback** — {len(changes)} file(s) updated.\n\n"
                f"**Files changed:**\n{file_list}\n\n"
                f"**Summary of changes:**\n{explanation}\n\n"
                f"---\n*Please re-review the updated code above.*",
            )

            elapsed = _time.time() - _t_start
            logger.info("[%s] ================================================", ticket_id)
            logger.info("[%s] ⭐ REVIEW LOOP COMPLETE in %.1fs", ticket_id, elapsed)
            logger.info("[%s] ================================================", ticket_id)

        except Exception as exc:  # noqa: BLE001
            logger.error("[%s] 💥 Review revision failed: %s", ticket_id, exc)
            try:
                post_pr_comment(
                    repo, pr_number,
                    f"⚠️ **AEA encountered an error** while processing the review:\n\n`{exc}`",
                )
            except Exception:
                pass

    _review_executor.submit(_run)


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/github", status_code=200)
async def github_webhook(
    request: Request,
    x_hub_signature_256: Optional[str] = Header(None, alias="X-Hub-Signature-256"),
):
    """
    Receive GitHub webhook events for the PR review loop.

    Supported events
    ----------------
    - ``pull_request_review`` (action: submitted) — reviewer submits a review
    - ``pull_request_review_comment`` (action: created) — reviewer adds a line comment
    """
    body: bytes = await request.body()

    # Signature verification
    if GITHUB_WEBHOOK_SECRET and not _verify_signature(
        body, GITHUB_WEBHOOK_SECRET, x_hub_signature_256
    ):
        raise HTTPException(status_code=401, detail="Invalid GitHub webhook signature.")

    try:
        payload: dict = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=422, detail="Request body is not valid JSON.")

    event = request.headers.get("X-GitHub-Event", "")
    action = payload.get("action", "")

    # -----------------------------------------------------------------------
    # pull_request_review — reviewer submits a full review
    # -----------------------------------------------------------------------
    if event == "pull_request_review" and action == "submitted":
        review = payload.get("review", {})
        pr = payload.get("pull_request", {})
        repo = payload.get("repository", {}).get("full_name", "")
        pr_number = pr.get("number")
        pr_title = pr.get("title", "")
        head_branch = pr.get("head", {}).get("ref", "")
        review_state = review.get("state", "").lower()

        # Only act on reviews that request changes or leave comments
        # Skip "approved" reviews with no body and pure "dismissed" reviews
        review_body = (review.get("body") or "").strip()
        if review_state == "dismissed":
            return {"detail": "Dismissed review — no action."}
        if review_state == "approved" and not review_body:
            return {"detail": "Approval with no comments — no action."}

        ticket_id = _extract_ticket_id(pr_title, head_branch)
        logger.info(
            "[%s] 📬 PR #%d review received (state: %s) — triggering revision.",
            ticket_id, pr_number, review_state,
        )
        _trigger_review_revision(ticket_id, pr_title, repo, pr_number, head_branch)
        return {"detail": f"Review revision triggered for PR #{pr_number}."}

    # -----------------------------------------------------------------------
    # pull_request_review_comment — individual line comment added
    # -----------------------------------------------------------------------
    if event == "pull_request_review_comment" and action == "created":
        pr = payload.get("pull_request", {})
        repo = payload.get("repository", {}).get("full_name", "")
        pr_number = pr.get("number")
        pr_title = pr.get("title", "")
        head_branch = pr.get("head", {}).get("ref", "")

        ticket_id = _extract_ticket_id(pr_title, head_branch)
        logger.info(
            "[%s] 📬 PR #%d line comment received — triggering revision.",
            ticket_id, pr_number,
        )
        _trigger_review_revision(ticket_id, pr_title, repo, pr_number, head_branch)
        return {"detail": f"Review revision triggered for PR #{pr_number}."}

    return {"detail": f"Event '{event}' / action '{action}' acknowledged but not processed."}
