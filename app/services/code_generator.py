"""
Week 4 — Code Generation Service (Two-Pass File Selection)

Instead of chunk-based RAG, uses a smarter two-pass approach:

Pass 1 — File Selection
    Claude receives the full file tree (paths only) + ticket description.
    It returns the exact list of files it needs to read to solve the ticket.
    This ensures no relevant file is missed.

Pass 2 — Code Generation
    Claude receives the full content of every file it selected + ticket.
    It produces the exact file changes needed.

Output schema:
    {
        "changes": [
            {"file_path": "app/main.py", "new_content": "...full file..."},
            ...
        ],
        "explanation": "What was changed and why"
    }
"""

import json
import logging
import os
import re

import anthropic
from dotenv import load_dotenv

from app.services.github_fetcher import get_file_content, list_code_files

load_dotenv()

logger = logging.getLogger(__name__)

_client = anthropic.Anthropic(
    api_key=os.getenv("ANTHROPIC_API_KEY", ""),
    base_url=os.getenv("ANTHROPIC_BASE_URL"),
    default_headers={"anthropic-version": "2023-06-01"},
)

_MODEL = "claude-sonnet-4-5"
_MAX_FILES_TO_FETCH = 15   # cap to avoid context overflow


_PASS1_SYSTEM = """You are an expert software engineer doing codebase analysis.
You will be given a Jira ticket and a list of file paths in a repository.

Your task: identify which files you need to read to implement the ticket.

Rules:
- Return ONLY valid JSON — no prose, no markdown fences.
- Schema:
  {"files": ["path/to/file1.py", "path/to/file2.py"]}
- List only file paths that exist in the provided tree.
- Be thorough — include every file that would need to be read or changed.
- Maximum 15 files.
- If the ticket cannot be implemented from the available files, return {"files": []}.
"""

_PASS2_SYSTEM = """You are an expert software engineer.
You will be given a Jira ticket and the full content of relevant source files.

Your task: produce the exact file changes needed to fulfil the ticket.

Rules:
- Return ONLY valid JSON — no prose, no markdown fences.
- Schema:
  {
    "changes": [
      {
        "file_path": "<repo-relative path>",
        "new_content": "<complete new file content as a string>"
      }
    ],
    "explanation": "<one paragraph: what changed and why>"
  }
- Always output the FULL file content — never partial diffs.
- Only include files that actually need to change.
- Preserve existing code style, imports, and patterns.
- If the ticket is ambiguous or cannot be safely implemented, return:
  {"changes": [], "explanation": "<reason>"}
"""


def _strip_json(text: str) -> str:
    """Strip markdown fences and extract the first JSON object."""
    match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    if match:
        return match.group(1).strip()
    match = re.search(r"(\{[\s\S]+\})", text)
    if match:
        return match.group(1).strip()
    return text.strip()


def _pass1_select_files(
    ticket_id: str, title: str, description: str, file_tree: list[str]
) -> list[str]:
    """
    Pass 1: Ask Claude which files it needs to read to solve this ticket.
    Returns a filtered list of valid file paths.
    """
    tree_text = "\n".join(file_tree)
    user_content = f"""Ticket ID: {ticket_id}
Title: {title}

Description:
{description}

Repository file tree:
{tree_text}

Which files do you need to read to implement this ticket? Return JSON."""

    try:
        response = _client.messages.create(
            model=_MODEL,
            max_tokens=1024,
            temperature=0,
            system=_PASS1_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
        )
    except Exception as exc:
        logger.error("Pass 1 API error: %s", exc)
        return []

    raw = response.content[0].text if response.content else ""
    try:
        data = json.loads(_strip_json(raw))
        selected = data.get("files", [])
    except json.JSONDecodeError:
        logger.error("Pass 1 JSON parse failed. Raw: %s", raw)
        return []

    # Validate: only keep paths that actually exist in the tree
    tree_set = set(file_tree)
    valid = [f for f in selected if f in tree_set]
    logger.info(
        "Pass 1: Claude selected %d file(s) from %d in tree: %s",
        len(valid), len(file_tree), valid,
    )
    return valid[:_MAX_FILES_TO_FETCH]


