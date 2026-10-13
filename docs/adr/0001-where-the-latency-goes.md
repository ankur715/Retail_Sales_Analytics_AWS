# ADR 0001: Where the chatbot's latency goes, and what to optimize

- Status: accepted
- Date: 2026-10-12
- Milestone: 1 (diagnostics and observability)

## Context

The chatbot answers each question in a fixed sequence: `route` (LLM) → known values (DB,
cached for an hour) → `write_sql` (LLM) → validate (sqlglot) → query (DB) → data-as-of (DB)
→ `summarize` (LLM). We had no measurements, only the guess that "the LLM is slow".

Milestone 1 added an OpenTelemetry span per step (Jaeger), per-step and per-request
Prometheus histograms, a golden-signals Grafana dashboard, and a k6 load test. The test ran
in a GitHub Codespace against the local Compose stack: the real chatbot code, a Postgres
copy of the `chat.*` views, and `LLM_PROVIDER=fake` (each model call sleeps a random,
exponentially distributed ~800 ms and returns canned SQL). It never touched Redshift or a
real model. Every question had a unique suffix, so the answer cache never hit.

## Measurements (2026-10-12, 3 min per load level)

| | 1x (2 VUs) | 10x (20 VUs) |
|---|---|---|
| Requests / throughput | 157 / 0.85 req/s | 1,495 / 7.99 req/s (9.4x) |
| Failed | 0% | 0% |
| p50 / p95 / p99 (k6) | 1.98 / 4.23 / 5.80 s | 2.17 / 5.13 / 7.10 s |
| Share of request time in the 3 LLM calls | 99% | 98.6% |
| Share in DB queries + SQL validation | ~1.3% | 1.4% |

The three LLM calls split the time about evenly (32–35% each). The fake model gives every
call the same delay distribution; a real model would differ by step, because `write_sql`
and `summarize` produce more output tokens than `route`.

## Decision

1. **Optimize the LLM path, not the database.** Database work is about 1.4% of latency;
   even making it free would save less than 100 ms at p95.
2. In order of expected payoff:
   - **Cache answers in Redis** (milestone 2), invalidated on each new `load_id`. A hit
     skips all three LLM calls. Repeated questions are common, because the data changes
     only weekly.
   - **Merge `route` and `write_sql`** into one structured call when the catalog is small
     enough to send whole. That removes one of the three sequential calls (about a third
     of the latency).
   - **Stream the summary** to the page, so the time to first word drops even though the
     total time doesn't.
   - **Use a smaller, faster model for `route`**, a short classification-style call.
3. **Treat the provider's rate limit as the real capacity ceiling, not this service.**
   Throughput scaled almost linearly (9.4x for 10x the users), because the time is spent
   waiting on I/O, not on CPU. With a real provider, throttling (Bedrock tokens-per-minute
   quotas, Gemini rate limits) will fail first. The chaos tests in milestone 3 inject it with
   `FAKE_LLM_ERROR_RATE`.

## Consequences and caveats

- **Concurrency limit:** each request holds one worker thread for its whole duration,
  mostly waiting. FastAPI's default thread pool has 40 threads, so beyond ~40 concurrent
  requests, new ones queue. 10x (20 in flight) stayed below that. Async LLM calls, or more
  workers, would lift the limit.
- **Grafana's p95 is coarser than k6's:** Prometheus estimates percentiles from the
  histogram buckets (…, 4, 8, … s), so Grafana showed p95 ≈ 6.6 s where k6 measured 5.1 s.
  Add 3, 5 and 6 s buckets before using these panels for SLOs (milestone 3).
- **Fake latency is a model, not a measurement:** the absolute numbers say nothing about
  Bedrock or Gemini speed. The conclusion that matters, sequential LLM calls versus a
  millisecond-scale database, holds for any real model, which is slower still.
