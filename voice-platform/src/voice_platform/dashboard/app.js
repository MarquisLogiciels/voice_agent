// Voice Platform admin dashboard. Dependency-free ES module; every action calls the REST API.
// All dynamic text goes through esc() or textContent (no unescaped HTML injection).

const state = { token: sessionStorage.getItem("vp_token"), me: null, tenant: null };
const app = document.getElementById("app");

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = {
  date: (s) => (s ? new Date(s).toLocaleString() : "—"),
  pct: (v) => (v == null ? "—" : `${(v * 100).toFixed(0)}%`),
  sec: (v) => (v == null ? "—" : `${Number(v).toFixed(2)}s`),
  usd: (v) => (v == null ? "—" : `$${Number(v).toFixed(4)}`),
};
const badge = (text, kind) => `<span class="badge ${kind || ""}">${esc(text)}</span>`;
const statusBadge = (s) => badge(s, ["succeeded", "completed", "ready", "published", "ok",
  "eligible", "approved", "transferred"].includes(s) ? "ok" : ["failed", "rejected", "dead",
  "busy", "no_answer"].includes(s) ? "bad" : "warn");

class ApiError extends Error {
  constructor(status, body) {
    super(body?.error?.message || `HTTP ${status}`);
    this.status = status; this.body = body;
  }
}

