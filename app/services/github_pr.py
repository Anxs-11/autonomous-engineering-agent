"""
Week 4 — GitHub PR Service

Creates a feature branch, commits generated file changes, and opens a Pull
Request — all via the GitHub REST API (no local git clone needed).

Flow
----
1. GET  /repos/{repo}/git/ref/heads/{base}       → get SHA of base branch tip
2. POST /repos/{repo}/git/refs                    → create new branch
3. For each changed file:
   GET  /repos/{repo}/contents/{path}             → get current file SHA (if exists)
   PUT  /repos/{repo}/contents/{path}             → create/update file
4. POST /repos/{repo}/pulls                       → open Pull Request
"""

import base64
import logging
import os
import re
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")
GITHUB_API = "https://api.github.com"

_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def _slugify(text: str, max_len: int = 40) -> str:
    """Convert ticket title to a branch-name-safe slug."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].strip("-")


def _get_base_sha(client: httpx.Client, repo: str, branch: str) -> str:
    """Return the commit SHA at the tip of *branch*."""
    url = f"{GITHUB_API}/repos/{repo}/git/ref/heads/{branch}"
    resp = client.get(url, headers=_HEADERS)
    resp.raise_for_status()
    return resp.json()["object"]["sha"]


def _create_branch(client: httpx.Client, repo: str, new_branch: str, sha: str) -> None:
    """Create a new branch pointing at *sha*."""
    url = f"{GITHUB_API}/repos/{repo}/git/refs"
    payload = {"ref": f"refs/heads/{new_branch}", "sha": sha}
    resp = client.post(url, headers=_HEADERS, json=payload)
    if resp.status_code == 422:
        # Branch already exists — that's acceptable (re-run scenario)
        logger.warning("Branch %s already exists, continuing.", new_branch)
        return
    resp.raise_for_status()


def _get_file_sha(client: httpx.Client, repo: str, path: str, branch: str) -> str | None:
    """Return the blob SHA of *path* on *branch*, or None if the file doesn't exist yet."""
    url = f"{GITHUB_API}/repos/{repo}/contents/{path}"
    resp = client.get(url, headers=_HEADERS, params={"ref": branch})
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json().get("sha")


def _commit_file(
    client: httpx.Client,
    repo: str,
    path: str,
    content: str,
    branch: str,
    commit_message: str,
    existing_sha: str | None,
) -> None:
    """Create or update a single file on *branch*."""
    url = f"{GITHUB_API}/repos/{repo}/contents/{path}"
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    payload: dict = {
        "message": commit_message,
        "content": encoded,
        "branch": branch,
    }
    if existing_sha:
        payload["sha"] = existing_sha  # required for updates

    resp = client.put(url, headers=_HEADERS, json=payload)
    resp.raise_for_status()
    logger.info("Committed %s to branch %s", path, branch)


def _open_pull_request(
    client: httpx.Client,
    repo: str,
    head_branch: str,
    base_branch: str,
    title: str,
    body: str,
) -> str:
    """Open a PR and return its HTML URL."""
    url = f"{GITHUB_API}/repos/{repo}/pulls"
    payload = {
        "title": title,
        "body": body,
        "head": head_branch,
        "base": base_branch,
    }
    resp = client.post(url, headers=_HEADERS, json=payload)
    resp.raise_for_status()
    return resp.json()["html_url"]


def create_pull_request(
    ticket_id: str,
    title: str,
    changes: list[dict],
    explanation: str,
    repo: str,
    base_branch: str = "dev",
) -> str:
    """
    End-to-end: create branch, commit all file changes, open PR.

    Parameters
    ----------
    ticket_id   : Jira ticket key, e.g. "AEA-42"
    title       : Ticket title (used for branch name + PR title)
    changes     : List of {"file_path": str, "new_content": str}
    explanation : Claude's explanation of the changes (PR body)
    repo        : GitHub repo slug, e.g. "Anxs-11/aea-test-sandbox"
    base_branch : Branch to merge into (default "dev")

    Returns the HTML URL of the newly created PR.
    Raises RuntimeError on any GitHub API failure.
    """
    if not GITHUB_TOKEN:
        raise RuntimeError("GITHUB_TOKEN is not set — cannot create PR.")

    if not changes:
        raise RuntimeError("No file changes provided — nothing to commit.")

    # Feature branch: {ticket_id}-dev  (e.g. aea-123-dev)
    # PR targets: base_branch          (e.g. dev)
    new_branch = f"{ticket_id.lower()}-dev"
    pr_title = f"[{ticket_id}] {title}"

    try:
        with httpx.Client(timeout=30) as client:
            # 1. Get base SHA
            base_sha = _get_base_sha(client, repo, base_branch)
            logger.info("Base branch %s SHA: %s", base_branch, base_sha)

            # 2. Create feature branch
            _create_branch(client, repo, new_branch, base_sha)
            logger.info("Created branch: %s", new_branch)

            # 3. Commit each file
            for change in changes:
                path = change["file_path"]
                content = change["new_content"]
                existing_sha = _get_file_sha(client, repo, path, new_branch)
                commit_msg = f"[{ticket_id}] Update {path}"
                _commit_file(client, repo, path, content, new_branch, commit_msg, existing_sha)
                # Small delay to avoid secondary rate limits
                time.sleep(0.3)

            # 4. Open PR
            pr_body = (
                f"**Jira Ticket:** {ticket_id}\n\n"
                f"**Changes made by AEA (Autonomous Engineering Agent):**\n\n"
                f"{explanation}\n\n"
                f"---\n*This PR was generated automatically. Please review carefully before merging.*"
            )
            pr_url = _open_pull_request(
                client, repo, new_branch, base_branch, pr_title, pr_body
            )
            logger.info("PR created: %s", pr_url)
            return pr_url

    except httpx.HTTPStatusError as exc:
        msg = f"GitHub API error {exc.response.status_code}: {exc.response.text}"
        logger.error(msg)
        raise RuntimeError(msg) from exc
