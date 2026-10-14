"""All runtime configuration comes from environment variables (.env locally,
Airflow Variables/Connections or Secrets Manager in a real deployment).
Nothing secret is ever hardcoded or committed.

RETAIL_ENV is the one switch between development and production. The same
code runs in both; the environment only changes WHERE data goes:

    env    Redshift database   S3 prefix            DAGs
    dev    retail_dev          s3://<bucket>/dev/   paused on creation, history loads allowed
    prod   retail_prod         s3://<bucket>/prod/  scheduled, rolling 5-week upsert
"""
import os

from dotenv import load_dotenv

from etl import secrets   # Vault first, then the environment (milestone 4)

# Load the project's .env (one folder up from etl/) no matter where the
# script is launched from. Real environment variables win over .env, so
# Airflow/CI can override anything.
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

ENVIRONMENTS = ("dev", "prod")

ENV = os.environ.get("RETAIL_ENV", "dev").lower()
if ENV not in ENVIRONMENTS:
    raise ValueError(f"RETAIL_ENV must be one of {ENVIRONMENTS}, got {ENV!r}")

# --- AWS ---
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
S3_BUCKET = os.environ.get("S3_BUCKET", "")          # terraform output: s3_bucket
S3_ENV_PREFIX = ENV                                   # every key in this env starts with dev/ or prod/

# --- Redshift Serverless (Postgres wire protocol, port 5439) ---
REDSHIFT_HOST = os.environ.get("REDSHIFT_HOST", "")
REDSHIFT_PORT = int(os.environ.get("REDSHIFT_PORT", "5439"))
REDSHIFT_SSLMODE = os.environ.get("REDSHIFT_SSLMODE", "require")   # disable only for the local Postgres copy
REDSHIFT_DB = os.environ.get("REDSHIFT_DB", f"retail_{ENV}")   # retail_dev / retail_prod
REDSHIFT_USER = os.environ.get("REDSHIFT_USER", "admin")
REDSHIFT_PASSWORD = secrets.get("REDSHIFT_PASSWORD", "")
REDSHIFT_IAM_ROLE_ARN = os.environ.get("REDSHIFT_IAM_ROLE_ARN", "")   # role Redshift assumes to COPY

# --- Mock retailer API (mock_api/) ---
RETAILER_API_URL = os.environ.get("RETAILER_API_URL", "http://localhost:9100")
RETAILER_API_TOKEN = secrets.get("RETAILER_API_TOKEN", "local-dev-token")

