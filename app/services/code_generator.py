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
import time

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

_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
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
    "explanation": "<one paragraph: what changed and why>",
    "testing_checklist": [
      "<specific test step 1>",
      "<specific test step 2>"
    ]
  }
- Always output the FULL file content — never partial diffs.
- Only include files that actually need to change.
- Preserve existing code style, imports, and patterns.
- The testing_checklist must contain 3-6 concrete, specific steps a reviewer can follow
  to manually verify the changes work correctly.
- If the ticket is ambiguous or cannot be safely implemented, return:
  {"changes": [], "explanation": "<reason>", "testing_checklist": []}
"""


_REVISION_SYSTEM = """You are an expert software engineer revising your code based on peer review feedback.
You previously implemented a feature on a Pull Request. A reviewer has now left comments.

Your task: address ALL review feedback and return updated file contents.

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
    "explanation": "<paragraph summarising what you changed to address the review>"
  }
- Always output the FULL file content for every modified file — never partial diffs.
- Only include files that actually changed in response to the review.
- Address every review comment — if a suggestion is ambiguous, make the most reasonable interpretation.
- Preserve existing code style, imports, and patterns.
- If the review is an approval with no actionable changes, return:
  {"changes": [], "explanation": "No changes needed — reviewer approved the implementation."}
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
        t0 = time.time()
        response = _client.messages.create(
            model=_MODEL,
            max_tokens=1024,
            temperature=0,
            system=_PASS1_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
        )
        logger.info("[%s] ⏱  Pass 1 Claude call completed in %.1fs", ticket_id, time.time() - t0)
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
        "[%s] ✅ Pass 1 done: selected %d/%d file(s): %s",
        ticket_id, len(valid), len(file_tree), valid,
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
        t0 = time.time()
        logger.info("[%s] ⏳ Pass 2: sending %d file(s) to Claude for code generation...", ticket_id, len(file_contents))
        response = _client.messages.create(
            model=_MODEL,
            max_tokens=16000,
            temperature=0.1,
            system=_PASS2_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
        )
        logger.info("[%s] ⏱  Pass 2 Claude call completed in %.1fs", ticket_id, time.time() - t0)
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
    testing_checklist = [
        str(item) for item in result.get("testing_checklist", [])
        if isinstance(item, str) and item.strip()
    ]
    logger.info(
        "[%s] ✅ Pass 2 done: %d file(s) to change. %s",
        ticket_id, len(validated), explanation[:120] if explanation else "",
    )
    return {"changes": validated, "explanation": explanation, "testing_checklist": testing_checklist}


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
    logger.info("[%s] ==================================================", ticket_id)
    logger.info("[%s] 🤖 AEA code generation started", ticket_id)
    logger.info("[%s]    Repo   : %s @ %s", ticket_id, repo, branch)
    logger.info("[%s]    Title  : %s", ticket_id, title)
    logger.info("[%s] ==================================================", ticket_id)
    _t_start = time.time()

    # --- Fetch file tree ---
    path_filter = os.getenv("GITHUB_REPO_PATH_FILTER", "")
    logger.info("[%s] 📂 Step 1/4: Fetching file tree from GitHub...", ticket_id)
    try:
        file_tree = list_code_files(repo, branch, path_filter)
    except Exception as exc:
        logger.error("Failed to fetch file tree for %s: %s", repo, exc)
        return {"changes": [], "explanation": f"Could not fetch repo file tree: {exc}"}

    if not file_tree:
        return {"changes": [], "explanation": "No indexable files found in the repository."}

    logger.info("[%s] ✅ Step 1/4 done: %d files in tree", ticket_id, len(file_tree))

    # --- Pass 1: select files ---
    logger.info("[%s] 🧠 Step 2/4: Pass 1 — Claude selecting relevant files...", ticket_id)
    selected_files = _pass1_select_files(ticket_id, title, description, file_tree)
    if not selected_files:
        return {"changes": [], "explanation": "Claude could not identify relevant files for this ticket."}

    # --- Fetch full file contents ---
    file_contents: dict[str, str] = {}
    logger.info("[%s] 📥 Step 3/4: Fetching %d file(s) from GitHub...", ticket_id, len(selected_files))
    for i, path in enumerate(selected_files, 1):
        try:
            content = get_file_content(repo, path, branch)
            if content:
                logger.info("[%s]    [%d/%d] Fetched: %s (%d chars)", ticket_id, i, len(selected_files), path, len(content))
                file_contents[path] = content
            else:
                logger.warning("Empty content for %s — skipping.", path)
        except Exception as exc:
            logger.warning("Could not fetch %s: %s", path, exc)

    if not file_contents:
        return {"changes": [], "explanation": "Could not fetch content of any selected files."}

    logger.info("[%s] ✅ Step 3/4 done: fetched %d file(s) for Pass 2", ticket_id, len(file_contents))

    # --- Pass 2: generate code ---
    logger.info("[%s] 🤖 Step 4/4: Pass 2 — Claude generating code changes...", ticket_id)
    result = _pass2_generate_code(ticket_id, title, description, file_contents)

    elapsed = time.time() - _t_start
    n_changes = len(result.get("changes", []))
    logger.info("[%s] ==================================================", ticket_id)
    logger.info("[%s] 🏁 AEA code generation COMPLETE in %.1fs", ticket_id, elapsed)
    logger.info("[%s]    Files changed : %d", ticket_id, n_changes)
    logger.info("[%s] ==================================================", ticket_id)
    return result


