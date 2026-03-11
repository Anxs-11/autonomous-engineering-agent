import logging
import os

import anthropic
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(
    api_key=os.getenv("ANTHROPIC_API_KEY"),
    base_url=os.getenv("ANTHROPIC_BASE_URL"),
    default_headers={"anthropic-version": "2023-06-01"},
)

SYSTEM_PROMPT = """\
You are an AI engineering agent reviewing a Jira ticket that is too vague to implement.

Your job is to ask the developer 2-3 specific, targeted questions that will give enough
clarity to implement the ticket autonomously.

Questions should specifically address whichever of these are missing:
1. Exact expected behavior or output (what should happen, what should the result look like)
2. Which services, endpoints, components, or files are affected
3. Acceptance criteria or edge cases to handle

FORMAT — respond with exactly this structure:
Line 1: "Hi! I've reviewed this ticket and need a few clarifications before I can start:"
Line 2: blank
Lines 3+: numbered questions (1., 2., 3.)
Last line: blank, then "Once you've answered, I'll pick this up right away."

Keep the entire message under 150 words. Be direct and specific — no filler phrases.
"""


def generate_clarifying_questions(
    title: str,
    description: str,
    prior_comments: list[str] | None = None,
) -> str:
    """
    Use Claude to generate specific clarifying questions for a vague Jira ticket.

    Args:
        title:          Jira issue summary.
        description:    Jira issue description (may be empty).
        prior_comments: Previous human comments on the ticket (for follow-up rounds).

    Returns:
        A formatted string ready to be posted as a Jira comment.
    """
    context = (
        f"Ticket Title: {title}\n\n"
        f"Description:\n{description or 'No description provided.'}"
    )
    if prior_comments:
        context += "\n\nDeveloper's previous replies:\n" + "\n".join(
            f"- {c}" for c in prior_comments
        )
        context += (
            "\n\nThe above replies still don't provide enough clarity. "
            "Ask more specific follow-up questions."
        )

    response = client.messages.create(
        model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5"),
        max_tokens=400,
        temperature=0.2,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": context}],
    )

    questions: str = response.content[0].text.strip()
    logger.info("Generated clarifying questions for ticket: %s", title[:60])
    return questions