def _pass2_generate_code(
    ticket_id: str,
    title: str,
    description: str,
    file_contents: dict[str, str],
) -> dict:
    """
    Pass 2: Claude reads full file contents and generates code changes.
    Returns {"changes": [...], "explanation": str}.
    """
    files_block = ""
    for path, content in file_contents.items():
        files_block += f"\n\n=== {path} ===\n{content}"

    user_content = f"""Ticket ID: {ticket_id}
Title: {title}

Description:
{description}

Relevant source files:{files_block}

Generate the required file changes as JSON per the schema in your instructions."""

    try:
        response = _client.messages.create(
            model=_MODEL,
            max_tokens=8192,
            temperature=0.1,
            system=_PASS2_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
        )
    except Exception as exc:
        logger.error("Pass 2 API error: %s", exc)
        return {"changes": [], "explanation": f"LLM call failed: {exc}"}

    raw = response.content[0].text if response.content else ""
    logger.debug("Pass 2 raw response:\n%s", raw)

    try:
        result = json.loads(_strip_json(raw))
    except json.JSONDecodeError as exc:
        logger.error("Pass 2 JSON parse failed: %s\nRaw: %s", exc, raw)
        return {"changes": [], "explanation": f"JSON parse error: {exc}"}

    # Validate changes
    validated = [
        {"file_path": str(c["file_path"]).strip(), "new_content": str(c["new_content"])}
        for c in result.get("changes", [])
        if isinstance(c, dict) and "file_path" in c and "new_content" in c
    ]

    explanation = result.get("explanation", "")
    logger.info(
        "Pass 2: %d file(s) to change. %s",
        len(validated), explanation[:120] if explanation else "",
    )
    return {"changes": validated, "explanation": explanation}


def generate_code_changes(
    ticket_id: str,
    title: str,
    description: str,
    repo: str,
    branch: str = "main",
) -> dict:
    """
    Full two-pass code generation:
      1. Fetch file tree from GitHub
      2. Claude selects which files it needs
      3. Fetch full content of those files
      4. Claude generates code changes

    Returns {"changes": [...], "explanation": str}.
    """
    logger.info("Starting two-pass code generation for %s on %s@%s", ticket_id, repo, branch)

    # --- Fetch file tree ---
    path_filter = os.getenv("GITHUB_REPO_PATH_FILTER", "")
    try:
        file_tree = list_code_files(repo, branch, path_filter)
    except Exception as exc:
        logger.error("Failed to fetch file tree for %s: %s", repo, exc)
        return {"changes": [], "explanation": f"Could not fetch repo file tree: {exc}"}

    if not file_tree:
        return {"changes": [], "explanation": "No indexable files found in the repository."}

    logger.info("File tree: %d files found in %s@%s", len(file_tree), repo, branch)

    # --- Pass 1: select files ---
    selected_files = _pass1_select_files(ticket_id, title, description, file_tree)
    if not selected_files:
        return {"changes": [], "explanation": "Claude could not identify relevant files for this ticket."}

    # --- Fetch full file contents ---
    file_contents: dict[str, str] = {}
    for path in selected_files:
        try:
            content = get_file_content(repo, path, branch)
            if content:
                file_contents[path] = content
            else:
                logger.warning("Empty content for %s — skipping.", path)
        except Exception as exc:
            logger.warning("Could not fetch %s: %s", path, exc)

    if not file_contents:
        return {"changes": [], "explanation": "Could not fetch content of any selected files."}

    logger.info("Fetched %d file(s) for Pass 2.", len(file_contents))

    # --- Pass 2: generate code ---
    return _pass2_generate_code(ticket_id, title, description, file_contents)


import json
import logging
import os
import re

import anthropic
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_client = anthropic.Anthropic(
    api_key=os.getenv("ANTHROPIC_API_KEY", ""),
    base_url=os.getenv("ANTHROPIC_BASE_URL"),
    default_headers={"anthropic-version": "2023-06-01"},
)

_MODEL = "claude-sonnet-4-5"

_SYSTEM_PROMPT = """You are an expert software engineer.
You will be given a Jira ticket (title + description) and relevant sections of
the codebase retrieved via semantic search.

Your task: produce the exact file changes needed to fulfil the ticket.

Rules:
- Return ONLY valid JSON — no prose, no markdown fences.
- The JSON must match this schema exactly:
  {
    "changes": [
      {
        "file_path": "<repo-relative path, e.g. app/main.py>",
        "new_content": "<complete new content of the file as a string>"
      }
    ],
    "explanation": "<one paragraph explaining what you changed and why>"
  }
- Always output the FULL file content for every changed file — never partial diffs.
- Only include files that actually need to change.
- Preserve existing code style, imports, and patterns from the context.
- If the ticket is ambiguous or impossible to implement safely, return:
  {"changes": [], "explanation": "<reason why no changes were made>"}
"""


def _strip_json_fences(text: str) -> str:
    """Remove markdown code fences if Claude wraps the JSON in them."""
    match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    if match:
        return match.group(1).strip()
    # Try to extract the first {...} block
    match = re.search(r"(\{[\s\S]+\})", text)
    if match:
        return match.group(1).strip()
    return text.strip()