# ---------------------------------------------------------------------------
# Week 6 — Review Revision Pass
# ---------------------------------------------------------------------------

def generate_revision_from_review(
    ticket_id: str,
    pr_title: str,
    repo: str,
    branch: str,
    pr_files: list[str],
    review_comments: list[dict],
) -> dict:
    """
    Re-generate code changes in response to PR review feedback.

    Fetches the current content of every file changed in the PR from the
    feature branch, then asks Claude to produce updated versions that
    address all review comments.

    Returns {"changes": [...], "explanation": str}
    """
    _t_start = time.time()
    logger.info("[%s] ==================================================", ticket_id)
    logger.info("[%s] 🔍 Review revision starting", ticket_id)
    logger.info("[%s]    Branch : %s", ticket_id, branch)
    logger.info("[%s]    Files  : %d | Comments: %d", ticket_id, len(pr_files), len(review_comments))
    logger.info("[%s] ==================================================", ticket_id)

    # Fetch current file contents from the feature branch
    file_contents: dict[str, str] = {}
    for i, path in enumerate(pr_files, 1):
        try:
            content = get_file_content(repo, path, branch)
            if content:
                logger.info("[%s]    [%d/%d] Fetched: %s (%d chars)", ticket_id, i, len(pr_files), path, len(content))
                file_contents[path] = content
            else:
                logger.warning("[%s] Empty content for %s — skipping.", ticket_id, path)
        except Exception as exc:
            logger.warning("[%s] Could not fetch %s: %s", ticket_id, path, exc)

    if not file_contents:
        return {"changes": [], "explanation": "Could not fetch any PR files for revision."}

    # Build files block
    files_block = ""
    for path, content in file_contents.items():
        files_block += f"\n\n=== {path} ===\n{content}"

    # Build review comments block
    comments_block = ""
    for c in review_comments:
        if c["type"] == "line" and c.get("file"):
            loc = f"{c['file']} (line {c['line']})" if c.get("line") else c["file"]
            comments_block += f"\n• [File: {loc}] {c['body']}"
        else:
            comments_block += f"\n• [General] {c['body']}"

    user_content = f"""PR Title: {pr_title}

Review feedback from the reviewer:
{comments_block}

Current file contents on branch '{branch}':{files_block}

Please address all review comments and return the updated file(s) as JSON."""

    try:
        t0 = time.time()
        logger.info("[%s] 🤖 Sending revision request to Claude...", ticket_id)
        response = _client.messages.create(
            model=_MODEL,
            max_tokens=16000,
            temperature=0.1,
            system=_REVISION_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
        )
        logger.info("[%s] ⏱  Revision Claude call completed in %.1fs", ticket_id, time.time() - t0)
    except Exception as exc:
        logger.error("[%s] Revision API error: %s", ticket_id, exc)
        return {"changes": [], "explanation": f"LLM call failed: {exc}"}

    raw = response.content[0].text if response.content else ""
    try:
        result = json.loads(_strip_json(raw))
    except json.JSONDecodeError as exc:
        logger.error("[%s] Revision JSON parse failed: %s\nRaw: %s", ticket_id, exc, raw)
        return {"changes": [], "explanation": f"JSON parse error: {exc}"}

    validated = [
        {"file_path": str(c["file_path"]).strip(), "new_content": str(c["new_content"])}
        for c in result.get("changes", [])
        if isinstance(c, dict) and "file_path" in c and "new_content" in c
    ]

    elapsed = time.time() - _t_start
    logger.info("[%s] ==================================================", ticket_id)
    logger.info("[%s] 🏁 Review revision COMPLETE in %.1fs — %d file(s) updated", ticket_id, elapsed, len(validated))
    logger.info("[%s] ==================================================", ticket_id)
    return {"changes": validated, "explanation": result.get("explanation", "")}


