#!/bin/sh
# Seeds Vault (dev mode) for the auth profile, then exits:
#   - the chatbot's database password at secret/retail/chatbot
#   - a read-only policy for exactly that path
#   - a 24 h token with only that policy, written to a shared volume the chatbot mounts read-only
# The root token never reaches the chatbot; the chatbot's env holds no password.
set -eu
export VAULT_ADDR=http://vault:8200 VAULT_TOKEN="$VAULT_DEV_ROOT_TOKEN"
until vault status >/dev/null 2>&1; do sleep 1; done
vault kv put -mount=secret retail/chatbot CHAT_REDSHIFT_PASSWORD="$CHAT_READER_PASSWORD" >/dev/null
vault policy write chatbot-read - >/dev/null <<'POLICY'
path "secret/data/retail/chatbot" { capabilities = ["read"] }
POLICY
vault token create -policy=chatbot-read -ttl=24h -field=token > /vault-token/chatbot
chmod 444 /vault-token/chatbot
echo "vault seeded: secret/retail/chatbot, token with policy chatbot-read"
