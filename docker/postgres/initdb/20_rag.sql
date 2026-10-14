-- Milestone 5: pgvector for retrieval (rag/). The extension goes in its own schema: 00_roles.sh
-- revoked access to `public` from everyone, and the read-only chat_reader needs the vector type
-- and operators -- so it gets USAGE on `extensions` only, not on public.
-- The index itself is (re)built by `python -m rag.ingest` as the owner; chat_reader only reads it.
CREATE SCHEMA IF NOT EXISTS extensions;
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions;
GRANT USAGE ON SCHEMA extensions TO chat_reader;
CREATE SCHEMA IF NOT EXISTS rag;
GRANT USAGE ON SCHEMA rag TO chat_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA rag GRANT SELECT ON TABLES TO chat_reader;
