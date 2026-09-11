"""Retrieval layer — Qdrant hybrid index and its sparse encoder."""
from search.qdrant_index import (DENSE, SPARSE, eligibility_filter,
                                 ensure_collection, hybrid_search, make_client,
                                 to_point, upsert_points)
from search.sparse import encode_documents, encode_query

__all__ = ["DENSE", "SPARSE", "make_client", "ensure_collection",
           "eligibility_filter", "hybrid_search", "to_point", "upsert_points",
           "encode_documents", "encode_query"]
