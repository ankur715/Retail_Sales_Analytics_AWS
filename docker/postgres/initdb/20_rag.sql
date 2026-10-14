-- Milestone 5: pgvector for retrieval (rag/). The index itself is (re)built by `python -m rag.ingest`
-- as the owner; the chatbot only reads it as chat_reader.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS rag;
GRANT USAGE ON SCHEMA rag TO chat_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA rag GRANT SELECT ON TABLES TO chat_reader;
