#!/usr/bin/env bash
# Read-only chat_reader, like the Redshift user (chatbot/setup_reader.py): SELECT on the chat
# schema only (granted in 10_chat_views.sql) and a 30 s statement timeout.
set -euo pipefail
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v pw="$CHAT_READER_PASSWORD" <<'SQL'
CREATE ROLE chat_reader LOGIN PASSWORD :'pw';
ALTER ROLE chat_reader SET statement_timeout = '30s';
REVOKE ALL ON SCHEMA public FROM PUBLIC;
SQL
