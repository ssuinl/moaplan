"""금융 안내 문서 RAG 검색

문서: backend/rag/docs/*.md (머리말에 출처, URL, 확인일)
저장: Chroma, backend/data/chroma
임베딩: GMS 키 있으면 text-embedding-3-small, 없으면 문자 n-gram 해싱 (오프라인용)
색인이 없으면 NO_INDEX
색인 만들기: python -m scripts.build_index
"""
from __future__ import annotations

import hashlib
import math
import os
import re
from pathlib import Path

from .base import REGISTRY, Tool, fail, fake_mode, ok

DOCS = Path(__file__).resolve().parents[1] / "rag" / "docs"
COLLECTION = "moaplan_guides"
DIM = 384


def chroma_dir() -> str:
    return os.getenv("MOAPLAN_CHROMA", str(Path(__file__).resolve().parents[1] / "data" / "chroma"))


class HashEmbedding:
    """문자 2~3-gram 해싱 벡터. 외부 모델 없이 한국어 검색되는 간단한 임베딩"""

    def __call__(self, input):  # chromadb EmbeddingFunction 규약
        return [self._embed(t) for t in input]

    def embed_documents(self, input):
        return self(input)

    def embed_query(self, input):
        return self(input)

    @staticmethod
    def name():
        return "moaplan-hash-ngram"

    def supported_spaces(self):
        return ["cosine", "l2", "ip"]

    def default_space(self):
        return "cosine"

    def is_legacy(self):
        return False

    def get_config(self):
        return {}

    @staticmethod
    def build_from_config(config):
        return HashEmbedding()

    def _embed(self, text: str):
        v = [0.0] * DIM
        s = re.sub(r"\s+", " ", text.lower())
        for n in (2, 3):
            for i in range(len(s) - n + 1):
                g = s[i:i + n]
                if g.strip():
                    h = int(hashlib.md5(g.encode()).hexdigest(), 16)
                    v[h % DIM] += 1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]


def _embedding_fn():
    if not fake_mode() and os.getenv("GMS_API_KEY") and os.getenv("MOAPLAN_EMBED", "gms") == "gms":
        try:
            from chromadb.utils.embedding_functions import OpenAIEmbeddingFunction
            return OpenAIEmbeddingFunction(api_key=os.environ["GMS_API_KEY"],
                                           api_base="https://gms.ssafy.io/gmsapi/api.openai.com/v1",
                                           model_name="text-embedding-3-small")
        except Exception:
            pass
    return HashEmbedding()


def _client():
    import chromadb
    return chromadb.PersistentClient(path=chroma_dir())


def parse_doc(path: Path) -> tuple[dict, list[tuple[str, str]]]:
    raw = path.read_text(encoding="utf-8")
    meta, body = {}, raw
    if raw.startswith("---"):
        _, head, body = raw.split("---", 2)
        for line in head.strip().splitlines():
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
    sections, cur, buf = [], "개요", []
    for line in body.strip().splitlines():
        if line.startswith("## "):
            if buf:
                sections.append((cur, " ".join(buf).strip()))
            cur, buf = line[3:].strip(), []
        elif line.strip():
            buf.append(line.strip())
    if buf:
        sections.append((cur, " ".join(buf).strip()))
    return meta, sections


def build_index() -> int:
    client = _client()
    try:
        client.delete_collection(COLLECTION)
    except Exception:
        pass
    col = client.create_collection(COLLECTION, embedding_function=_embedding_fn(),
                                   metadata={"hnsw:space": "cosine"})
    ids, docs, metas = [], [], []
    for p in sorted(DOCS.glob("*.md")):
        meta, sections = parse_doc(p)
        for i, (heading, text) in enumerate(sections):
            ids.append(f"{p.stem}#{i}")
            docs.append(f"{meta.get('title', p.stem)} — {heading}\n{text}")
            metas.append({"title": meta.get("title", p.stem), "section": heading,
                          "source": meta.get("source", ""), "url": meta.get("url", ""),
                          "checked": meta.get("checked", "")})
    if ids:
        col.add(ids=ids, documents=docs, metadatas=metas)
    return len(ids)


def rag_search(query: str, top_k: int = 3) -> dict:
    query = (query or "").strip()
    if not query:
        return fail("MISSING_ARGUMENT", "검색할 질문이 필요합니다.")
    try:
        col = _client().get_collection(COLLECTION, embedding_function=_embedding_fn())
        if col.count() == 0:
            raise ValueError
    except Exception:
        return fail("NO_INDEX", "금융 안내 문서 색인이 아직 없습니다. (python -m scripts.build_index)")
    res = col.query(query_texts=[query], n_results=max(1, min(int(top_k), 5)))
    hits = []
    for doc, meta, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0]):
        hits.append({"text": doc, "title": meta["title"], "section": meta["section"],
                     "source": meta["source"], "url": meta["url"], "checked": meta["checked"],
                     "score": round(1 - dist, 3)})
    hits = [h for h in hits if h["score"] > 0.05]
    if not hits:
        return fail("NO_RESULT", "질문과 관련된 안내 문서를 찾지 못했습니다.")
    return ok({"query": query, "hits": hits})


REGISTRY.register(Tool(
    name="rag_search",
    description=("외부 API 로는 확인할 수 없는 제도 · 용어 질문(예금자보호 한도, 이자 세금, 단리·복리, 우대금리, "
                 "중도해지 등)에 답해야 할 때 부른다. 근거 문서와 출처를 돌려준다."),
    parameters={"type": "object", "properties": {
        "query": {"type": "string"}, "top_k": {"type": "integer", "default": 3}},
        "required": ["query"]},
    func=rag_search, kind="local", source="자체 색인 (backend/rag/docs, 각 문서 머리말에 원 출처)",
))
