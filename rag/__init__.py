"""Retrieval for the chatbot (milestone 5): pgvector search over what the model needs to write
good SQL and explain data problems.

  documents.py  chunks: solved example questions (config/sql_examples.yaml + the catalog's),
                view docs, business rules, and past triage-agent notes (brand-tagged)
  embed.py      text -> vector: `hash` (no dependencies; tests, CI) or `fastembed`
                (BAAI/bge-small-en-v1.5, ONNX on CPU; the Codespace and the chatbot image)
  store.py      Postgres + pgvector: rebuild the index, cosine search with filters
  ingest.py     python -m rag.ingest  -- (re)build the index
  retrieve.py   what the chatbot calls per question; never fails the request

Retrieval respects access: triage notes carry their brand, and a caller only ever gets notes
for brands they can see. Example SQL only comes from examples that use the routed views, so
it also passes the SQL guard.
"""
