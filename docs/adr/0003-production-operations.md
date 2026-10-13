# ADR 0003: Running the chatbot like a production service: GitOps, events, SLOs and chaos

- Status: accepted
- Date: 2026-10-13
- Milestone: 3 (production operations)

## Context

After milestone 2 the chatbot had a cache, retries and a separate SQL service, but nothing
showed how it behaves when parts of it fail. Deploys were `docker compose up`. "Is it healthy?"
had no definition. A new data load reached cached answers only through a once-a-minute version
check.

## Decisions

**1. Deploy with GitOps: Argo CD syncs `deploy/k8s` from GitHub into kind.**
- Kustomize, not Helm: there's one environment and nothing to template. The ConfigMaps are
  generated from the files the Compose stack already uses (init scripts, seed CSVs, Prometheus
  rules, Grafana dashboards), so each exists once.
- Automated sync with prune: a pushed manifest reaches the cluster with no `kubectl`. During
  this milestone, the Redpanda fix and the sqlgen Service change were deployed that way.
- `selfHeal: false`: a deliberate live change shows as OutOfSync instead of being reverted in
  seconds. It is not a freeze, though. A newer, unsynced commit is applied the moment the app
  goes OutOfSync. That reverted the first LLM-error experiment, so `chaos/run.py` now pauses
  automated sync for the duration and always restores it.
- The image is built in the Codespace and loaded into kind (`imagePullPolicy` resolves it
  locally). In a real setup, CI would push to a registry and a commit would bump the tag.

**2. `load_completed` events on Redpanda.**
- `etl.steps.finish` publishes after the audit row is committed, so the Airflow DAG and the
  CLI runner both send it. It is keyed by `pipeline_id` (per-feed ordering) and never raises:
  the load already succeeded.
- Every chatbot replica consumes with its own consumer group, because each holds its data
  version in memory and must hear every event. It starts at the latest offset; a restarted
  replica looks the version up fresh anyway.
- The 60 s version check stays as the fallback if Redpanda is down. The event only makes
  invalidation immediate.

**3. Failure behaviour: degrade, don't fail.**

| Failure | What users get |
|---|---|
| Database down | Cached answers keep working (last known version, re-checked every 10 s). Uncached questions get a clear 503 within ~3 s; no 35 s retry hang, no pointless "repair" call to the model. |
| Model or database down, question asked before | The last answer to that question, marked `stale: true` with its `data_as_of`, instead of a 503. It is never stored as the idempotent result, so a retry can still get the live answer. |
| sqlgen pod dies | Retried on `UNAVAILABLE` through the Service. Connect timeout and backoff capped at 1 s (gRPC's defaults: 20 s and 2 min). |
| Redis down | Answers run uncached (milestone 2) |

Readiness checks only the process itself, not its dependencies. Pulling a chatbot out of the
Service while Postgres is down would turn a partial outage, where cached and stale answers
still work, into a full one.

**4. sqlgen behind a ClusterIP Service, not headless with client-side `round_robin`.**
Round-robin balanced per call, but failover depended on DNS answers, which CoreDNS caches. The
ClusterIP lets kube-proxy route reconnects around dead pods. The cost is per-connection
balancing: each chatbot sticks to one sqlgen pod until the server ages the connection out
(30 s). The decisive fix turned out to be the client's connect timeout (§3), so this choice
rests on reasoning, not a measurement. It removes one cache from the failover path.

**5. SLOs with multi-window burn-rate alerts** (`observability/rules/slo.yml`).
- **Availability:** 99.5% of chat requests are not 5xx, over 28 days. Stale fallbacks are
  200s and count as available; `ChatbotServingStale` says a dependency is down.
- **Latency:** 95% of chat requests finish within 8 s. In milestone 1, misses had a p95 of
  about 5 s on the fake model.
- **Alerts:**
  - Page at a 14.4x burn over 1 h confirmed over 5 min, and at 6x over 6 h confirmed over
    30 min. Ticket at 1x over 3 days (Google SRE workbook).
  - Dependency alerts: no healthy chatbot or sqlgen, stale fallbacks served, store errors,
    event listener failing.
- **Tests:** promtool unit tests in CI check the alerts fire, and don't fire, on synthetic
  series.

**6. Chaos tests with traffic inside the cluster.** `chaos/run.py` kills Postgres, one sqlgen
pod, all sqlgen pods and Redis; sends a load event; and runs the fake model at a 50% error
rate. Traffic runs as a pod hitting the chatbot Service, one connection per request.
A port-forward or kept-alive connections would pin a single pod and invent outages, or hide
them. An experiment passes only with zero 500s, dropped connections or timeouts, and only
200s once the dependency is back.

## Results (2026-10-13, kind on a 4-core Codespace, 2 chatbot + 2 sqlgen pods, fake LLM)

| Experiment | Fresh | Cached | Stale | 503 | 500s / dropped | Back in | 200s after |
|---|---|---|---|---|---|---|---|
| Postgres killed | 29 | 72 | 0 | 3 | 0 | 3.6 s | 82/82 |
| One sqlgen killed | 21 | 42 | 0 | 0 | 0 | 3.5 s | 46/46 |
| All sqlgen killed | 28 | 110 | 0 | 6 | 0 | 1.9 s | 112/112 |
| New load (event) | 26 | 10 | 0 | 0 | 0 | 0.8 s | 28/28 |
| LLM errors 50% (+ a new load) | 45 | 46 | **41** | 20 | 0 | 41 s (two rollouts) | 23/23 |
| Redis killed | 27 | 10 | 0 | 0 | 0 | 1.2 s | 31/31 |

- **All six passed.**
- **Load event:** the first fresh answer to a repeated question came 0.8 s after the event.
  Without the event, it would come within 60 s.
- **One sqlgen pod killed:** no user saw it.
- **LLM errors:** 41 users got the previous answer, marked stale, instead of an error.

Getting there took four failed runs, and each exposed something real
(docs/m3-what-broke.md):
- Argo CD reverting an experiment.
- gRPC's 20-second connect timeout.
- Kubernetes service-link env vars colliding with `SQLGEN_PORT`.
- A latency SLO that could never have fired, because its bucket label was `le="8"`, not
  `le="8.0"`.

## Consequences

- **The availability budget reads −375% after the chaos run.** On a cluster whose Prometheus
  is minutes old, one chaos run is the entire 28-day window. In production the budget would
  absorb such a run, and the burn-rate alerts are the signal to watch.
- **The stale fallback trades freshness for availability.** It's right for weekly sales
  data, where `data_as_of` says how old an answer is. It would be wrong for data people act on
  within minutes.
- **No Alertmanager.** Alerts are visible in Prometheus and Grafana but go nowhere. Routing
  them (email, Slack) is a small next step.
- **Single-node kind with one replica each of Postgres, Redis and Redpanda.** These tests
  cover the chatbot's handling of dependency failure, not the dependencies' own high
  availability.

## Alternatives considered

- **Helm:** more machinery than one environment needs. Kustomize overlays can come when a
  second environment exists.
- **Chaos Mesh / Litmus:** proper tools for a shared cluster, but heavy for a Codespace. A
  short script using `kubectl delete pod` and `set env` covers the faults that matter here,
  with pass/fail criteria tied to what users see.
- **Polling the database for new loads:** that's the existing 60 s check. Events make
  invalidation immediate without adding load on Redshift, which bills per query.
