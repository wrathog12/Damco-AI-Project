"""
BM25 sparse vectors, via fastembed.

The dense half of the index is a paid API call; this half is deliberately local
and free. BM25 is not a neural model — tokenisation plus term frequencies — so
it needs no GPU, no torch, and no network. Qdrant applies IDF server-side
(`Modifier.IDF` on the collection), which is why `embed_documents` returns raw
term frequencies and does not try to weight them here.

Documents and queries use different fastembed entry points (`embed` vs
`query_embed`) and the distinction is not cosmetic: BM25 query encoding omits
the term-frequency saturation that only makes sense for documents.
"""
from typing import Iterable, Sequence

from fastembed import SparseTextEmbedding

MODEL_NAME = "Qdrant/bm25"

SparseVec = tuple[list[int], list[float]]


class SparseEncoder:
    """Lazy singleton wrapper — loading the tokeniser twice is pure waste."""

    _model: SparseTextEmbedding | None = None

    @classmethod
    def model(cls) -> SparseTextEmbedding:
        if cls._model is None:
            cls._model = SparseTextEmbedding(model_name=MODEL_NAME)
        return cls._model

    # fastembed returns numpy arrays, so indices arrive as np.int32. Those do not
    # JSON-serialise for Qdrant's REST transport — cast to builtins here rather
    # than discovering it as an opaque encoder error at upsert time.
    @classmethod
    def encode_documents(cls, texts: Sequence[str]) -> list[SparseVec]:
        return [([int(i) for i in e.indices], [float(v) for v in e.values])
                for e in cls.model().embed(list(texts))]

    @classmethod
    def encode_query(cls, text: str) -> SparseVec:
        for e in cls.model().query_embed(text):
            return [int(i) for i in e.indices], [float(v) for v in e.values]
        # An all-stopword query ("is it for me?") legitimately yields no terms.
        # Return an empty vector rather than raising: the dense half of the
        # hybrid search still has something useful to say.
        return [], []


def encode_documents(texts: Iterable[str]) -> list[SparseVec]:
    return SparseEncoder.encode_documents(list(texts))


def encode_query(text: str) -> SparseVec:
    return SparseEncoder.encode_query(text)
