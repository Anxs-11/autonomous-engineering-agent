"""
Week 5 — Slack Notifier

Sends a Slack message via an Incoming Webhook URL when AEA creates a PR.
If SLACK_WEBHOOK_URL is not set, all calls are silently skipped.
"""

import logging
import os

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

SLACK_WEBHOOK_URL: str = os.getenv("SLACK_WEBHOOK_URL", "")


def notify_pr_created(
    ticket_id: str,
    title: str,
    pr_url: str,
    repo: str,
    num_files: int,
) -> None:
    """
    Post a Slack message announcing a new AEA-generated PR.

    Does nothing if SLACK_WEBHOOK_URL is not configured.
    """
    if not SLACK_WEBHOOK_URL:
        logger.debug("SLACK_WEBHOOK_URL not set — Slack notification skipped.")
        return

    message = {
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": ":robot_face: AEA Created a Pull Request",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Ticket:*\n{ticket_id}"},
                    {"type": "mrkdwn", "text": f"*Repo:*\n{repo}"},
                    {"type": "mrkdwn", "text": f"*Title:*\n{title}"},
                    {"type": "mrkdwn", "text": f"*Files changed:*\n{num_files}"},
                ],
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "View Pull Request", "emoji": True},
                        "url": pr_url,
                        "style": "primary",
                    }
                ],
            },
        ]
    }

    try:
        resp = httpx.post(SLACK_WEBHOOK_URL, json=message, timeout=10)
        resp.raise_for_status()
        logger.info("Slack notification sent for %s PR: %s", ticket_id, pr_url)
    except Exception as exc:  # noqa: BLE001
        # Never let a Slack failure break the main flow
        logger.error("Slack notification failed for %s: %s", ticket_id, exc)


def notify_pr_failed(ticket_id: str, title: str, error: str) -> None:
    """
    Post a Slack message when AEA fails to create a PR.

    Does nothing if SLACK_WEBHOOK_URL is not configured.
    """
    if not SLACK_WEBHOOK_URL:
        return

    message = {
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": ":warning: AEA PR Creation Failed",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Ticket:*\n{ticket_id}"},
                    {"type": "mrkdwn", "text": f"*Title:*\n{title}"},
                ],
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Error:*\n```{error}```"},
            },
        ]
    }

    try:
        resp = httpx.post(SLACK_WEBHOOK_URL, json=message, timeout=10)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        logger.error("Slack failure notification failed for %s: %s", ticket_id, exc)
