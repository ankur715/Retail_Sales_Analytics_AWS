const examples = [
  "Total sales by retailer last week",
  "How did Aurora Apparel do at Summit Outfitters over the last 4 weeks vs the 4 weeks before?",
  "Top 5 products by sales for Cobalt Outdoor in the last 4 weeks",
  "Which products are out of stock this week?",
  "How current is the data for Harbor Mart?",
];
const log = document.getElementById("log"), q = document.getElementById("q"), send = document.getElementById("send");

// --- Sign-in (milestone 4): OIDC authorization code + PKCE with Keycloak, as the public client
// chatbot-web. No client secret exists in the browser; the server verifies every access token.
// The token lives in this tab's sessionStorage (cleared when the tab closes) and is sent only
// to this server. With AUTH_MODE=off the page works as before.
const auth = {cfg: {auth: "off"}, token: sessionStorage.getItem("access_token")};
const b64url = bytes => btoa(String.fromCharCode(...new Uint8Array(bytes)))
  .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const randomString = () => b64url(crypto.getRandomValues(new Uint8Array(32)));
const claims = t => JSON.parse(atob(t.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
const oidc = path => `${auth.cfg.issuer}/protocol/openid-connect/${path}`;
const home = () => location.origin + "/";

async function login() {
  const verifier = randomString(), state = randomString();
  sessionStorage.setItem("pkce_verifier", verifier);
  sessionStorage.setItem("pkce_state", state);
  const challenge = b64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
  location.assign(oidc("auth") + "?" + new URLSearchParams({
    response_type: "code", client_id: auth.cfg.client_id, redirect_uri: home(), scope: "openid",
    state, code_challenge: challenge, code_challenge_method: "S256"}));
}

function logout() {
  const idToken = sessionStorage.getItem("id_token");
  sessionStorage.clear();
  location.assign(oidc("logout") + "?" + new URLSearchParams({
    client_id: auth.cfg.client_id, post_logout_redirect_uri: home(), ...(idToken ? {id_token_hint: idToken} : {})}));
}

async function initAuth() {
  auth.cfg = await (await fetch("/v1/auth-config")).json();
  if (auth.cfg.auth !== "oidc") return;
  const params = new URLSearchParams(location.search);
  if (params.has("code")) {                       // back from Keycloak: swap the code for tokens
    history.replaceState({}, "", "/");
    if (params.get("state") !== sessionStorage.getItem("pkce_state")) throw new Error("login state mismatch");
    const r = await fetch(oidc("token"), {method: "POST", body: new URLSearchParams({
      grant_type: "authorization_code", code: params.get("code"), redirect_uri: home(),
      client_id: auth.cfg.client_id, code_verifier: sessionStorage.getItem("pkce_verifier")})});
    if (!r.ok) throw new Error("login failed");
    const t = await r.json();
    sessionStorage.removeItem("pkce_verifier"); sessionStorage.removeItem("pkce_state");
    sessionStorage.setItem("access_token", auth.token = t.access_token);
    if (t.id_token) sessionStorage.setItem("id_token", t.id_token);
  }
  if (auth.token && claims(auth.token).exp * 1000 < Date.now() + 10000) {
    auth.token = null; sessionStorage.removeItem("access_token");
  }
  if (!auth.token) return login();
  const me = await (await fetch("/v1/me", {headers: {Authorization: `Bearer ${auth.token}`}})).json();
  document.getElementById("whoami").textContent =
    `Signed in as ${me.username} · ${me.brands ? me.brands.join(", ") : "all brands"}`;
  document.getElementById("who").classList.add("on");
  document.getElementById("logout").onclick = logout;
}

initAuth().catch(e => log.append(el("div", "msg err", "Sign-in failed: " + e.message)));

for (const text of examples) {
  const b = document.createElement("button");
  b.className = "chip"; b.type = "button"; b.textContent = text;
  b.onclick = () => ask(text);
  document.getElementById("chips").append(b);
}

fetch("/v1/health").then(r => r.json()).then(h => {
  document.getElementById("status").textContent =
    `${h.env} · ${h.database} · ${h.model}${h.llm_configured ? "" : " · GOOGLE_API_KEY missing"}`;
}).catch(() => {});

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function table(columns, rows) {
  const wrap = el("div", "table-wrap"), t = el("table"), head = el("tr");
  columns.forEach(c => head.append(el("th", "", c)));
  t.append(head);
  rows.forEach(r => {
    const tr = el("tr");
    r.forEach(v => {
      const isNum = typeof v === "number";
      tr.append(el("td", isNum ? "num" : "", v === null ? "" :
        isNum ? v.toLocaleString(undefined, {maximumFractionDigits: 2}) : String(v)));
    });
    t.append(tr);
  });
  wrap.append(t);
  return wrap;
}

async function ask(text) {
  text = text.trim();
  if (!text) return;
  log.append(el("div", "msg user", text));
  const reply = el("div", "msg", "Thinking…");
  log.append(reply);
  q.value = ""; send.disabled = true;
  reply.scrollIntoView({behavior: "smooth", block: "end"});
  try {
    const headers = {"Content-Type": "application/json"};
    if (auth.token) headers.Authorization = `Bearer ${auth.token}`;
    const res = await fetch("/v1/chat", {method: "POST", headers,
                                           body: JSON.stringify({message: text})});
    const d = await res.json();
    reply.textContent = "";
    if (res.status === 401 && auth.cfg.auth === "oidc") { reply.append(el("div", "err", d.detail)); return login(); }
    if (!res.ok) { reply.append(el("div", "err", d.detail || `Error ${res.status}`)); return; }
    if (d.stale) reply.append(el("div", "err", "Live data is unavailable right now, so this is the last answer we had. Check the date below."));
    reply.append(el("div", d.error ? "err" : "", d.answer));
    if (d.rows && d.rows.length) {
      const det = el("details"); det.open = d.rows.length <= 12;
      det.append(el("summary", "", `Data (${d.row_count} row${d.row_count === 1 ? "" : "s"})`), table(d.columns, d.rows));
      reply.append(det);
    }
    if (d.sql) {
      const det = el("details");
      det.append(el("summary", "", "SQL"), el("pre", "", d.sql + (d.error ? `\n\n-- error: ${d.error}` : "")));
      reply.append(det);
    }
    const bits = [];
    if (d.views && d.views.length) bits.push(`views: ${d.views.join(", ")}`);
    if (d.data_as_of) bits.push(`data through week ending ${d.data_as_of}`);
    if (d.cached) bits.push(d.stale ? "stale fallback" : "cached");
    if (bits.length) reply.append(el("div", "meta", bits.join(" · ")));
  } catch (e) {
    reply.textContent = ""; reply.append(el("div", "err", "Request failed: " + e));
  } finally {
    send.disabled = false; q.focus();
  }
}

document.getElementById("form").onsubmit = e => { e.preventDefault(); ask(q.value); };
