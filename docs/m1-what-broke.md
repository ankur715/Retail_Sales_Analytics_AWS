# Milestone 1: what broke and why

| # | What broke | Why | Fix |
|---|---|---|---|
| 1 | The first Codespace came up "Available" but had no Docker, no `.venv` and an Alpine shell | The base image's Yarn apt source has an expired signing key, so `apt-get update` failed inside the docker-in-docker feature. GitHub then fell back to a recovery container instead of failing visibly. | `.devcontainer/Dockerfile` removes that apt source before features install. Lesson: check `gh codespace logs`, not just the state. |
| 2 | `LLM_PROVIDER=fake` would have made the **triage agent call real Bedrock** on every failed load in a test | The triage gate was `LLM_PROVIDER != "none"`, so any new value turned it on | Changed the gate to an allowlist (`gemini` or `bedrock` only), pinned by a test |
| 3 | The chatbot couldn't connect to the local Postgres | `sslmode="require"` was hardcoded for Redshift | New `REDSHIFT_SSLMODE` setting (default `require`; the compose stack sets `disable`) |
| 4 | Grafana's error tile showed **"No data"** during a run with zero errors | With no 5xx responses, the 5xx series doesn't exist, so the division returns nothing | `(… or vector(0)) / …` |
| 5 | The first dashboard screenshot showed 0.35 req/s, 0 in flight and a p95 of 11.7 s | The time range ran past the end of the test, so the "last value" tiles showed the wind-down, and the last few slow requests dominated a thin 1-minute window | End the range while 10x is still running |
| 6 | Grafana and the collector idled at 80% and 66% of their memory caps | The caps (256 MB, 128 MB) were guesses | Raised them to 384 MB and 192 MB after watching `docker stats` |
| 7 | `git push` from the Codespace over `gh codespace ssh` failed | SSH sessions don't get the Codespace's GitHub token (browser and VS Code sessions do) | Copied the reviewed screenshots (checksums matched) and committed them from the laptop |
| 8 | Grafana's p95 (6.6 s) didn't match k6's (5.1 s) | Prometheus interpolates percentiles inside wide histogram buckets | Documented. Finer buckets come before SLO work (ADR 0001). |
| 9 | Unit tests and git on the laptop were at risk mid-milestone | The laptop dropped to 130 MB free disk: macOS swap (1 GB) and app caches (2.7 GB), not this project | Cleared the caches (2.4 GB back). All heavy work stays in the Codespace. |
