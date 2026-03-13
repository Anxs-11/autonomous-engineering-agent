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
    """Open a PR and return its HTML URL. If one already exists, return its URL."""
    url = f"{GITHUB_API}/repos/{repo}/pulls"
    payload = {
        "title": title,
        "body": body,
        "head": head_branch,
        "base": base_branch,
    }
    resp = client.post(url, headers=_HEADERS, json=payload)

    if resp.status_code == 422 and "pull request already exists" in resp.text.lower():
        # PR already open for this branch — fetch and return its URL
        logger.warning("PR already exists for %s, fetching existing URL.", head_branch)
        list_resp = client.get(
            url,
            headers=_HEADERS,
            params={"head": f"{repo.split('/')[0]}:{head_branch}", "state": "open"},
        )
        list_resp.raise_for_status()
        prs = list_resp.json()
        if prs:
            return prs[0]["html_url"]

    resp.raise_for_status()
    return resp.json()["html_url"]


def create_pull_request(
    ticket_id: str,
    title: str,
    changes: list[dict],
    explanation: str,
    repo: str,
    base_branch: str = "dev",
    testing_checklist: list[str] | None = None,
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

    _t_pr_start = time.time()
    logger.info("[%s] ================================================", ticket_id)
    logger.info("[%s] 🚀 GitHub PR creation starting", ticket_id)
    logger.info("[%s]    Repo   : %s", ticket_id, repo)
    logger.info("[%s]    Branch : %s -> %s", ticket_id, new_branch, base_branch)
    logger.info("[%s]    Files  : %d", ticket_id, len(changes))
    logger.info("[%s] ================================================", ticket_id)

    try:
        with httpx.Client(timeout=30) as client:
            # 1. Get base SHA
            logger.info("[%s] 🔗 Step 1/3: Fetching base branch SHA (%s)...", ticket_id, base_branch)
            base_sha = _get_base_sha(client, repo, base_branch)
            logger.info("[%s] ✅ Step 1/3 done: SHA=%s", ticket_id, base_sha[:7])

            # 2. Create feature branch
            logger.info("[%s] 🌿 Step 2/3: Creating branch %s...", ticket_id, new_branch)
            _create_branch(client, repo, new_branch, base_sha)
            logger.info("[%s] ✅ Step 2/3 done: branch ready", ticket_id)

            # 3. Commit each file
            logger.info("[%s] 📤 Step 3/3: Committing %d file(s)...", ticket_id, len(changes))
            for i, change in enumerate(changes, 1):
                path = change["file_path"]
                content = change["new_content"]
                existing_sha = _get_file_sha(client, repo, path, new_branch)
                commit_msg = f"[{ticket_id}] Update {path}"
                logger.info("[%s]    [%d/%d] Committing %s...", ticket_id, i, len(changes), path)
                _commit_file(client, repo, path, content, new_branch, commit_msg, existing_sha)
                # Small delay to avoid secondary rate limits
                time.sleep(0.3)
            logger.info("[%s] ✅ Step 3/3 done: all files committed", ticket_id)

            # 4. Open PR
            checklist_section = ""
            if testing_checklist:
                items = "\n".join(f"- [ ] {item}" for item in testing_checklist)
                checklist_section = f"\n\n## Testing Checklist\n{items}"

            pr_body = (
                f"**Jira Ticket:** {ticket_id}\n\n"
                f"**Changes made by AEA (Autonomous Engineering Agent):**\n\n"
                f"{explanation}"
                f"{checklist_section}\n\n"
                f"---\n*This PR was generated automatically. Please review carefully before merging.*"
            )
            logger.info("[%s] 📝 Opening Pull Request...", ticket_id)
            pr_url = _open_pull_request(
                client, repo, new_branch, base_branch, pr_title, pr_body
            )
            elapsed = time.time() - _t_pr_start
            logger.info("[%s] ================================================", ticket_id)
            logger.info("[%s] 🎉 PR CREATED in %.1fs -> %s", ticket_id, elapsed, pr_url)
            logger.info("[%s] ================================================", ticket_id)
            return pr_url

    except httpx.HTTPStatusError as exc:
        msg = f"GitHub API error {exc.response.status_code}: {exc.response.text}"
        logger.error(msg)
        raise RuntimeError(msg) from exc


# ---------------------------------------------------------------------------
# PR Review helpers (Week 6)
# ---------------------------------------------------------------------------

def get_pr_review_comments(repo: str, pr_number: int) -> list[dict]:
    """
    Return all human feedback on a PR as a list of dicts:
      {"type": "review"|"line", "body": str, "file": str|None, "line": int|None}

    Combines:
    - Review body text  (GET /pulls/{n}/reviews)
    - Line-level comments (GET /pulls/{n}/comments)
    """
    comments: list[dict] = []
    with httpx.Client(timeout=30) as client:
        # Review-level comments
        reviews_resp = client.get(
            f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/reviews",
            headers=_HEADERS,
        )
        reviews_resp.raise_for_status()
        for review in reviews_resp.json():
            body = (review.get("body") or "").strip()
            if body:
                comments.append({"type": "review", "body": body, "file": None, "line": None})

        # Line-level review comments
        line_resp = client.get(
            f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/comments",
            headers=_HEADERS,
        )
        line_resp.raise_for_status()
        for comment in line_resp.json():
            body = (comment.get("body") or "").strip()
            path = comment.get("path", "")
            line = comment.get("line") or comment.get("original_line")
            if body:
                comments.append({"type": "line", "body": body, "file": path, "line": line})

    return comments


def get_pr_files(repo: str, pr_number: int) -> list[str]:
    """Return the list of file paths changed in a PR."""
    with httpx.Client(timeout=30) as client:
        resp = client.get(
            f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/files",
            headers=_HEADERS,
        )
        resp.raise_for_status()
        return [f["filename"] for f in resp.json()]


def post_pr_comment(repo: str, pr_number: int, body: str) -> None:
    """Post a general (issue-level) comment on a Pull Request."""
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            f"{GITHUB_API}/repos/{repo}/issues/{pr_number}/comments",
            headers=_HEADERS,
            json={"body": body},
        )
        resp.raise_for_status()
        logger.info("Posted comment on PR #%d in %s", pr_number, repo)


def commit_revision(
    ticket_id: str,
    repo: str,
    branch: str,
    changes: list[dict],
) -> None:
    """
    Commit updated files from a review revision onto the existing feature branch.
    The open PR updates automatically.
    """
    if not changes:
        return
    with httpx.Client(timeout=30) as client:
        logger.info("[%s] 📤 Committing %d revised file(s) to branch %s...", ticket_id, len(changes), branch)
        for i, change in enumerate(changes, 1):
            path = change["file_path"]
            content = change["new_content"]
            existing_sha = _get_file_sha(client, repo, path, branch)
            commit_msg = f"[{ticket_id}] Review revision: update {path}"
            logger.info("[%s]    [%d/%d] Committing %s...", ticket_id, i, len(changes), path)
            _commit_file(client, repo, path, content, branch, commit_msg, existing_sha)
            time.sleep(0.3)
        logger.info("[%s] ✅ Revision committed — PR updated automatically.", ticket_id)

