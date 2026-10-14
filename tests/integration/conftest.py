"""Integration tests against a real Postgres + pgvector seeded with the chat views (CI job
ai-integration; or in the Codespace). Skipped unless PG_TEST_DSN (owner) and PG_READER_DSN
(chat_reader) are set -- the unit-test run and the laptop never reach a database."""
import os
from urllib.parse import urlparse

import pytest

ADMIN, READER = os.environ.get("PG_TEST_DSN"), os.environ.get("PG_READER_DSN")
pytestmark = pytest.mark.skipif(not (ADMIN and READER), reason="needs PG_TEST_DSN and PG_READER_DSN")


@pytest.fixture(autouse=True)
def no_redshift(monkeypatch):
    """Overrides the unit-test guard of the same name: here the chatbot reads the real Postgres
    copy, as the read-only chat_reader (like production)."""
    if not READER:
        pytest.skip("needs PG_READER_DSN")
    from etl import config
    u = urlparse(READER)
    for key, value in {"REDSHIFT_HOST": u.hostname, "REDSHIFT_PORT": u.port, "REDSHIFT_DB": u.path.lstrip("/"),
                       "REDSHIFT_SSLMODE": "disable", "CHAT_REDSHIFT_USER": u.username,
                       "CHAT_REDSHIFT_PASSWORD": u.password}.items():
        monkeypatch.setattr(config, key, value)
