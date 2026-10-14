# Keycloak realm `retail` (milestone 4)

`realm-retail.json` is imported when the `auth` profile starts (`deploy/auth/up.sh`).

| | |
|---|---|
| Groups | `brand-C101` (Aurora Apparel), `brand-C102` (Bramble Footwear), `brand-C103` (Cobalt Outdoor), `all-brands` |
| `chatbot-web` | the chat page: public client, authorization code + PKCE (S256), redirects only to `http://localhost:8000/*` |
| `chatbot-test` | local test scripts only: public client with the password grant, used by `scripts/access_demo.py` |
| Token claims | `aud` includes `chatbot-api` (checked by the chatbot); `groups` lists the user's groups |
| Users | `aurora.analyst`, `bramble.analyst`, `cobalt.analyst` (one brand each), `retail.admin` (all brands) |

No password is in git: users get `${KC_TEST_USER_PASSWORD}`, which `deploy/auth/up.sh` generates
into `.secrets/local.env` (gitignored, Codespace only) the first time it runs. Keycloak substitutes
it at import. The realm uses plain HTTP (`sslRequired: none`) because it only listens inside the
Codespace; a real deployment needs TLS and a confidential backend client (see the threat model).