# --- LLM features: the analytics chatbot (chatbot/) and the triage agent (triage/) ---
# none (the default) turns every LLM feature off, so CI, tests and a fresh clone never
# call a model. gemini | bedrock pick the chatbot's provider; fake answers the chatbot
# with canned SQL and never calls a model (load and chaos tests). The triage agent runs
# on Amazon Bedrock (TRIAGE_MODEL below) only with gemini | bedrock.
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "none").strip().lower()   # none | gemini | bedrock | fake
BEDROCK_MODEL = os.environ.get("BEDROCK_MODEL", "claude-opus-5-5")       # or a full id / inference profile
LLM_BEDROCK_ENDPOINT = os.environ.get("LLM_BEDROCK_ENDPOINT", "mantle").strip().lower()   # mantle | runtime
GOOGLE_API_KEY = secrets.get("GOOGLE_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
FAKE_LLM_LATENCY_MS = float(os.environ.get("FAKE_LLM_LATENCY_MS", "0"))   # mean delay per fake call
FAKE_LLM_ERROR_RATE = float(os.environ.get("FAKE_LLM_ERROR_RATE", "0"))   # 0..1, share raising "busy"
CHAT_REDSHIFT_USER = os.environ.get("CHAT_REDSHIFT_USER", "chat_reader")
CHAT_REDSHIFT_PASSWORD = secrets.get("CHAT_REDSHIFT_PASSWORD", "")
# A person is waiting: the chatbot gives up on a down database fast (the ETL keeps the patient
# 4-attempt default). 10 s still covers a paused Redshift Serverless waking up.
CHAT_DB_CONNECT_TIMEOUT_S = int(os.environ.get("CHAT_DB_CONNECT_TIMEOUT_S", "10"))
CHAT_DB_ATTEMPTS = int(os.environ.get("CHAT_DB_ATTEMPTS", "2"))
# Answer cache + idempotency keys. Unset = an in-process store (tests, a laptop uvicorn);
# the compose stack points it at its Redis.
REDIS_URL = os.environ.get("REDIS_URL", "")
# SQL generation (sqlgen/). Unset = in-process; host:port = the gRPC service (compose: sqlgen:50051).
SQLGEN_ADDR = os.environ.get("SQLGEN_ADDR", "")
SQLGEN_TIMEOUT_S = float(os.environ.get("SQLGEN_TIMEOUT_S", "60"))

# --- Login (milestone 4): Keycloak OIDC. off = no login, every brand visible (tests, CI, the
# laptop, load and chaos tests); oidc = POST /v1/chat needs a Keycloak access token, and the
# token's groups decide which brands the caller sees (chatbot/auth.py, chatbot/access.py).
AUTH_MODE = os.environ.get("AUTH_MODE", "off").strip().lower()
OIDC_ISSUER = os.environ.get("OIDC_ISSUER", "")          # as the browser sees it, e.g. http://localhost:8081/realms/retail
OIDC_JWKS_URL = os.environ.get("OIDC_JWKS_URL", "")      # signing keys, as the server reaches them (default: issuer's)
OIDC_AUDIENCE = os.environ.get("OIDC_AUDIENCE", "chatbot-api")
OIDC_WEB_CLIENT_ID = os.environ.get("OIDC_WEB_CLIENT_ID", "chatbot-web")   # the chat page's public client (PKCE)

# --- Retrieval (milestone 5): pgvector index of example SQL, view docs and triage notes (rag/).
# Off = the catalog's own examples go in the prompt, as before. The index lives in Postgres
# (the Codespace copy; Neon/Supabase in production) -- Redshift has no vector type.
RAG_ENABLED = os.environ.get("RAG_ENABLED", "false").strip().lower() in ("1", "true", "yes")
RAG_DATABASE_URL = secrets.get("RAG_DATABASE_URL", "")     # read-only user
RAG_EMBEDDER = os.environ.get("RAG_EMBEDDER", "hash")       # hash | fastembed (must match the index)
RAG_EXAMPLES_K = int(os.environ.get("RAG_EXAMPLES_K", "4"))
RAG_NOTES_K = int(os.environ.get("RAG_NOTES_K", "2"))

# --- Events (milestone 3): a load_completed message per successful load, on Redpanda (Kafka API) ---
# Unset = no events (CI, tests, the laptop); the chatbot then falls back to its 60 s version check.
EVENTS_BOOTSTRAP = os.environ.get("EVENTS_BOOTSTRAP", "")          # e.g. redpanda:9092
EVENTS_TOPIC = os.environ.get("EVENTS_TOPIC", "retail.load_completed")

# --- Pipeline Triage Agent (triage/): Bedrock Converse tool-use loop over read-only tools ---
# nova-lite (default, cheapest) | claude-haiku | a full Bedrock model id / inference profile
TRIAGE_MODEL = os.environ.get("TRIAGE_MODEL", "nova-lite").strip()
TRIAGE_MAX_STEPS = int(os.environ.get("TRIAGE_MAX_STEPS", "8"))          # model calls per run
TRIAGE_MAX_TOKENS = int(os.environ.get("TRIAGE_MAX_TOKENS", "40000"))    # input + output, whole run
TRIAGE_REDSHIFT_USER = os.environ.get("TRIAGE_REDSHIFT_USER", "triage_reader")   # read-only user
TRIAGE_REDSHIFT_PASSWORD = secrets.get("TRIAGE_REDSHIFT_PASSWORD", "")

# --- Failure email (etl/alerts.py): one email per final task failure, with the triage note ---
# Off unless SMTP_USER, SMTP_PASSWORD and ALERT_EMAIL are all set (CI and tests leave them empty).
# Gmail: smtp.gmail.com:587 with an App Password (Google Account -> Security -> App passwords).
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))           # STARTTLS
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = secrets.get("SMTP_PASSWORD", "")
ALERT_EMAIL = os.environ.get("ALERT_EMAIL", "")               # recipient(s), comma-separated
ALERT_FROM = os.environ.get("ALERT_FROM", "") or SMTP_USER

# --- Automation window: how many weeks every scheduled run restates ---
DEFAULT_LOOKBACK_WEEKS = int(os.environ.get("DEFAULT_LOOKBACK_WEEKS", "5"))


def s3_key(*parts: str) -> str:
    """Build an S3 key inside this environment, e.g. dev/landing/C101/R201/x.csv."""
    return "/".join([S3_ENV_PREFIX, *[p.strip("/") for p in parts]])
