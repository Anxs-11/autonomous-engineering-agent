import json
import logging
import os
import re

import anthropic
from dotenv import load_dotenv

from app.models.ticket import ClassificationLabel, ClassificationResult

load_dotenv()

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(
    api_key=os.getenv("ANTHROPIC_API_KEY"),
    base_url=os.getenv("ANTHROPIC_BASE_URL"),
    default_headers={
        "anthropic-version": "2023-06-01",
    },
)

SYSTEM_PROMPT = """\
You are an engineering ticket classifier for an Autonomous Engineering Agent (AEA).

Classify each Jira ticket into EXACTLY ONE of the following labels:

- AUTOMATABLE  : Clear, specific, small-scope task. Examples: bug fix with a clear
                 reproduction path, adding a single REST endpoint, config or env-var
                 change, small UI/copy tweak. The agent can implement this autonomously.

- CLARIFICATION: Vague requirements, missing acceptance criteria, ambiguous scope, or
                 insufficient detail to begin implementation. A human must clarify before
                 work can start.

- COMPLEX      : Large feature requiring architecture discussion, multiple services or
                 teams affected, significant design decisions, database schema design, or
                 performance/security considerations. Too large for autonomous handling.

- NONCODE      : Documentation updates, design specs, HR tasks, process/policy tickets,
                 meeting notes, knowledge-base articles, or anything that does not require
                 writing or changing code.

Respond ONLY with valid JSON — no markdown fences, no extra text:
{
  "label": "AUTOMATABLE" | "CLARIFICATION" | "COMPLEX" | "NONCODE",
  "reason": "<one concise sentence explaining the classification>"
}
"""


def classify_ticket(title: str, description: str) -> ClassificationResult:
    """
    Send a Jira ticket to OpenAI GPT-4o and return a structured classification.

    Args:
        title:       The Jira issue summary/title.
        description: The Jira issue description (may be empty).

    Returns:
        ClassificationResult with a label and a brief reason.

    Raises:
        ValueError: If the model returns an unexpected label.
        openai.OpenAIError: On API failures.
    """
    user_content = (
        f"Ticket Title: {title}\n\n"
        f"Ticket Description:\n{description or 'No description provided.'}\n\n"
        "Classify this ticket."
    )

    response = client.messages.create(
        model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5"),
        max_tokens=512,
        temperature=0.1,
        system=SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": user_content},
        ],
    )

    raw: str = response.content[0].text.strip()
    logger.info("Claude raw response: %s", raw)

    # Strip markdown fences if Claude wrapped the JSON (e.g. ```json ... ```)
    fenced = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", raw)
    if fenced:
        raw = fenced.group(1).strip()

    # Extract the first {...} block in case there is any surrounding text
    brace_match = re.search(r"\{[\s\S]+\}", raw)
    if brace_match:
        raw = brace_match.group(0)

    data: dict = json.loads(raw)

    label = ClassificationLabel(data["label"])
    reason: str = data["reason"]

    return ClassificationResult(label=label, reason=reason)
