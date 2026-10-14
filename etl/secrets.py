"""Where secrets come from (milestone 4): Vault first, the environment second -- never a file in git.

  VAULT_ADDR set   read VAULT_SECRET_PATH (KV v2, e.g. retail/chatbot) once, with the token in
                   VAULT_TOKEN_FILE (preferred: a file mounted read-only, not an env var) or
                   VAULT_TOKEN. Each process gets a token whose policy reads only its own path.
  otherwise        the environment, as before. In a Codespace that means Codespaces secrets
                   (repo settings), so nothing secret is stored on disk. On the laptop it's .env,
                   which is gitignored and never leaves the machine.

If Vault is configured but unreachable, the error is logged and the environment is used, so
a broken Vault shows up as a clear "password authentication failed" rather than a crash at import.
"""
import logging
import os
from functools import lru_cache

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _vault_values() -> dict:
    addr = os.environ.get("VAULT_ADDR", "")
    if not addr:
        return {}
    token_file = os.environ.get("VAULT_TOKEN_FILE", "")
    token = open(token_file).read().strip() if token_file and os.path.exists(token_file) else \
        os.environ.get("VAULT_TOKEN", "")
    mount, _, path = os.environ.get("VAULT_SECRET_PATH", "secret/retail/app").partition("/")
    import requests   # only when Vault is in use: etl.config stays importable with no extra packages
    try:
        r = requests.get(f"{addr}/v1/{mount}/data/{path}", headers={"X-Vault-Token": token}, timeout=5)
        r.raise_for_status()
        values = r.json()["data"]["data"]
        log.info("loaded %d secret(s) from Vault %s/%s", len(values), mount, path)
        return values
    except Exception as e:   # never log the token or values
        log.error("Vault read of %s/%s failed (%s); falling back to the environment", mount, path, type(e).__name__)
        return {}


def get(name: str, default: str = "") -> str:
    return str(_vault_values().get(name) or os.environ.get(name, default))
