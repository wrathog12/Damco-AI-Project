"""Embedding generation — the Pass B half of ingestion."""
from embeddings.document import build_query_text, build_search_document
from embeddings.gemini import EmbeddingError, GeminiEmbedder

__all__ = ["GeminiEmbedder", "EmbeddingError",
           "build_search_document", "build_query_text"]
