"""The vector index in Postgres + pgvector.

    rag.chunks(id, kind, title, body, views text[], brand, meta jsonb, embedding vector(384))
    rag.meta(key, value)            -- which embedder built the index

The ingest rebuilds the table in one transaction (the corpus is small), with an HNSW index for
cosine distance. Queries read as the chatbot's read-only user (SELECT on schema rag only).
Vectors travel as pgvector's text form ('[0.1,0.2,...]'), so no extra client package is needed.
"""
import json

import psycopg2

from rag.documents import Chunk
from rag.embed import DIM


def connect(dsn: str):
    # pgvector lives in schema `extensions` (docker/postgres/initdb/20_rag.sql), not public
    return psycopg2.connect(dsn, connect_timeout=5, options="-c search_path=rag,extensions,public")


def _vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def rebuild(conn, chunks: list[Chunk], embedder) -> int:
    vectors = embedder.documents([c.embed_text for c in chunks])
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS extensions")
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions")
        cur.execute("GRANT USAGE ON SCHEMA extensions TO chat_reader")
        cur.execute("CREATE SCHEMA IF NOT EXISTS rag")
        cur.execute("DROP TABLE IF EXISTS rag.chunks")
        cur.execute(f"""CREATE TABLE rag.chunks (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
            views TEXT[] NOT NULL DEFAULT '{{}}', brand TEXT, meta JSONB NOT NULL DEFAULT '{{}}',
            embedding vector({DIM}) NOT NULL)""")
        for c, v in zip(chunks, vectors):
            cur.execute("INSERT INTO rag.chunks VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector)",
                        (c.id, c.kind, c.title, c.body, c.views, c.brand, json.dumps(c.meta), _vec(v)))
        cur.execute("CREATE INDEX chunks_embedding ON rag.chunks USING hnsw (embedding vector_cosine_ops)")
        cur.execute("CREATE TABLE IF NOT EXISTS rag.meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        cur.execute("INSERT INTO rag.meta VALUES ('embedder', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                    (embedder.name,))
        cur.execute("GRANT USAGE ON SCHEMA rag TO chat_reader")
        cur.execute("GRANT SELECT ON ALL TABLES IN SCHEMA rag TO chat_reader")
    conn.commit()
    return len(chunks)


def embedder_name(conn) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM rag.meta WHERE key = 'embedder'")
        row = cur.fetchone()
    return row[0] if row else None


def search(conn, query_vector: list[float], kind: str, k: int, views: list[str] | None = None,
           brands: frozenset[str] | None = None) -> list[dict]:
    """Nearest chunks of one kind. views: only chunks whose views are all among these.
    brands: None = no restriction; otherwise brand-specific chunks must be in this set."""
    where, params = ["kind = %s"], [kind]
    if views is not None:
        where.append("views <@ %s::text[]")
        params.append(list(views))
    if brands is not None:
        where.append("(brand IS NULL OR brand = ANY(%s::text[]))")
        params.append(sorted(brands))
    sql = (f"SELECT id, title, body, views, brand, meta, 1 - (embedding <=> %s::vector) AS similarity "
           f"FROM rag.chunks WHERE {' AND '.join(where)} ORDER BY embedding <=> %s::vector LIMIT %s")
    vec = _vec(query_vector)
    with conn.cursor() as cur:
        cur.execute(sql, [vec, *params, vec, k])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
