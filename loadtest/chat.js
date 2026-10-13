// k6 load test for the chatbot (milestone 1). Runs as a container in the Codespace against the
// compose stack -- the fake LLM and the Postgres copy, never Redshift or a real model.
//
//   docker compose --profile load run --rm k6 run -e LOAD=1x  /scripts/chat.js
//   docker compose --profile load run --rm k6 run -e LOAD=10x /scripts/chat.js
//
// 1x = 2 virtual users asking back to back (a couple of analysts); 10x = 20. Each question gets a
// unique suffix so it misses the answer cache: this measures the full route -> SQL -> query ->
// summarize path, the worst case.
import http from "k6/http";
import { check } from "k6";
import { Trend } from "k6/metrics";

const LOAD = __ENV.LOAD || "1x";
const VUS = { "1x": 2, "10x": 20 }[LOAD];
const DURATION = __ENV.DURATION || "3m";
const BASE = __ENV.BASE_URL || "http://chatbot:8000";

const QUESTIONS = [
  "Total sales by retailer last week",
  "Which products were out of stock last week?",
  "How did Aurora Apparel do at Summit Outfitters over the last 4 weeks?",
  "Top 5 styles by sales at Harbor Mart",
  "Week over week sales change by brand",
];

const chatLatency = new Trend("chat_latency", true);

export const options = {
  scenarios: { [LOAD]: { executor: "constant-vus", vus: VUS, duration: DURATION } },
  thresholds: {
    http_req_failed: ["rate<0.01"],
    "http_req_duration{name:chat}": ["p(95)<15000"],
  },
  summaryTrendStats: ["avg", "med", "p(90)", "p(95)", "p(99)", "max"],
};

export default function () {
  const q = `${QUESTIONS[Math.floor(Math.random() * QUESTIONS.length)]} (${__VU}-${__ITER})`;
  const res = http.post(`${BASE}/api/chat`, JSON.stringify({ message: q }),
    { headers: { "Content-Type": "application/json" }, tags: { name: "chat" }, timeout: "60s" });
  chatLatency.add(res.timings.duration);
  check(res, { "status 200": (r) => r.status === 200, "has answer": (r) => !!(r.json("answer")) });
}

export function handleSummary(data) {
  return { [`/scripts/results/k6_${LOAD}.json`]: JSON.stringify(data, null, 2) };
}
