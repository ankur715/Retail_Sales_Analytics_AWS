#!/usr/bin/env bash
# Start the chatbot with login (Keycloak) and Vault -- in the Codespace, never on the laptop:
#   bash deploy/auth/up.sh            then open http://localhost:8000 (forwarded port) and sign in
#   docker compose --profile app --profile auth down     when done
#
# The first run generates every local credential into .secrets/local.env (gitignored): the
# Keycloak admin password, the test users' password, the Vault root token and the chat_reader
# database password. Nothing secret is in git or in the chatbot's environment.
set -euo pipefail
cd "$(dirname "$0")/../.."
mkdir -p .secrets && chmod 700 .secrets
if [ ! -f .secrets/local.env ]; then
  gen() { python3 -c 'import secrets; print(secrets.token_urlsafe(18))'; }
  umask 077
  cat > .secrets/local.env <<ENV
KEYCLOAK_ADMIN_PASSWORD=$(gen)
KC_TEST_USER_PASSWORD=$(gen)
VAULT_DEV_ROOT_TOKEN=$(gen)
CHAT_READER_PASSWORD=$(gen)
ENV
  echo "generated .secrets/local.env"
fi
# Login on; the DB password comes from Vault, so the chatbot's env has none
export AUTH_MODE=oidc VAULT_ADDR=http://vault:8200 CHAT_REDSHIFT_PASSWORD_ENV=
docker compose --env-file .secrets/local.env --profile app --profile auth up -d --build \
  postgres redis sqlgen chatbot keycloak vault vault-init 2>&1 | tail -3
# (not --wait: vault-init is a one-shot that exits, which --wait can report as a failure)
for _ in $(seq 1 60); do
  curl -sf localhost:8000/v1/health >/dev/null && curl -sf localhost:8081/realms/retail >/dev/null && break
  sleep 3
done
docker compose --profile app --profile auth ps --format 'table {{.Service}}\t{{.Status}}'
echo "users: aurora.analyst, bramble.analyst, cobalt.analyst, retail.admin (password: KC_TEST_USER_PASSWORD in .secrets/local.env)"