async function api(path, { method = "GET", body, form } = {}) {
  const headers = {};
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  let payload;
  if (form) payload = form;
  else if (body !== undefined) { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
  const res = await fetch(path, { method, headers, body: payload });
  if (res.status === 401 && path !== "/v1/auth/login") { logout(); throw new ApiError(401, {}); }
  if (res.status === 204) return null;
  const data = await res.json().catch(() => null);
  if (!res.ok) throw new ApiError(res.status, data);
  return data;
}
const T = (p) => `/v1/tenants/${state.tenant}${p}`;

function errorText(e) {
  const details = e.body?.error?.details;
  const extra = details?.errors ? "\n" + details.errors.map((x) => `• ${(x.loc || []).join(".")}: ${x.msg}`).join("\n")
    : details && Object.keys(details).length ? "\n" + JSON.stringify(details, null, 2) : "";
  return `${e.message}${extra}`;
}

// Runs an async action bound to a button, with disabled state and inline error/notice output.
async function act(btn, fn, okMsg) {
  const out = btn.closest(".panel")?.querySelector(".out") || btn.parentElement;
  btn.disabled = true;
  out.querySelectorAll?.(".error,.notice").forEach((n) => n.remove());
  try {
    const result = await fn();
    if (okMsg) out.insertAdjacentHTML("beforeend", `<div class="notice">${esc(typeof okMsg === "function" ? okMsg(result) : okMsg)}</div>`);
    return result;
  } catch (e) {
    out.insertAdjacentHTML("beforeend", `<div class="error">${esc(errorText(e))}</div>`);
  } finally { btn.disabled = false; }
}

function logout() { sessionStorage.removeItem("vp_token"); state.token = null; state.me = null; render(); }

// ------------------------------------------------------------------------------------------
// Shell & routing
// ------------------------------------------------------------------------------------------
const ROUTES = [
  ["overview", "Overview"], ["agents", "Agents"], ["conversations", "Conversations"],
  ["knowledge", "Knowledge"], ["integrations", "Integrations"], ["numbers", "Phone numbers"],
  ["evaluations", "Evaluations"], ["optimizations", "Optimizations"], ["settings", "Settings"],
];

async function render() {
  if (!state.token) return renderLogin();
  if (!state.me) {
    try { state.me = await api("/v1/me"); } catch { return; }
    state.tenant = sessionStorage.getItem("vp_tenant") || state.me.tenants[0]?.id || null;
  }
  const [route, ...args] = (location.hash.slice(2) || "overview").split("/");
  const tenantOpts = state.me.tenants.map((t) =>
    `<option value="${esc(t.id)}" ${t.id === state.tenant ? "selected" : ""}>${esc(t.name)} (${esc(t.role)})</option>`).join("");
  app.innerHTML = `<div class="layout"><nav class="side"><h1>Voice Platform</h1>
    <select id="tenant">${tenantOpts || "<option>No tenants</option>"}</select>
    ${ROUTES.map(([r, label]) => `<a href="#/${r}" class="${r === route ? "active" : ""}">${label}</a>`).join("")}
    <p class="muted mt-20">${esc(state.me.kind)}${state.me.is_platform_admin ? " · platform admin" : ""}</p>
    <button class="secondary" id="logout">Sign out</button></nav><main id="main"><div class="muted">Loading…</div></main></div>`;
  document.getElementById("logout").onclick = logout;
  document.getElementById("tenant").onchange = (e) => { state.tenant = e.target.value; sessionStorage.setItem("vp_tenant", state.tenant); render(); };
  const main = document.getElementById("main");
  if (!state.tenant) {
    main.innerHTML = state.me.is_platform_admin ? await platformAdminView() : `<div class="empty">You are not a member of any tenant.</div>`;
    bindPlatformAdmin(main);
    return;
  }
  try {
    const view = VIEWS[route] || VIEWS.overview;
    await view(main, ...args);
  } catch (e) {
    main.innerHTML = `<div class="error">${esc(errorText(e))}</div>`;
  }
}
window.addEventListener("hashchange", render);

function renderLogin() {
  app.innerHTML = `<div class="login panel"><h2>Sign in</h2>
    <form id="login"><input name="email" type="email" placeholder="Email" required autocomplete="username">
    <input name="password" type="password" placeholder="Password" required autocomplete="current-password">
    <button type="submit">Sign in</button><div class="out"></div></form></div>`;
  const form = document.getElementById("login");
  form.onsubmit = async (ev) => {
    ev.preventDefault();
    const btn = form.querySelector("button");
    const data = Object.fromEntries(new FormData(form));
    const res = await act(btn, () => api("/v1/auth/login", { method: "POST", body: data }));
    if (res) { state.token = res.access_token; sessionStorage.setItem("vp_token", res.access_token); render(); }
  };
}

async function platformAdminView() {
  const tenants = await api("/v1/tenants");
  return `<h2>Tenants</h2><div class="panel"><div class="row">
    <label>Slug<input id="t-slug" placeholder="acme-dental"></label><label>Name<input id="t-name"></label>
    <label>Owner email<input id="t-email" type="email"></label><label>Owner password<input id="t-pass" type="password"></label>
    <button id="t-create">Create tenant</button></div><div class="out"></div></div>
    <div class="panel">${table(["Name", "Slug", "Status", "Created"], tenants.map((t) => [esc(t.name), esc(t.slug), statusBadge(t.status), fmt.date(t.created_at)]))}</div>`;
}
function bindPlatformAdmin(main) {
  const b = main.querySelector("#t-create");
  if (!b) return;
  b.onclick = () => act(b, async () => {
    const body = { slug: val("#t-slug"), name: val("#t-name") };
    if (val("#t-email")) Object.assign(body, { owner_email: val("#t-email"), owner_password: val("#t-pass") });
    await api("/v1/tenants", { method: "POST", body });
    state.me = null; await render();
  }, "Tenant created");
}

const val = (sel) => document.querySelector(sel)?.value?.trim() ?? "";
function table(headers, rows, { onClick } = {}) {
  if (!rows.length) return `<div class="empty">Nothing here yet.</div>`;
  return `<table><thead><tr>${headers.map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead><tbody>
    ${rows.map((r, i) => `<tr ${onClick ? `class="clickable" data-i="${i}"` : ""}>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
}
function bindRows(root, onClick) {
  root.querySelectorAll("tr.clickable").forEach((tr) => (tr.onclick = () => onClick(Number(tr.dataset.i))));
}

// ------------------------------------------------------------------------------------------
// Views
// ------------------------------------------------------------------------------------------
const VIEWS = {};

VIEWS.overview = async (main) => {
  const o = await api(T("/analytics/overview?days=30"));
  const stat = (label, value) => `<div class="stat"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div></div>`;
  const tools = Object.entries(o.tools || {}).map(([name, s]) => {
    const total = Object.values(s).reduce((a, b) => a + b, 0);
    return [esc(name), String(total), fmt.pct((s.succeeded || 0) / (total || 1))];
  });
  main.innerHTML = `<h2>Overview (last 30 days)</h2><div class="grid">
    ${stat("Calls", o.calls)}${stat("Transfer rate", fmt.pct(o.transfer_rate))}
    ${stat("Median call", fmt.sec(o.duration_s.p50))}
    ${stat("Response latency p50", fmt.sec(o.response_latency_p50_s.p50))}
    ${stat("Response latency p95", fmt.sec(o.response_latency_p95_s.p95))}
    ${stat("Est. cost / call", fmt.usd(o.estimated_cost_usd.per_call))}</div>
    <h3>Calls by status</h3><div class="panel">${table(["Status", "Calls"], Object.entries(o.by_status).map(([k, v]) => [statusBadge(k), String(v)]))}</div>
    <h3>Tool success</h3><div class="panel">${table(["Tool", "Executions", "Success"], tools)}</div>
    <p class="muted">Costs are estimates from approximate list prices, not billing data.</p>`;
};

const CONFIG_TEMPLATE = {
  name: "Front desk", language: "en", instructions: "Help callers book appointments and answer questions.",
  business: { name: "My Business", timezone: "America/Toronto",
    hours: { schedule: { mon: ["09:00-17:00"], tue: ["09:00-17:00"], wed: ["09:00-17:00"], thu: ["09:00-17:00"], fri: ["09:00-17:00"] } } },
  greeting: { mode: "say", text: "Hi, thanks for calling! How can I help?" },
  pipeline: { mode: "cascaded", stt: { provider: "deepgram", model: "nova-3" },
    llm: { provider: "openai", model: "gpt-4.1-mini", temperature: 0.3 },
    tts: { provider: "cartesia", model: "sonic-2", voice: "REPLACE_WITH_VOICE_ID" },
    vad: { provider: "silero" }, turn_detection: { mode: "model", provider: "livekit", model: "multilingual" } },
};

VIEWS.agents = async (main, agentId) => {
  if (agentId) return agentDetail(main, agentId);
  const agents = await api(T("/agents"));
  main.innerHTML = `<h2>Agents</h2><div class="panel">${table(["Name", "Slug", "Deployments", "Created"], agents.map((a) => [
    esc(a.name), esc(a.slug), a.deployments.map((d) => badge(`${d.environment}${d.canary_version_id ? ` +canary ${d.canary_percent}%` : ""}`)).join(" ") || "—", fmt.date(a.created_at)]), { onClick: true })}</div>
    <div class="panel"><h3>New agent</h3><div class="row"><label>Slug<input id="a-slug" placeholder="front-desk"></label><label>Name<input id="a-name" placeholder="Front desk"></label></div>
    <textarea id="a-config">${esc(JSON.stringify(CONFIG_TEMPLATE, null, 2))}</textarea>
    <div class="row"><button class="secondary" id="a-validate">Validate</button><button id="a-create">Create agent</button></div><div class="out"></div></div>`;
  bindRows(main, (i) => (location.hash = `#/agents/${agents[i].id}`));
  const cfg = () => JSON.parse(val("#a-config"));
  main.querySelector("#a-validate").onclick = (e) => act(e.target, () => api(T("/agents/validate"), { method: "POST", body: { config: cfg() } }),
    (r) => r.valid ? `Valid${r.problems.length ? " with warnings:\n" + r.problems.map((p) => `${p.path}: ${p.message}`).join("\n") : ""}` : "Invalid:\n" + r.problems.map((p) => `${p.path}: ${p.message}`).join("\n"));
  main.querySelector("#a-create").onclick = (e) => act(e.target, async () => {
    const a = await api(T("/agents"), { method: "POST", body: { slug: val("#a-slug"), name: val("#a-name"), config: cfg() } });
    location.hash = `#/agents/${a.id}`;
  });
};

async function agentDetail(main, agentId) {
  const a = await api(T(`/agents/${agentId}`));
  const latest = a.versions[0];
  const latestFull = latest ? await api(T(`/versions/${latest.id}`)) : null;
  const deps = Object.fromEntries(a.deployments.map((d) => [d.environment, d]));
  const vlabel = (id) => { const v = a.versions.find((x) => x.id === id); return v ? `v${v.version}` : "—"; };
  const versionOpts = a.versions.map((v) => `<option value="${v.id}">v${v.version} · ${v.status}</option>`).join("");
  main.innerHTML = `<h2>${esc(a.name)} <span class="muted">${esc(a.slug)}</span></h2>
    <div class="panel"><h3>Deployments</h3>${table(["Environment", "Stable", "Canary", "Updated"], ["dev", "staging", "prod"].map((env) => {
      const d = deps[env];
      return [esc(env), d ? vlabel(d.version_id) : "—", d?.canary_version_id ? `${vlabel(d.canary_version_id)} @ ${d.canary_percent}%` : "—", d ? fmt.date(d.updated_at) : "—"];
    }))}
    <div class="row"><label>Environment<select id="d-env"><option>dev</option><option>staging</option><option>prod</option></select></label>
      <label>Version<select id="d-version">${versionOpts}</select></label><label>Canary %<input id="d-pct" type="number" min="0" max="100" value="10" class="w-90"></label>
      <button id="d-deploy">Deploy</button><button class="secondary" id="d-canary">Set canary</button><button class="secondary" id="d-promote">Promote canary</button><button class="danger" id="d-rollback">Roll back</button></div><div class="out"></div></div>
    <div class="panel"><h3>Versions</h3>${table(["Version", "Status", "Source", "Notes", "Created"], a.versions.map((v) => [
      `v${v.version}`, statusBadge(v.status), esc(v.source), esc(v.notes.slice(0, 80)), fmt.date(v.created_at)]))}
    <div class="row"><label>Diff from<select id="v-from">${versionOpts}</select></label><label>to<select id="v-to">${versionOpts}</select></label><button class="secondary" id="v-diff">Compare</button>
      <button class="secondary" id="v-publish">Publish selected "to" version</button></div><div class="out"></div><div id="v-diff-out"></div></div>
    <div class="panel"><h3>Edit configuration → new draft (from v${latest?.version ?? "?"})</h3><textarea id="v-config">${esc(JSON.stringify(latestFull?.config ?? {}, null, 2))}</textarea>
    <div class="row"><input id="v-notes" placeholder="Change notes" class="grow"><button class="secondary" id="v-validate">Validate</button><button id="v-save">Save as new draft</button></div><div class="out"></div></div>`;
  const body = (extra) => ({ version_id: val("#d-version"), ...extra });
  const env = () => val("#d-env");
  const refresh = () => agentDetail(main, agentId);
  main.querySelector("#d-deploy").onclick = (e) => act(e.target, async () => { await api(T(`/agents/${agentId}/deployments/${env()}`), { method: "PUT", body: body() }); refresh(); });
  main.querySelector("#d-canary").onclick = (e) => act(e.target, async () => { await api(T(`/agents/${agentId}/deployments/${env()}/canary`), { method: "PUT", body: body({ percent: Number(val("#d-pct")) }) }); refresh(); });
  main.querySelector("#d-promote").onclick = (e) => act(e.target, async () => { await api(T(`/agents/${agentId}/deployments/${env()}/promote`), { method: "POST" }); refresh(); });
  main.querySelector("#d-rollback").onclick = (e) => { if (confirm(`Roll back ${env()} to the previous version?`)) act(e.target, async () => { await api(T(`/agents/${agentId}/deployments/${env()}/rollback`), { method: "POST", body: {} }); refresh(); }); };
  main.querySelector("#v-diff").onclick = (e) => act(e.target, async () => {
    const d = await api(T(`/versions/${val("#v-from")}/diff/${val("#v-to")}`));
    document.getElementById("v-diff-out").innerHTML = d.length ? table(["Path", "Change", "Old", "New"], d.map((c) => [esc(c.path), esc(c.op), `<pre>${esc(JSON.stringify(c.old, null, 1))}</pre>`, `<pre>${esc(JSON.stringify(c.new, null, 1))}</pre>`])) : `<div class="empty">No differences.</div>`;
  });
  main.querySelector("#v-publish").onclick = (e) => act(e.target, async () => { await api(T(`/versions/${val("#v-to")}/publish`), { method: "POST" }); refresh(); });
  main.querySelector("#v-validate").onclick = (e) => act(e.target, () => api(T("/agents/validate"), { method: "POST", body: { config: JSON.parse(val("#v-config")) } }),
    (r) => (r.valid ? "Valid" : "Invalid") + (r.problems.length ? ":\n" + r.problems.map((p) => `${p.severity} ${p.path}: ${p.message}`).join("\n") : ""));
  main.querySelector("#v-save").onclick = (e) => act(e.target, async () => {
    await api(T(`/agents/${agentId}/versions`), { method: "POST", body: { config: JSON.parse(val("#v-config")), notes: val("#v-notes"), parent_version_id: latest?.id } });
    refresh();
  });
}

VIEWS.conversations = async (main, convId) => {
  if (convId) {
    const c = await api(T(`/conversations/${convId}`));
    const turns = c.events.map((e) => e.type === "transcript" ? `<div class="turn ${esc(e.role)}">${esc(e.text)}${e.interrupted ? " <span class='muted'>(interrupted)</span>" : ""}</div>`
      : e.type === "tool" ? `<div class="turn tool">⚙ ${esc(e.tool)} → ${e.ok ? "ok" : esc(e.error)}${e.replayed ? " (replayed)" : ""}</div>`
      : e.type.startsWith("transfer") ? `<div class="turn tool">☎ transfer ${esc(e.destination)}: ${esc(e.status)}</div>` : "").join("");
    const lat = c.metrics?.latency?.e2e || {};
    main.innerHTML = `<h2>Conversation ${statusBadge(c.status)}</h2><div class="grid">
      <div class="stat"><div class="label">Direction</div><div class="value">${esc(c.direction)}</div></div>
      <div class="stat"><div class="label">Duration</div><div class="value">${fmt.sec(c.duration_s)}</div></div>
      <div class="stat"><div class="label">Latency p50 / p95</div><div class="value">${fmt.sec(lat.p50)} / ${fmt.sec(lat.p95)}</div></div>
      <div class="stat"><div class="label">Est. cost</div><div class="value">${fmt.usd(c.cost?.total_usd)}</div></div></div>
      ${c.summary ? `<div class="panel"><h3>Summary</h3>${esc(c.summary)}</div>` : ""}
      <div class="panel"><h3>Transcript</h3>${turns || '<div class="empty">No transcript events.</div>'}</div>
      ${c.recording_status === "completed" ? `<div class="panel"><button id="rec">Get recording link</button><div class="out"></div></div>` : ""}
      <div class="panel"><h3>Details</h3><pre>${esc(JSON.stringify({ end_reason: c.end_reason, transfer: c.transfer, metrics: c.metrics, cost: c.cost, meta: c.meta }, null, 2))}</pre></div>`;
    const rec = main.querySelector("#rec");
    if (rec) rec.onclick = (e) => act(e.target, () => api(T(`/conversations/${convId}/recording`)), (r) => `Link (valid 5 min): ${r.url}`);
    return;
  }
  const convs = await api(T("/conversations?limit=100"));
  main.innerHTML = `<h2>Conversations</h2><div class="panel">${table(["Started", "Direction", "Status", "Caller", "Duration", "Latency p50", "Cost"], convs.map((c) => [
    fmt.date(c.started_at), esc(c.direction), statusBadge(c.status), esc(c.caller_number ? "•••" + c.caller_number.slice(-4) : "—"), fmt.sec(c.duration_s),
    fmt.sec(c.metrics?.latency?.e2e?.p50), fmt.usd(c.cost?.total_usd)]), { onClick: true })}</div>`;
  bindRows(main, (i) => (location.hash = `#/conversations/${convs[i].id}`));
};

VIEWS.knowledge = async (main, kbId) => {
  const kbs = await api(T("/knowledge-bases"));
  const current = kbId || kbs[0]?.id;
  const docs = current ? await api(T(`/knowledge-bases/${current}/documents`)) : [];
  main.innerHTML = `<h2>Knowledge</h2><div class="panel"><div class="row"><label>Knowledge base<select id="kb-sel">${kbs.map((k) => `<option value="${k.id}" ${k.id === current ? "selected" : ""}>${esc(k.name)}</option>`).join("")}</select></label>
    <label>New KB name<input id="kb-name"></label><label>Embeddings<select id="kb-emb"><option value="openai">OpenAI (semantic)</option><option value="hashing">Offline lexical (dev)</option></select></label><button class="secondary" id="kb-create">Create</button></div><div class="out"></div></div>
    ${current ? `<div class="panel"><h3>Documents</h3>${table(["Title", "Type", "Status", "Chunks", "Updated", ""], docs.map((d) => [esc(d.title), esc(d.mime_type.split("/").pop()), statusBadge(d.status) + (d.error ? ` <span class="muted">${esc(d.error)}</span>` : ""), String(d.chunk_count), fmt.date(d.updated_at), `<button class="danger" data-del="${d.id}">Delete</button>`]))}
    <div class="row"><input type="file" id="kb-file" accept=".pdf,.docx,.txt,.md,.html,.csv,.json"><button id="kb-upload">Upload</button>
    <input id="kb-url" placeholder="https://example.com/faq" class="grow"><button class="secondary" id="kb-addurl">Add web page</button></div><div class="out"></div></div>
    <div class="panel"><h3>Test retrieval</h3><div class="row"><input id="kb-q" placeholder="What are your hours?" class="grow"><button id="kb-search">Search</button></div><div class="out"></div><div id="kb-results"></div></div>` : `<div class="empty">Create a knowledge base to upload documents.</div>`}`;
  main.querySelector("#kb-sel")?.addEventListener("change", (e) => (location.hash = `#/knowledge/${e.target.value}`));
  main.querySelector("#kb-create").onclick = (e) => act(e.target, async () => {
    const emb = val("#kb-emb");
    const kb = await api(T("/knowledge-bases"), { method: "POST", body: { name: val("#kb-name"), embedding_provider: emb, embedding_model: emb === "openai" ? "text-embedding-3-small" : "hashing-v1" } });
    location.hash = `#/knowledge/${kb.id}`;
  });
  if (!current) return;
  const reload = () => VIEWS.knowledge(main, current);
  main.querySelectorAll("[data-del]").forEach((b) => (b.onclick = () => { if (confirm("Delete this document?")) act(b, async () => { await api(T(`/documents/${b.dataset.del}`), { method: "DELETE" }); reload(); }); }));
  main.querySelector("#kb-upload").onclick = (e) => act(e.target, async () => {
    const f = document.getElementById("kb-file").files[0];
    if (!f) throw new Error("Choose a file first");
    const form = new FormData(); form.append("file", f);
    await api(T(`/knowledge-bases/${current}/documents`), { method: "POST", form });
    reload();
  });
  main.querySelector("#kb-addurl").onclick = (e) => act(e.target, async () => { await api(T(`/knowledge-bases/${current}/urls`), { method: "POST", body: { url: val("#kb-url") } }); reload(); });
  main.querySelector("#kb-search").onclick = (e) => act(e.target, async () => {
    const hits = await api(T("/knowledge/search"), { method: "POST", body: { query: val("#kb-q"), knowledge_base_ids: [current] } });
    document.getElementById("kb-results").innerHTML = hits.length ? hits.map((h) => `<div class="panel"><b>${esc(h.document_title)}</b> <span class="muted">relevance ${h.score.toFixed(2)}</span><pre>${esc(h.content)}</pre></div>`).join("") : `<div class="empty">No results.</div>`;
  });
};

VIEWS.integrations = async (main) => {
  const [integs, kinds, creds] = await Promise.all([api(T("/integrations")), api("/v1/integration-kinds"), api(T("/credentials")).catch(() => [])]);
  main.innerHTML = `<h2>Integrations</h2><div class="panel">${table(["Name", "Kind", "Capabilities", "Enabled", ""], integs.map((i) => [esc(i.name), esc(i.kind), esc(i.capabilities.join(", ")), i.enabled ? "yes" : "no", `<button class="secondary" data-test="${i.id}">Test</button>`]))}<div class="out"></div></div>
    <div class="panel"><h3>Add integration</h3><div class="row"><label>Name<input id="i-name" placeholder="hubspot-main"></label>
      <label>Kind<select id="i-kind">${kinds.map((k) => `<option value="${k.kind}">${esc(k.kind)} — ${esc(k.description)}</option>`).join("")}</select></label>
      <label>Credential<select id="i-cred"><option value="">(none)</option>${creds.map((c) => `<option value="${c.id}">${esc(c.name)} (${esc(c.kind)})</option>`).join("")}</select></label></div>
      <textarea id="i-config" class="h-120">{}</textarea><button id="i-create">Add</button><div class="out"></div></div>
    <div class="panel"><h3>Credentials <span class="muted">(write-only; values are never shown again)</span></h3>${table(["Name", "Kind", "Updated"], creds.map((c) => [esc(c.name), esc(c.kind), fmt.date(c.updated_at)]))}
      <div class="row"><label>Name<input id="c-name"></label><label>Kind<select id="c-kind">${["hubspot", "salesforce", "google_service_account", "google_oauth", "twilio", "bearer", "basic", "api_key_header", "provider_api_key"].map((k) => `<option>${k}</option>`).join("")}</select></label></div>
      <textarea id="c-payload" class="h-90" placeholder='{"access_token": "..."}'></textarea><button id="c-create">Store credential</button><div class="out"></div></div>`;
  const reload = () => VIEWS.integrations(main);
  main.querySelectorAll("[data-test]").forEach((b) => (b.onclick = () => act(b, () => api(T(`/integrations/${b.dataset.test}/test`), { method: "POST" }), (r) => r.ok ? `Connection OK (${r.latency_ms} ms)` : `Failed: ${r.error} — ${r.message}`)));
  main.querySelector("#i-create").onclick = (e) => act(e.target, async () => {
    await api(T("/integrations"), { method: "POST", body: { name: val("#i-name"), kind: val("#i-kind"), config: JSON.parse(val("#i-config") || "{}"), credential_id: val("#i-cred") || null } });
    reload();
  });
  main.querySelector("#c-create").onclick = (e) => act(e.target, async () => {
    await api(T("/credentials"), { method: "POST", body: { name: val("#c-name"), kind: val("#c-kind"), payload: JSON.parse(val("#c-payload")) } });
    reload();
  });
};

VIEWS.numbers = async (main) => {
  const [nums, agents] = await Promise.all([api(T("/phone-numbers")), api(T("/agents"))]);
  const agentName = (id) => agents.find((a) => a.id === id)?.name || "—";
  const agentOpts = agents.map((a) => `<option value="${a.id}">${esc(a.name)}</option>`).join("");
  main.innerHTML = `<h2>Phone numbers</h2><div class="panel">${table(["Number", "Agent", "Environment", "Inbound trunk", "Outbound trunk", "Status"], nums.map((n) => [esc(n.e164), esc(agentName(n.agent_id)), esc(n.environment), esc(n.livekit_inbound_trunk_id || "—"), esc(n.livekit_outbound_trunk_id || "—"), statusBadge(n.status)]))}</div>
    <div class="panel"><h3>Add number</h3><div class="row"><label>E.164<input id="n-num" placeholder="+15145550100"></label><label>Agent<select id="n-agent">${agentOpts}</select></label>
      <label>Environment<select id="n-env"><option>prod</option><option>staging</option><option>dev</option></select></label><label>Outbound trunk id<input id="n-out" placeholder="ST_..."></label>
      <label><span><input type="checkbox" id="n-prov"> provision LiveKit inbound trunk + dispatch</span></label><button id="n-add">Add</button></div><div class="out"></div></div>
    <div class="panel"><h3>Place outbound call</h3><div class="row"><label>Agent<select id="o-agent">${agentOpts}</select></label><label>To<input id="o-to" placeholder="+15145550123"></label>
      <label>Purpose<input id="o-purpose" placeholder="Appointment reminder"></label><button id="o-call">Call</button></div>
      <p class="muted">Subject to the outbound allowlist, do-not-call list, calling window and dry-run setting.</p><div class="out"></div></div>`;
  main.querySelector("#n-add").onclick = (e) => act(e.target, async () => {
    await api(T("/phone-numbers"), { method: "POST", body: { e164: val("#n-num"), agent_id: val("#n-agent") || null, environment: val("#n-env"), livekit_outbound_trunk_id: val("#n-out") || null, provision_livekit: document.getElementById("n-prov").checked } });
    VIEWS.numbers(main);
  });
  main.querySelector("#o-call").onclick = (e) => act(e.target, () => api(T("/calls/outbound"), { method: "POST", body: { agent_id: val("#o-agent"), to: val("#o-to"), purpose: val("#o-purpose") } }),
    (r) => r.dry_run ? `Dry run: call validated but not placed (conversation ${r.conversation_id})` : `Dialing… conversation ${r.conversation_id}`);
};

VIEWS.evaluations = async (main, runId) => {
  if (runId) {
    const r = await api(T(`/evaluations/${runId}`));
    const s = r.summary || {};
    main.innerHTML = `<h2>Evaluation ${statusBadge(r.status)}</h2>${r.error ? `<div class="error">${esc(r.error)}</div>` : ""}
      <div class="grid"><div class="stat"><div class="label">Pass rate</div><div class="value">${fmt.pct(s.pass_rate)}</div></div>
      <div class="stat"><div class="label">95% CI</div><div class="value">${s.pass_rate_ci95 ? s.pass_rate_ci95.map(fmt.pct).join("–") : "—"}</div></div>
      <div class="stat"><div class="label">Mean score</div><div class="value">${s.mean_score ?? "—"}</div></div>
      <div class="stat"><div class="label">Tool success</div><div class="value">${fmt.pct(s.tool_success_rate)}</div></div>
      <div class="stat"><div class="label">Turn latency p95</div><div class="value">${fmt.sec(s.turn_latency_s?.p95)}</div></div></div>
      <h3>Failure categories</h3><div class="panel">${table(["Check", "Count"], Object.entries(s.failure_categories || {}).map(([k, v]) => [esc(k), String(v)]))}</div>
      <h3>Scenarios</h3>${(r.results || []).map((x) => `<div class="panel"><b>${esc(x.scenario)}</b> #${x.repetition} ${x.passed ? badge("passed", "ok") : badge("failed", "bad")} <span class="muted">score ${x.score}</span>
        ${x.failures.length ? `<ul>${x.failures.map((f) => `<li>${esc(f.check)}${f.critical ? " (critical)" : ""}: ${esc(f.detail)}</li>`).join("")}</ul>` : ""}
        <details><summary>Transcript</summary>${x.transcript.map((t) => `<div class="turn ${esc(t.role)}">${esc(t.text)}</div>`).join("")}<pre>${esc(JSON.stringify(x.tool_calls, null, 1))}</pre></details></div>`).join("")}`;
    return;
  }
  const [runs, agents, scenarios] = await Promise.all([api(T("/evaluations")), api(T("/agents")), api(T("/scenarios"))]);
  main.innerHTML = `<h2>Evaluations</h2><div class="panel"><h3>Run benchmark</h3><div class="row"><label>Agent<select id="e-agent">${agents.map((a) => `<option value="${a.id}">${esc(a.name)}</option>`).join("")}</select></label>
    <label>Version<select id="e-version"></select></label><label>Repetitions<input id="e-reps" type="number" value="1" min="1" max="10" class="w-80"></label><label>Seed<input id="e-seed" type="number" value="0" class="w-80"></label>
    <label><span><input type="checkbox" id="e-judge" checked> LLM judge</span></label></div>
    <label class="muted">Scenarios (none selected = all)</label><select id="e-scen" multiple size="6" class="full">${scenarios.map((s) => `<option value="${esc(s.slug)}">${esc(s.slug)} — ${esc(s.name)}${s.critical ? " (critical)" : ""}</option>`).join("")}</select>
    <div class="row"><button id="e-run">Start</button></div><div class="out"></div></div>
    <div class="panel">${table(["Created", "Suite", "Status", "Pass rate", "Runs"], runs.map((r) => [fmt.date(r.created_at), esc(r.suite), statusBadge(r.status), fmt.pct(r.summary?.pass_rate), String(r.summary?.runs ?? "—")]), { onClick: true })}</div>`;
  bindRows(main, (i) => (location.hash = `#/evaluations/${runs[i].id}`));
  await versionPicker(main, "#e-agent", "#e-version");
  main.querySelector("#e-run").onclick = (e) => act(e.target, async () => {
    const slugs = [...document.getElementById("e-scen").selectedOptions].map((o) => o.value);
    const r = await api(T("/evaluations"), { method: "POST", body: { agent_id: val("#e-agent"), version_id: val("#e-version"), scenario_slugs: slugs, repetitions: Number(val("#e-reps")), seed: Number(val("#e-seed")), use_judge: document.getElementById("e-judge").checked } });
    location.hash = `#/evaluations/${r.id}`;
  }, "Queued");
};

async function versionPicker(main, agentSel, versionSel) {
  const fill = async () => {
    const id = val(agentSel);
    if (!id) return;
    const a = await api(T(`/agents/${id}`));
    main.querySelector(versionSel).innerHTML = a.versions.map((v) => `<option value="${v.id}">v${v.version} · ${esc(v.status)}</option>`).join("");
  };
  main.querySelector(agentSel).onchange = fill;
  await fill();
}

VIEWS.optimizations = async (main, optId) => {
  if (optId) {
    const o = await api(T(`/optimizations/${optId}`));
    const b = o.report?.baseline, best = o.report?.best;
    main.innerHTML = `<h2>Optimization ${statusBadge(o.status)} ${o.decision ? statusBadge(o.decision) : ""}</h2>${o.error ? `<div class="error">${esc(o.error)}</div>` : ""}
      <div class="grid"><div class="stat"><div class="label">Baseline pass rate</div><div class="value">${fmt.pct(b?.pass_rate)}</div></div>
      <div class="stat"><div class="label">Best candidate</div><div class="value">${fmt.pct(best?.pass_rate)}</div></div>
      <div class="stat"><div class="label">Iterations</div><div class="value">${o.report?.iterations?.length ?? 0}</div></div></div>
      ${o.decision === "eligible" ? `<div class="panel"><h3>Approve candidate</h3><p class="muted">Publishes the candidate version and rolls it out. Requires admin.</p>
        <div class="row"><label>Environment<select id="ap-env"><option>staging</option><option>prod</option><option>dev</option></select></label><label>Canary %<input id="ap-pct" type="number" value="10" min="0" max="100" class="w-90"></label>
        <button id="ap-ok">Approve & roll out</button><button class="danger" id="ap-no">Reject</button></div><div class="out"></div></div>` : ""}
      <div class="panel"><h3>Report</h3><pre>${esc(o.report_markdown || "Not finished yet.")}</pre></div>`;
    const ok = main.querySelector("#ap-ok");
    if (ok) {
      ok.onclick = (e) => act(e.target, async () => { await api(T(`/optimizations/${optId}/approve`), { method: "POST", body: { environment: val("#ap-env"), canary_percent: Number(val("#ap-pct")) } }); VIEWS.optimizations(main, optId); });
      main.querySelector("#ap-no").onclick = (e) => act(e.target, async () => { await api(T(`/optimizations/${optId}/reject`), { method: "POST" }); VIEWS.optimizations(main, optId); });
    }
    return;
  }
  const [opts, agents] = await Promise.all([api(T("/optimizations")), api(T("/agents"))]);
  main.innerHTML = `<h2>Optimizations</h2><div class="panel"><h3>Start optimization</h3><div class="row"><label>Agent<select id="op-agent">${agents.map((a) => `<option value="${a.id}">${esc(a.name)}</option>`).join("")}</select></label>
    <label>Baseline version<select id="op-version"></select></label><label>Max iterations<input id="op-iter" type="number" value="3" min="1" max="10" class="w-80"></label>
    <label>Min pass rate<input id="op-min" type="number" value="0.8" step="0.05" min="0" max="1" class="w-90"></label><button id="op-start">Start</button></div>
    <p class="muted">Candidates are evaluated on the same seeded scenarios; regressions are rejected; promotion always requires approval.</p><div class="out"></div></div>
    <div class="panel">${table(["Created", "Status", "Decision", "Baseline", "Best"], opts.map((o) => [fmt.date(o.created_at), statusBadge(o.status), o.decision ? statusBadge(o.decision) : "—", fmt.pct(o.report?.baseline?.pass_rate), fmt.pct(o.report?.best?.pass_rate)]), { onClick: true })}</div>`;
  bindRows(main, (i) => (location.hash = `#/optimizations/${opts[i].id}`));
  await versionPicker(main, "#op-agent", "#op-version");
  main.querySelector("#op-start").onclick = (e) => act(e.target, async () => {
    const o = await api(T("/optimizations"), { method: "POST", body: { agent_id: val("#op-agent"), baseline_version_id: val("#op-version"), max_iterations: Number(val("#op-iter")), min_pass_rate: Number(val("#op-min")) } });
    location.hash = `#/optimizations/${o.id}`;
  }, "Queued");
};

VIEWS.settings = async (main) => {
  const [members, keys, audit] = await Promise.all([api(T("/members")), api(T("/api-keys")).catch(() => null), api(T("/audit?limit=50")).catch(() => null)]);
  main.innerHTML = `<h2>Settings</h2><div class="panel"><h3>Members</h3>${table(["Email", "Name", "Role"], members.map((m) => [esc(m.email), esc(m.full_name), badge(m.role)]))}
    <div class="row"><label>Email<input id="m-email" type="email"></label><label>Role<select id="m-role"><option>viewer</option><option>editor</option><option>admin</option><option>owner</option></select></label>
    <label>Initial password (new users)<input id="m-pass" type="password"></label><button id="m-add">Add member</button></div><div class="out"></div></div>
    ${keys ? `<div class="panel"><h3>API keys</h3>${table(["Name", "Role", "Prefix", "Last used", "Expires", ""], keys.map((k) => [esc(k.name), badge(k.role), esc(k.prefix), fmt.date(k.last_used_at), fmt.date(k.expires_at), k.revoked ? badge("revoked", "bad") : `<button class="danger" data-revoke="${k.id}">Revoke</button>`]))}
      <div class="row"><label>Name<input id="k-name"></label><label>Role<select id="k-role"><option>viewer</option><option selected>editor</option><option>admin</option></select></label><button id="k-add">Create key</button></div><div class="out"></div></div>` : ""}
    ${audit ? `<div class="panel"><h3>Audit log</h3>${table(["When", "Actor", "Action", "Resource"], audit.map((a) => [fmt.date(a.at), esc(a.actor), esc(a.action), esc(a.resource)]))}</div>` : ""}`;
  main.querySelector("#m-add").onclick = (e) => act(e.target, async () => {
    const body = { email: val("#m-email"), role: val("#m-role") };
    if (val("#m-pass")) body.password = val("#m-pass");
    await api(T("/members"), { method: "POST", body }); VIEWS.settings(main);
  });
  main.querySelector("#k-add")?.addEventListener("click", (e) => act(e.target, () => api(T("/api-keys"), { method: "POST", body: { name: val("#k-name"), role: val("#k-role") } }),
    (r) => `New key (copy it now, it will not be shown again):\n${r.key}`));
  main.querySelectorAll("[data-revoke]").forEach((b) => (b.onclick = () => { if (confirm("Revoke this key?")) act(b, async () => { await api(T(`/api-keys/${b.dataset.revoke}`), { method: "DELETE" }); VIEWS.settings(main); }); }));
};

render();
