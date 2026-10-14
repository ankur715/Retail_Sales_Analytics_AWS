# AI governance: how this project uses language models

A short, practical note. It covers what the models do, what they may see, how people stay in
control, and how we'd know they went wrong. Reviewed with each milestone; last updated 2026-10-14
(milestone 4).

## Where models are used

| Use | Model | What it decides | Who acts on it |
|---|---|---|---|
| Analytics chatbot: route, write SQL, summarize | Claude Haiku 4.5 on Bedrock (default), Gemini (option), a fake model for tests | Which views to read and the SQL to run. It never decides access: the guard does (below). | The analyst, who sees the data, the SQL and the date of the data behind every answer |
| Pipeline triage agent | Nova Lite on Bedrock (Claude Haiku optional) | A suggested diagnosis and fix for a failed load | A person. The note is a suggestion on the audit row and in the failure email; nothing acts on it automatically. |

No model makes a decision about a person, and none acts without a human reading the result.

## Principles and how they're enforced

1. **The model is never the security boundary.** Access is enforced in code on every query,
   whatever the model writes:
   - the SQL guard (one read-only SELECT, routed views only, rows confined to the caller's
     brands);
   - a read-only database user;
   - login verified on every request.

   Prompt injection can at worst produce a wrong answer from data the user may already see.
   (Threat model T2, I1.)
2. **Show the work.** Every answer carries the SQL that produced it, the views used, the data's
   "as of" date, and flags for `cached` and `stale` (a fallback served while the live path is
   down). The page says so in words when an answer is stale.
3. **Least data to the model.**
   - The model sees view descriptions, the caller's own brand names, and at most 50 result rows
     for the summary.
   - It never sees credentials or other brands' rows.
   - Real brand data should only go to Bedrock, which keeps it in our AWS account and doesn't
     train on it.
   - Gemini's free tier is for the synthetic dataset only.
4. **Bounded cost.**
   - Answers are cached.
   - Redshift has a hard daily RPU cap.
   - The IAM policy allows only two inexpensive Bedrock models.
   - Load and chaos tests use the fake model, never a real one.
   - Real-model runs (demos, the evaluation) are capped per session, and their token cost is
     printed afterwards.
5. **No personal data in telemetry.** Traces and metrics carry step names, timings, view names
   and row counts. Never the question text, answers or rows.

## Known limitations (what users should be told)

- Answers can be wrong in plausible ways: a misread question, the wrong week, a wrong
  aggregate. The SQL is shown so an analyst can check it, and important numbers should be
  checked.
- The data is weekly and can be up to a week old; every answer shows its "as of" date.
- The model can't answer questions the curated views don't cover, and it is told to say so
  rather than guess.

## How we'd know it went wrong

| Signal | Where |
|---|---|
| Errors, latency, fallbacks (stale answers) | Grafana SLO dashboard and burn-rate alerts (milestone 3) |
| Answer quality | ~50-question evaluation set with LLM-as-judge scoring, run before model or prompt changes (milestone 5) |
| Prompt-injection resistance | Injection test suite against the guard (milestone 5) |
| Cost per request | Per-request cost in Grafana (milestone 5) |

## Changes and incidents

- **Changes:** a model, prompt or provider change goes through a PR like any code change. It
  must pass the evaluation set (milestone 5) and be noted in an ADR.
- **Incidents:** wrong data shown to the wrong brand, a leaked credential, or runaway cost.
  1. Turn the chatbot off (`LLM_PROVIDER=none`, or scale it to zero).
  2. Rotate the affected secrets.
  3. Write a "what broke" note like the milestone notes in `docs/`.
