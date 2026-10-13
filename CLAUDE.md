# CLAUDE.md: Retail Sales Analytics, AI Systems Engineering additions

I'm extending this repo to work through a 5-milestone AI systems engineering
curriculum (roadmap below). It runs on my own time and money, so two hard rules
come first: **keep my laptop light**, and **AWS spend stays inside my
credits**. The existing pipeline, chatbot and triage agent must keep working,
and CI stays green.

## Hard rule 1: keep my laptop light

My laptop is low on memory. Small work stays local; anything heavy runs in a
**GitHub Codespace** (2-core / 8 GB by default).

- **Fine on the laptop:**
  - Editing code, SQL, docs and the README.
  - git, `gh`, and running Claude Code.
  - The existing `.venv` for unit tests (`pytest -q tests`) and small Python
    packages.
  - Small files, including the `pics/*.jpg` screenshots.
  - The few real-AWS commands (see Hard rule 2), because Redshift already
    allows my laptop's IP.
- **Codespace only:**
  - Docker / Docker Compose and every container.
  - kind / Kubernetes, k6, LocalStack.
  - Airflow (`airflow/requirements.txt`).
  - Playwright + Chromium, and Ollama or any model.
  - Large ML packages (torch, sentence-transformers, …).
  - Data files over ~50 MB.

  Never install Docker Desktop on my machine. If a new package is big (roughly
  50 MB+ installed, or it pulls in heavy dependencies), install it in the
  Codespace, not in my local `.venv`. Ask if unsure.
- **Code moves through git:**
  - Commit on the milestone branch locally, push, then `git pull` in the
    Codespace.
  - Screenshots go the other way: commit them in the Codespace, then pull them
    locally.
  - Never copy anything else large back to the laptop.
- **Running heavy commands from the laptop:** use
  `gh codespace ssh -c <name> -- '<command>'` (get the name from
  `gh codespace list`). Check it with `echo ok` first.
  - The devcontainer needs the `sshd` feature for this.
  - For long heavy sessions, I may open Claude Code inside the Codespace instead.
  - View dashboards (Grafana, Jaeger, Argo CD) in my browser through Codespaces
    port forwarding. A browser tab is fine.
- **Secrets:** never copy `.env` into the Codespace. Use GitHub Codespaces
  secrets for the repo.
- Keep the Codespace under 8 GB RAM:
  - Put services in `docker-compose.yml` **profiles** (e.g. `app`, `obs`, `stream`,
    `auth`) and start only what the current task needs.
  - Run `docker stats --no-stream` and `free -h` before starting more containers.
  - Stop with `docker compose --profile <x> down` when a task is done, and run
    `docker system prune -f` now and then (Codespace storage counts against my quota).
  - If a task needs more (e.g. kind + the full stack), say so and suggest a
    4-core machine for that session. Don't squeeze it in.
- Managed free tiers (Grafana Cloud, Neon/Supabase Postgres with pgvector,
  Upstash Redis) are fine when they save RAM, but **ask first**, because each
  one needs a signup and a secret.
- No GPU here. The "self-hosted model" step uses a tiny Ollama model inside the
  Codespace only if it fits in memory. Otherwise document it and skip it.

## Hard rule 2: stay inside my AWS credits

