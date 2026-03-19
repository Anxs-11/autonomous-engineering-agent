import base64
import concurrent.futures
import logging
import os
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")

_HEADERS: dict = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    **({"Authorization": f"Bearer {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}),
}

# File extensions to index
CODE_EXTENSIONS: set[str] = {
    ".py", ".js", ".ts", ".java", ".go", ".rs",
    ".cpp", ".c", ".h", ".cs", ".rb", ".php", ".md",
}

# Max file size to index (bytes)
MAX_FILE_SIZE = 100_000


def list_code_files(
    repo: str,
    branch: str = "main",
    path_filter: str = "",
) -> list[str]:
    """
    Return all indexable code file paths in a GitHub repo using the Git Trees API.

    Args:
        repo:        GitHub repo in 'owner/repo' format.
        branch:      Branch name to index.
        path_filter: Optional path prefix — only files under this path are returned.

    Returns:
        List of file paths (relative to repo root).
    """
    url = f"https://api.github.com/repos/{repo}/git/trees/{branch}?recursive=1"
    try:
        response = httpx.get(url, headers=_HEADERS, timeout=30)
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPError as exc:
        logger.error("Failed to fetch file tree for %s@%s: %s", repo, branch, exc)
        return []

    paths: list[str] = []
    for item in data.get("tree", []):
        if item.get("type") != "blob":
            continue
        path: str = item["path"]
        if path_filter and not path.startswith(path_filter):
            continue
        if item.get("size", 0) > MAX_FILE_SIZE:
            continue
        if any(path.endswith(ext) for ext in CODE_EXTENSIONS):
            paths.append(path)

    logger.info("Found %d indexable files in %s@%s", len(paths), repo, branch)
    return paths


def get_file_content(repo: str, path: str, branch: str = "main") -> Optional[str]:
    """
    Fetch and decode the content of a single file from GitHub.

    Returns the file content as a string, or None if not found / not decodable.
    """
    url = f"https://api.github.com/repos/{repo}/contents/{path}?ref={branch}"
    try:
        response = httpx.get(url, headers=_HEADERS, timeout=15)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        content_b64: str = response.json().get("content", "")
        return base64.b64decode(content_b64).decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not fetch %s from %s: %s", path, repo, exc)
        return None


def get_file_summary(repo: str, path: str, branch: str = "main", max_lines: int = 10) -> str:
    """
    Fetch the first max_lines non-empty lines of a file.
    Used to build a repo map so Claude understands each file's purpose
    before selecting which ones to read in full.
    """
    content = get_file_content(repo, path, branch)
    if not content:
        return ""
    lines = [l for l in content.splitlines() if l.strip()]
    return "\n".join(lines[:max_lines])


def build_repo_map(
    repo: str,
    file_paths: list[str],
    branch: str = "main",
    max_workers: int = 10,
) -> dict[str, str]:
    """
    Concurrently fetch a short summary (first few lines) of every file.
    Returns {path: summary_text} so Pass 1 can understand the repo
    before selecting files — not just guess from names.
    """
    def _fetch(path: str) -> tuple[str, str]:
        return path, get_file_summary(repo, path, branch)

    summaries: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        for path, summary in executor.map(_fetch, file_paths):
            if summary:
                summaries[path] = summary

    logger.info("Repo map built: %d/%d files summarised for %s@%s",
                len(summaries), len(file_paths), repo, branch)
    return summaries


def fetch_all_file_contents(
    repo: str,
    file_paths: list[str],
    branch: str = "main",
    max_workers: int = 10,
) -> dict[str, str]:
    """
    Concurrently fetch the FULL content of every file in file_paths.
    Returns {path: full_content}.
    Files that fail to fetch or are empty are silently excluded.
    """
    def _fetch(path: str) -> tuple[str, str]:
        return path, get_file_content(repo, path, branch) or ""

    contents: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        for path, content in executor.map(_fetch, file_paths):
            if content:
                contents[path] = content

    logger.info("Fetched full content of %d/%d files for %s@%s",
                len(contents), len(file_paths), repo, branch)
    return contents
