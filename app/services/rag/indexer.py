import logging
import os
from pathlib import Path

from dotenv import load_dotenv

try:
    import chromadb
    from chromadb.utils import embedding_functions
    _CHROMADB_AVAILABLE = True
except Exception as _chroma_err:  # noqa: BLE001
    chromadb = None  # type: ignore[assignment]
    embedding_functions = None  # type: ignore[assignment]
    _CHROMADB_AVAILABLE = False
    import logging as _log
    _log.getLogger(__name__).warning(
        "chromadb unavailable (%s) — RAG indexing disabled.", _chroma_err
    )

from app.services.github_fetcher import get_file_content, list_code_files

load_dotenv()

logger = logging.getLogger(__name__)

# ChromaDB persistent storage directory (alongside aea.db)
CHROMA_DIR = str(Path(__file__).resolve().parents[4] / "chroma_db")

_CHUNK_SIZE = 60    # lines per chunk
_CHUNK_OVERLAP = 10  # lines shared between adjacent chunks
_BATCH_SIZE = 50    # upsert batch size

_client = None
_ef = None


def _get_client():
    if not _CHROMADB_AVAILABLE:
        return None
    global _client
    if _client is None:
        _client = chromadb.PersistentClient(path=CHROMA_DIR)
        logger.info("ChromaDB initialised at %s", CHROMA_DIR)
    return _client


def _collection_name(repo: str) -> str:
    """Convert 'owner/repo' to a valid ChromaDB collection name."""
    name = repo.replace("/", "_").replace("-", "_").lower()
    return name if len(name) >= 3 else name + "_col"


def _get_ef():
    global _ef  # noqa: PLW0603
    if _ef is None and _CHROMADB_AVAILABLE:
        _ef = embedding_functions.DefaultEmbeddingFunction()
    return _ef


def get_collection(repo: str):
    if not _CHROMADB_AVAILABLE:
        return None
    return _get_client().get_or_create_collection(
        name=_collection_name(repo),
        embedding_function=_get_ef(),
        metadata={"hnsw:space": "cosine"},
    )


def is_indexed(repo: str) -> bool:
    """Return True if the repo has already been indexed into ChromaDB."""
    if not _CHROMADB_AVAILABLE:
        return False
    col = get_collection(repo)
    return col is not None and col.count() > 0


def chunk_file(content: str, file_path: str) -> list[dict]:
    """
    Split a file into overlapping line-based chunks.

    Returns list of {'text': str, 'metadata': dict}.
    """
    lines = content.splitlines()
    chunks: list[dict] = []
    step = max(1, _CHUNK_SIZE - _CHUNK_OVERLAP)

    for i in range(0, max(1, len(lines)), step):
        chunk_lines = lines[i: i + _CHUNK_SIZE]
        text = "\n".join(chunk_lines).strip()
        if len(text) < 50:   # skip tiny / blank chunks
            continue
        chunks.append({
            "text": text,
            "metadata": {
                "file": file_path,
                "start_line": i + 1,
                "end_line": min(i + _CHUNK_SIZE, len(lines)),
            },
        })

    return chunks


def index_repository(repo: str, branch: str = "main") -> None:
    """
    Fetch every code file from the GitHub repo and upsert chunks into ChromaDB.

    This is intentionally blocking (called from a background thread in webhook.py).
    Returns the number of chunks stored.
    """
    if not _CHROMADB_AVAILABLE:
        logger.warning("chromadb unavailable — skipping repo indexing.")
        return
    path_filter: str = os.getenv("GITHUB_REPO_PATH_FILTER", "")
    file_paths = list_code_files(repo, branch, path_filter)

    if not file_paths:
        logger.warning("No indexable files found in %s@%s", repo, branch)
        return 0

    collection = get_collection(repo)
    documents: list[str] = []
    metadatas: list[dict] = []
    ids: list[str] = []
    total = 0

    for file_path in file_paths:
        content = get_file_content(repo, file_path, branch)
        if not content:
            continue

        for idx, chunk in enumerate(chunk_file(content, file_path)):
            documents.append(chunk["text"])
            metadatas.append(chunk["metadata"])
            ids.append(f"{repo}::{file_path}::{idx}")
            total += 1

            if len(documents) >= _BATCH_SIZE:
                collection.upsert(documents=documents, metadatas=metadatas, ids=ids)
                documents, metadatas, ids = [], [], []

    if documents:
        collection.upsert(documents=documents, metadatas=metadatas, ids=ids)

    logger.info("Indexed %d chunks from %s into ChromaDB", total, repo)
    return total
