"""(Re)build the retrieval index.

    python -m rag.ingest                      # sample triage notes, embedder from RAG_EMBEDDER
    python -m rag.ingest --embedder fastembed
    python -m rag.ingest --notes notes.jsonl  # real notes (one JSON object per line), e.g. exported
                                              # read-only from etl.load_audit on the laptop

Needs RAG_ADMIN_DATABASE_URL (owner of schema rag); the chatbot only reads.
"""
import argparse
import json
import os
import sys
from collections import Counter

from rag import documents, embed, store


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Rebuild the RAG index")
    parser.add_argument("--embedder", default=os.environ.get("RAG_EMBEDDER", "hash"))
    parser.add_argument("--notes", help="JSONL triage notes (default: the synthetic sample)")
    parser.add_argument("--dsn", default=os.environ.get("RAG_ADMIN_DATABASE_URL", ""))
    args = parser.parse_args(argv)
    if not args.dsn:
        parser.error("set RAG_ADMIN_DATABASE_URL or pass --dsn")
    notes = [json.loads(line) for line in open(args.notes)] if args.notes else None
    chunks = documents.all_chunks(notes)
    conn = store.connect(args.dsn)
    try:
        n = store.rebuild(conn, chunks, embed.get(args.embedder))
    finally:
        conn.close()
    kinds = Counter(c.kind for c in chunks)
    print(f"indexed {n} chunks with {args.embedder}: " + ", ".join(f"{v} {k}" for k, v in sorted(kinds.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
