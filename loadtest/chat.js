// k6 load test for the chatbot (milestone 1). Runs as a container in the Codespace against the
// compose stack -- the fake LLM and the Postgres copy, never Redshift or a real model.
//
//   docker compose --profile load run --rm k6 run -e LOAD=1x  /scripts/chat.js
//   docker compose --profile load run --rm k6 run -e LOAD=10x /scripts/chat.js
//   docker compose --profile load run --rm k6 run -e LOAD=1x -e CACHE=on /scripts/chat.js   (milestone 2)
//
// 1x = 2 virtual users asking back to back (a couple of analysts); 10x = 20.
// CACHE=off (default): each question gets a unique suffix so it misses the answer cache -- the full
//   route -> SQL -> query -> summarize path, the worst case (milestone 1's numbers).
// CACHE=on: 70% of questions repeat from a pool of 40, so after warm-up they hit the Redis cache;
//   the other 30% are unique and miss, so both latencies stay visible side by side. Every
//   request carries an Idempotency-Key, and 10% are sent twice, like a client retrying after a
//   timeout; the retry must come back as a replay (Idempotent-Replayed: true).
import http from "k6/http";
import { check } from "k6";
import { Trend } from "k6/metrics";

const LOAD = __ENV.LOAD || "1x";
const VUS = { "1x": 2, "10x": 20 }[LOAD];
const DURATION = __ENV.DURATION || "3m";
const BASE = __ENV.BASE_URL || "http://chatbot:8000";
const CACHE = (__ENV.CACHE || "off") === "on";
const POOL = 8;            // suffixes per question in CACHE=on: 5 x 8 = 40 distinct questions
const REPEAT_SHARE = 0.7;
const RETRY_SHARE = 0.1;

const QUESTIONS = [
  "Total sales by retailer last week",
  "Which products were out of stock last week?",
  "How did Aurora Apparel do at Summit Outfitters over the last 4 weeks?",
  "Top 5 styles by sales at Harbor Mart",
  "Week over week sales change by brand",
];

const chatLatency = new Trend("chat_latency", true);
const hitLatency = new Trend("chat_latency_cache_hit", true);
const missLatency = new Trend("chat_latency_cache_miss", true);

export const options = {
  scenarios: { [LOAD]: { executor: "constant-vus", vus: VUS, duration: DURATION } },
  thresholds: {
    http_req_failed: ["rate<0.01"],
    "http_req_duration{name:chat}": ["p(95)<15000"],
  },
  summaryTrendStats: ["avg", "med", "p(90)", "p(95)", "p(99)", "max"],
};

function ask(q, key, name) {
  const headers = { "Content-Type": "application/json" };
  if (key) headers["Idempotency-Key"] = key;
  return http.post(`${BASE}/v1/chat`, JSON.stringify({ message: q }), { headers, tags: { name }, timeout: "60s" });
}

export default function () {
  const base = QUESTIONS[Math.floor(Math.random() * QUESTIONS.length)];
  const repeat = CACHE && Math.random() < REPEAT_SHARE;
  const q = repeat ? `${base} (#${Math.floor(Math.random() * POOL)})` : `${base} (${__VU}-${__ITER})`;
  const key = CACHE ? `k6-${__VU}-${__ITER}-${Date.now()}` : null;
  const res = ask(q, key, "chat");
  chatLatency.add(res.timings.duration);
  if (res.status === 200) (res.json("cached") ? hitLatency : missLatency).add(res.timings.duration);
  check(res, { "status 200": (r) => r.status === 200, "has answer": (r) => !!(r.json("answer")) });
  if (CACHE && Math.random() < RETRY_SHARE) {
    const retry = ask(q, key, "retry");
    check(retry, {
      "retry replayed": (r) => r.headers["Idempotent-Replayed"] === "true",
      "retry same answer": (r) => JSON.stringify(r.json()) === JSON.stringify(res.json()),
    });
  }
}

export function handleSummary(data) {
  const name = CACHE ? `k6_${LOAD}_cache` : `k6_${LOAD}`;
  return { [`/scripts/results/${name}.json`]: JSON.stringify(data, null, 2) };
}