**AWS credits remaining: $____ · monthly spend cap: $____** (I keep these updated;
if they're blank, ask me before touching AWS.)

- **Ask before anything that costs money or changes AWS:** `terraform apply` /
  `destroy`, any `aws ... create-* / put-* / run-* / delete-*`, Bedrock calls in
  a loop, and COPY or large queries on Redshift. Show the `terraform plan`
  summary and a rough cost estimate first. Read-only `describe/list/get` calls
  are fine.
- **New course infrastructure never goes on real AWS.** It runs as containers in
  the Codespace, or in LocalStack for AWS-shaped services:
  - Data and caching: Postgres, Redis
  - Observability: Prometheus, Grafana, OpenTelemetry Collector, Jaeger
  - Streaming: Redpanda
  - Kubernetes and deploys: kind, Argo CD
  - Security: Keycloak, Vault (dev mode)
  - AWS-shaped: LocalStack S3/SQS
- **Never create** NAT gateways, EKS, MWAA, EC2, RDS, ElastiCache, MSK,
  OpenSearch, SageMaker endpoints, provisioned Redshift, or anything else
  billed by the hour. Don't raise `redshift_daily_rpu_hours` (3) or
  `redshift_base_rpu` (8) in `infra/terraform/variables.tf`.
- **Load and chaos tests never touch Redshift or a real LLM.**
  - Point them at the Codespace Postgres copy of the `chat.*` views with
    `LLM_PROVIDER=fake`.
  - Add `chatbot/llm_fake.py` exposing `json_call()`, `text_call()` and
    `model_name()`, with canned SQL/answers and configurable latency and error
    rate. Register it in `PROVIDERS` in `chatbot/llm.py`.
- **Real Bedrock calls are only for demos, screenshots and the eval run.**
  - Use Claude Haiku 4.5 or Nova Lite (already allowed in Terraform).
  - At most ~20 calls per session unless I OK more.
  - Print tokens and estimated cost afterwards.
- **The Cost Explorer API charges per request.** Don't call `aws ce` in loops. I
  check spend in the Billing console myself.
- **Add a budget guard:** an `aws_budgets_budget` in `infra/terraform` (monthly
  limit = my cap above; email alerts at 50 / 80 / 100% actual and 100%
  forecast), like the one in Member_Engagement_Pipeline_AWS. Apply only after I
  approve.
- **Codespace IP vs `allowed_cidr`:** Redshift only accepts my laptop's /32.
  - Run the occasional Redshift step (seed, chat-view export, demo query) from
    the laptop's `.venv`. These are small Python scripts.
  - Only if a Redshift step must run in the Codespace (whose IP changes between
    sessions): get its IP with `curl -s https://checkip.amazonaws.com`, show me
    a targeted security-group plan, and apply only on my OK.
  - Never open `0.0.0.0/0`.
- **At the end of any session that used AWS**, list what was called and the
  estimated cost.

## Roadmap: one milestone per branch and PR

Use a branch per milestone (`course/p1-observability`, `course/p2-services`, …)
with a PR into `dev`. CI must pass, and the README is updated in the same PR.
Every milestone ends with an ADR in `docs/adr/` and a short "what broke and why"
note in `docs/`.

1. **Diagnostics and observability**
   - `.devcontainer/devcontainer.json` (Python 3.11, docker-in-docker, sshd,
     GitHub CLI, Terraform, AWS CLI, Claude Code), so a new Codespace starts
     ready and is reachable with `gh codespace ssh`.
   - `docker-compose.yml` running the chatbot, a Postgres copy of the `chat.*`
     views, and Redis.
   - OpenTelemetry traces (FastAPI → LLM → DB spans) to Jaeger.
   - Prometheus metrics and a Grafana golden-signals dashboard, with the JSON
     committed under `observability/`.
   - A k6 load test at 1x and 10x (run k6 as a container).
   - A C4 diagram in Mermaid, plus an ADR on where the latency goes.
2. **Service architecture and data tier**
   - `/v1/chat`, keeping the old route.
   - `Idempotency-Key` support on POST requests.
   - Move the 15-minute in-process cache in `chatbot/app.py` to Redis, and
     invalidate it on each new `load_id`.
   - SQL generation as a gRPC service.
   - A migration-safety check in CI.
3. **Production operations**
   - A kind cluster with manifests or Helm, deployed by Argo CD.
   - A Redpanda `load_completed` event from the Airflow DAG that invalidates the
     cache.
   - SLOs and alert rules.
   - Chaos tests: kill Postgres, inject LLM errors, and verify the fallback.
4. **Security and compliance**
   - A STRIDE threat model.
   - Keycloak OIDC login on the chatbot.
   - Per-`client_id` access, so a brand only sees its own data.
   - Secrets out of `.env` (Codespaces secrets or Vault dev mode).
   - Trivy, gitleaks and bandit in CI.
   - A short AI-governance note.
5. **AI system**
   - pgvector RAG over `config/chat_catalog.yaml`, the view docs and past triage
     notes.
   - Provider adapter for Bedrock / Gemini / fake (optionally Ollama).
   - A prompt-injection test suite against `chatbot/guard.py`.
   - A ~50-question eval set with LLM-as-judge scoring.
   - Cost and latency per request in Grafana.

## Screenshots for the README

- **How to capture:** Playwright with headless Chromium in the Codespace
  (`pip install playwright && python -m playwright install --with-deps chromium`).
  - Drive it from `scripts/screenshots.py`, so every shot can be retaken.
  - Viewport 1440×900. Save JPEG (quality ~85) to `pics/`, ideally under 300 KB.
- **Naming:** follow the existing style, `pics/<area>_<what>.jpg` (e.g.
  `obs_grafana_golden_signals.jpg`, `obs_jaeger_llm_span.jpg`,
  `loadtest_k6_10x.jpg`).
- **Only capture real results.** Run the load test for a few minutes so the
  dashboards have data. Set the time range so the interesting moment is visible:
  the 10x spike, the cache-hit latency drop, the failover.
- **Terminal output** (k6 summary, eval scores, chaos results): render with
  `rich` (`Console(record=True)` → `save_svg`), then screenshot that, so it
  matches `pics/triage_cli.jpg`.
- **Diagrams** (C4, architecture): use Mermaid in the README (GitHub renders it),
  not images.
- **Redact before saving.** No AWS account IDs, ARNs, endpoints, IPs, emails,
  tokens or Codespace URLs. Open every image and check it before committing.
- **AWS console screens** (Budgets, Bedrock usage, Redshift usage limit) need my
  login. Don't attempt them. List them as a checklist in the PR description and
  I'll take them.
- **README style:** match what's there.
  - A short section per addition: what it does and why.
  - The image with specific alt text and an italic one-line caption saying what
    to notice.
  - A new row in "At a glance".
  - Updated "Cost" and "Layout" sections.

Shots per milestone:

| Milestone | Screenshots |
|---|---|
| 1 | Grafana golden signals at 1x vs 10x · Jaeger trace showing the LLM span dominating · k6 summary |
| 2 | `/docs` (Swagger) showing `/v1` · cache hit vs miss latency · idempotent replay test output |
| 3 | Argo CD app synced · `kubectl get pods` during a chaos test · SLO / error-budget panel |
| 4 | Keycloak login · 403 for another brand's data · CI security-scan checks passing |
| 5 | Eval scorecard · prompt-injection suite results · cost and latency per request panel |
