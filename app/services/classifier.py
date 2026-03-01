import json
import os

from dotenv import load_dotenv
from openai import OpenAI

from app.models.ticket import ClassificationLabel, ClassificationResult

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

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

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        temperature=0.1,
        max_tokens=150,
        response_format={"type": "json_object"},
    )

    raw: str = response.choices[0].message.content.strip()
    data: dict = json.loads(raw)

    label = ClassificationLabel(data["label"])
    reason: str = data["reason"]

    return ClassificationResult(label=label, reason=reason)
