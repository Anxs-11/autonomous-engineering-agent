import logging

from app.services.rag.indexer import get_collection

logger = logging.getLogger(__name__)


def retrieve_relevant_chunks(query: str, repo: str, top_k: int = 5) -> list[dict]:
    """
    Search ChromaDB for code chunks most relevant to the query.

    Returns list of dicts: {text, file, start_line, end_line, distance}.
    """
    collection = get_collection(repo)
    if collection is None:
        logger.warning("ChromaDB unavailable — skipping retrieval for %s.", repo)
        return []

    count = collection.count()

    if count == 0:
        logger.warning("ChromaDB collection for %s is empty — skipping retrieval.", repo)
        return []

    results = collection.query(
        query_texts=[query],
        n_results=min(top_k, count),
        include=["documents", "metadatas", "distances"],
    )

    chunks: list[dict] = []
    for doc, meta, dist in zip(
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0],
    ):
        chunks.append({
            "text": doc,
            "file": meta.get("file", "unknown"),
            "start_line": meta.get("start_line", 0),
            "end_line": meta.get("end_line", 0),
            "distance": round(dist, 4),
        })

    logger.info("Retrieved %d relevant chunks for: %s...", len(chunks), query[:60])
    return chunks


def format_context_for_llm(chunks: list[dict]) -> str:
    """
    Format retrieved code chunks into a structured string for the code-generation LLM.
    """
    if not chunks:
        return ""

    parts = ["=== RELEVANT CODEBASE CONTEXT ===\n"]
    for i, chunk in enumerate(chunks, 1):
        parts.append(
            f"[{i}] File: {chunk['file']} "
            f"(lines {chunk['start_line']}-{chunk['end_line']})\n"
            f"```\n{chunk['text']}\n```\n"
        )

    return "\n".join(parts)
