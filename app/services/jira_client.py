import logging
import os
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

JIRA_BASE_URL: str = os.getenv("JIRA_BASE_URL", "").rstrip("/")
JIRA_EMAIL: str = os.getenv("JIRA_EMAIL", "")
JIRA_API_TOKEN: str = os.getenv("JIRA_API_TOKEN", "")
JIRA_BOT_ACCOUNT_ID: str = os.getenv("JIRA_BOT_ACCOUNT_ID", "")

_AUTH = (JIRA_EMAIL, JIRA_API_TOKEN)


def _is_configured() -> bool:
    return bool(JIRA_BASE_URL and JIRA_EMAIL and JIRA_API_TOKEN)


def _extract_text_from_adf(node: dict) -> str:
    """Recursively extract plain text from an Atlassian Document Format node."""
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return node.get("text", "")
    return " ".join(
        _extract_text_from_adf(child)
        for child in node.get("content", [])
        if _extract_text_from_adf(child)
    ).strip()


def post_comment(issue_key: str, text: str) -> None:
    """
    Post a plain-text comment to a Jira issue using the Atlassian Document Format.

    Silently logs and returns if Jira credentials are not configured.
    """
    if not _is_configured():
        logger.warning("Jira credentials not set — skipping comment on %s.", issue_key)
        return

    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}/comment"
    payload = {
        "body": {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": text}],
                }
            ],
        }
    }

    try:
        response = httpx.post(url, json=payload, auth=_AUTH, timeout=15)
        response.raise_for_status()
        logger.info("Posted clarification comment to %s.", issue_key)
    except httpx.HTTPError as exc:
        logger.error("Failed to post comment to %s: %s", issue_key, exc)


def get_human_comments(issue_key: str) -> list[str]:
    """
    Fetch all comments on a Jira issue, excluding comments posted by the bot account.

    Returns a list of plain-text comment strings, oldest first.
    """
    if not _is_configured():
        return []

    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}/comment"
    try:
        response = httpx.get(url, auth=_AUTH, timeout=15)
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPError as exc:
        logger.error("Failed to fetch comments for %s: %s", issue_key, exc)
        return []

    texts: list[str] = []
    for comment in data.get("comments", []):
        # Skip the bot's own comments to avoid infinite loops
        if JIRA_BOT_ACCOUNT_ID:
            author_id: str = comment.get("author", {}).get("accountId", "")
            if author_id == JIRA_BOT_ACCOUNT_ID:
                continue

        body = comment.get("body", {})
        text: str = (
            _extract_text_from_adf(body) if isinstance(body, dict) else str(body)
        )
        if text.strip():
            texts.append(text.strip())

    return texts


def get_bot_account_id() -> Optional[str]:
    """Look up the account ID for the configured JIRA_EMAIL (used to identify bot comments)."""
    if not _is_configured():
        return None
    url = f"{JIRA_BASE_URL}/rest/api/3/myself"
    try:
        response = httpx.get(url, auth=_AUTH, timeout=10)
        response.raise_for_status()
        return response.json().get("accountId")
    except httpx.HTTPError as exc:
        logger.error("Failed to fetch bot account ID: %s", exc)
        return None
