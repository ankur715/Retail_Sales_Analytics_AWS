# ADR 0005: Retrieval, cost accounting, an injection suite and an evaluation for the chatbot

- Status: accepted
- Date: 2026-10-14
- Milestone: 5 (AI system)

## Context

The chatbot worked, but we couldn't say how *well*, what each question cost, or whether a
manipulated model could get past the guard. The model saw every catalog example for a view
whether relevant or not, and knew nothing about past pipeline problems.

## Decisions

**1. Every model call reports tokens and cost.**
- **Recording:** the providers record the counts they're given (Bedrock, Gemini, Ollama); the
  fake model estimates them from the real prompt text. Calls are labeled by step and priced
  from a reviewed `config/llm_prices.yaml`.
- **Unknown models:** counted at cost 0 and logged, never guessed.
- **Across services:** SQL generation runs in the sqlgen service, so its gRPC reply carries its
  usage back.
- **Where it shows:** each response includes its own `usage`, and Grafana gets cost per
  question, spend by step, and tokens per step.
- **Load tests:** the fake model is priced as Haiku 4.5, so a load test shows what the same
  prompts would cost on the real model.

**2. Retrieval with pgvector** (`rag/`):
- **What's indexed:** 30 example questions with SQL (a reviewed bank plus the catalog's), view
  docs, business rules, and past triage notes.
- **Examples:** only those whose SQL uses just the routed views are offered, so retrieved
  examples always pass the guard. They replace the fixed per-view examples in the SQL prompt.
- **Triage notes:** tagged with their brand and filtered to the caller's brands (they name
  pipelines). Only offered for questions about a problem; the eval showed why (see Results).
- **Embedders:** `hash` (no dependencies, used by CI) or `fastembed` (bge-small on CPU, ~130 MB,
  Codespace only). The index records which embedder built it and refuses a mismatch.
- **Failure:** if retrieval fails, the chatbot falls back to the catalog's examples; nothing
  fails.
- **Where it lives:** Postgres. Redshift has no vector type, so production would use a managed
  Postgres (Neon / Supabase / RDS).

**3. The prompt-injection suite assumes the attacker wins the prompt.** It plays a fully
compromised model and checks that the guard holds. Writing it found a real gap: functions
weren't checked (`pg_read_file`, `pg_sleep`, `current_user`, `version()`). The guard now
refuses functions sqlglot doesn't recognise, plus those that reveal the session or server.
**46/46 attacks held**: 38 refused, and 8 cross-brand attempts confined or answered with a 403.
End-to-end tests confirm a compromised model never gets a query to the database.

**4. A 50-question evaluation** (`evals/`). Questions are spread across totals, trends,
products, stock, named periods, freshness, comparisons, refusals, and adversarial (injection
and cross-brand). They are distinct from the retrieval examples, and a test checks they don't
overlap. Each answerable question has reference SQL; CI runs all 40 on real Postgres. Scoring:

| Check | How |
|---|---|
| Routing | Did the route include the views needed? |
| Execution accuracy | Does the result contain the reference result (row count, values within 0.5%)? |
| LLM judge | Haiku grades the written answer against the reference result |
| Refusals | No data returned |
| Cross-brand | A 403, a decline, or rows confined to the caller's brand |

A hard budget cap stops the run before it exceeds the configured calls or dollars.

## Results (2026-10-14, Claude Haiku 4.5 on Bedrock for chat and judge; Postgres copy; RAG with the hash embedder)

| Run | Pass | Result matches | Judge pass | Routed | Calls | Cost |
|---|---|---|---|---|---|---|
| 1 (as first scored) | 39/50 (78%) | 82% | 84% | 98% | 182 | $0.187 |
| 1, with the corrected cross-brand rule | 42/50 (84%) | 88% | 84% | 98% | (same) | (same) |
| 2, after the two changes below | **43/50 (86%)** | 88% | 86% | 98% | 182 | $0.192 |

- **Cost:** about **$0.004 per question**, including the judge call (route + write_sql +
  summarize + judge). Separately, the load test priced the live chatbot's prompts (no judge) at
  a median of $0.0015 per question at Haiku rates.
- **Latency:** a median of 4.7 s per question.
- **Perfect categories:** calendar, refusals and adversarial, 100% each.
- **What changed between runs:**
  - Triage notes now come only with problem questions. The first run showed Haiku repeating
    unrelated notes as facts in plain "when was it loaded" answers; fresh-03 was fixed.
  - Two business rules added for the remaining misses ("a pair's own latest week", "search
    product names with LIKE") **did not fix them**. With 50 questions, one question is 2 points.
- **Failures left, all 7 read individually:**
  - stock-02: "latest" read as the global latest week, for a pair that hadn't reported it, so
    "no data".
  - trend-03: after the new rule, the sales figures and week-over-week change are now correct,
    but the answer states the wrong week-ending date.
  - prod-05: said there's no product-level detail for "Daypack".
  - tot-07: averaged rows instead of weekly totals.
  - cmp-03: reported a difference with the wrong sign.
  - Two arguably right, left as written because changing references after seeing results would
    move the goalposts:
    - prod-04 named the style by its code `S01`, with the correct amount, where the reference
      used the name.
    - fresh-01 said Cobalt's data is "3 weeks behind", which is true against the latest week
      anywhere in the data. The reference only lists Cobalt's own dates, so the question is
      ambiguous.

## Consequences

- **The eval is the gate for prompt and model changes now.** A change ships if it doesn't lower
  the pass rate and fixes something specific. 50 questions is small: a held-out set should be
  added before tuning prompts against these.
- **Judge bias:** the judge is the same model it grades. The rubric gives it the reference
  result, and execution accuracy is computed independently, so the judge can't inflate a
  wrong number. A different-family judge (e.g. Nova) would reduce self-preference further.
- **Retrieval used the hash embedder in the scored runs,** because the real-model run went from
  the laptop, which can't load the embedding model. A fastembed run from the Codespace (with
  AWS credentials as Codespaces secrets) is the natural comparison.
- **Next things to try, judged on a held-out set:** few-shot examples for "a pair's latest week"
  and per-brand product names as known values.
- **Ollama:** the provider is implemented and tested with a mocked API, but wasn't run. A model
  small enough for the Codespace's CPU would be slow over 50 questions; it's the natural
  no-cost comparison.

## Alternatives considered

- **Exact-match accuracy on SQL text:** many correct queries differ in text. Execution accuracy
  plus a judge measures what users see.
- **Judge only:** a judge can be talked into accepting plausible wrong numbers. Execution
  accuracy keeps it honest.
- **Retrieval of whole view docs only:** helps routing, which was already 98%. Example
  retrieval targets SQL quality, where the errors are.
- **A hosted vector DB (OpenSearch, Pinecone):** more to pay for and secure. pgvector next to
  the data is enough for a few thousand chunks.
