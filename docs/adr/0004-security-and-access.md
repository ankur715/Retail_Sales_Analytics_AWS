# ADR 0004: Login, per-brand access, secrets and supply-chain scanning

- Status: accepted
- Date: 2026-10-14
- Milestone: 4 (security and compliance)

## Context

Until now, anyone who could reach the chatbot could ask about any brand. The brands are
competitors, so that is the main risk. Secrets lived in `.env` and environment variables. No
check stopped a vulnerable package, a leaked key or a root container from shipping. The model
writes the SQL, so any access rule that depends on the model can be talked around (prompt
injection). The STRIDE threat model is in `docs/security/threat-model.md`.

## Decisions

**1. Keycloak (OIDC) for login; the chatbot only verifies tokens.**
- **Browser:** the chat page uses the authorization-code flow with PKCE as a *public* client
  (`chatbot-web`), so no client secret exists in the browser.
- **API:** the chatbot checks each access token's RS256 signature against the realm's JWKS,
  plus issuer, audience (`chatbot-api`) and expiry. Only RS256 is accepted.
- **Brands:** they come from Keycloak **groups** (`brand-C101`, …, `all-brands`) in a `groups`
  claim. Groups avoid Keycloak 24+'s user-profile rules for custom attributes, and an admin
  manages them in the UI.
- **Realm as code:** the realm is in git (`deploy/keycloak/realm-retail.json`) with no
  passwords. User passwords are substituted at import from a value generated per Codespace.
- `AUTH_MODE=off` keeps the old behaviour for tests, CI, the laptop and the load and chaos
  tests.

**2. Per-brand access is enforced in the SQL guard, not in the prompt or the model.**
- **The enforcement:** after validation, every view with a `brand` column is replaced by
  `(SELECT * FROM view WHERE brand IN (<caller's brands>)) AS <same name>`. It works the same
  for joins, CTEs, subqueries and unions, whatever the model wrote (including `OR 1=1`, or no
  filter at all).
- **A clear 403:** a SQL literal naming another brand returns "You don't have access to X's
  data" instead of an empty result.
- **Less to leak:** the model only sees the caller's brands as known values.
- **Keys scoped per caller:** cache and stale-fallback keys include the caller's brand scope,
  and idempotency keys include their subject. Without this, milestone 2's cache would have
  served one brand's answer to another.
- Database row-level security would be stronger still, enforced even if the guard had a bug,
  but Redshift's RLS needs per-user database sessions. The guard keeps one read-only
  connection user and a single place to test. Recorded as a follow-up.

**3. Secrets: Vault first, then the environment.** `etl/secrets.py` reads the process's own
Vault path (KV v2) with a token from a mounted file. In the auth stack, `vault-init` writes
the chatbot's DB password to `secret/retail/chatbot` and creates a 24 h token whose policy
reads only that path. The chatbot's environment holds no password (verified). Without Vault,
values come from the environment: Codespaces secrets in a Codespace, the gitignored `.env` on
the laptop. Local credentials are generated per Codespace into `.secrets/local.env`
(gitignored, mode 600). Vault runs in dev mode, which is local only (threat model E3).

**4. Scans in CI, failing the build:**

| Job | Tool | What fails it |
|---|---|---|
| `secret-scan` | gitleaks | Any secret in **all of git history**. The allowlist covers only documented throwaway local defaults and placeholder examples. |
| `sast` | bandit | Medium+ severity with medium+ confidence. The 12 low-confidence B608 hits were reviewed: they interpolate only table and column names from code or config, never data. |
| `trivy` | Trivy | Dockerfile, Kubernetes and Terraform misconfiguration, secrets, and **HIGH/CRITICAL fixable CVEs in the built image** (OS and Python packages) |

Accepted findings live in `.trivyignore` with a reason and an expiry date, so they fail the
build again when the date passes.

**5. Containers and pods hardened.**
- **Image:** runs as uid 10001, with packaging tools patched.
- **Every pod**, ours and third-party (Postgres, Redis, Redpanda, Prometheus, Grafana):
  - non-root, no privilege escalation;
  - all capabilities dropped, seccomp `RuntimeDefault`;
  - a read-only root filesystem, with emptyDirs only where a process writes;
  - no service-account token except Prometheus (for pod discovery);
  - no service-link env vars.
- **Verified:** every pod starts, writes to the root filesystem fail, and the milestone 3 chaos
  suite still passes 6/6 on the hardened cluster.

## Results

- **Live access demo** (Keycloak + Vault + chatbot): no token → 401; a token with edited groups
  → 401; Aurora → only Aurora's rows; Aurora asking about Cobalt → 403; Aurora asking for
  "every brand" → still only Aurora; Cobalt → Cobalt; admin → all three brands.
- **CI:** 9 checks pass on every PR (3 new security scans).
- **Tests:** 234 unit tests, 35 of them new security tests (forged, expired, wrong-audience,
  wrong-issuer and algorithm-swap tokens; confinement across query shapes; isolation; CSP).
- **Git history:** checked by gitleaks and by hand. Only placeholders were ever committed (the
  example password is a placeholder; the example IP is in RFC 5737's documentation range).

## Consequences

- Access control now depends on the guard staying correct. It is the most-tested code in the
  repo, and milestone 5 adds a prompt-injection suite aimed at it.
- Tokens live in the tab's `sessionStorage`. With the strict CSP and no `innerHTML`, XSS is
  unlikely, but a stolen token works for up to 15 minutes. A backend-for-frontend with an
  httpOnly cookie would remove that; it's worth it once there are real users.
- Internal hops (Redis, Redpanda, sqlgen, Vault) have no TLS or auth. That's fine inside a
  Codespace, not for a shared deployment (threat model S4).
- **Owner follow-ups:** the Redshift security group's egress rule (needs an approved plan and
  apply), a per-user rate limit, and an audit log.

## Alternatives considered

- **API keys per brand:** simpler, but no single sign-on, no expiry, and keys get shared.
- **Filtering in the prompt** ("only answer about Aurora"): any injection defeats it. That's
  exactly what the guard exists to prevent.
- **Database row-level security:** stronger, but needs per-user Redshift sessions; noted as the
  next step if the guard ever proves insufficient.
- **SOPS or sealed secrets in git:** a good fit for GitOps (milestone 3's Argo CD). Vault was
  chosen because the roadmap asked for it and it demonstrates scoped, short-lived tokens.
