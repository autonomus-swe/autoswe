"use strict";
/* autoswe run console. No build step: plain modules, fetch and EventSource. */

const PHASES = ["setup", "analyze", "plan", "decompose", "code", "test", "pr", "done"];
// Phase 3 added three phases that are excursions rather than stages: a run in DEBUG has
// not left TEST behind, it is going round again. They are drawn as a chip of their own,
// anchored where they happen. The reason this matters more than cosmetics: the rail used
// `PHASES.indexOf(run.phase)`, which is -1 for any phase not in the list, and then no
// chip matched "now" and none matched "past" either — so the whole rail went blank
// exactly while the run was doing the most interesting thing it can do.
const OFF_RAIL = { debug: "test", escalate: "test", awaiting_input: "plan" };
const TERMINAL = new Set(["done", "failed", "cancelled"]);
const KEY_STORAGE = "autoswe.apikey";

const el = {
  key: document.getElementById("key"),
  runs: document.getElementById("runs"),
  detail: document.getElementById("detail"),
  refresh: document.getElementById("refresh"),
  newRun: document.getElementById("new-run"),
  dialog: document.getElementById("new-run-dialog"),
  form: document.getElementById("new-run-form"),
  formError: document.getElementById("new-run-error"),
};

let currentRunId = null;
let stream = null;
// the questions live in an event, not on the run row, so a re-render has to remember them
let pendingQuestions = [];
let answered = { id: null, at: 0 };

// ---- helpers ---------------------------------------------------------------

const apiKey = () => el.key.value.trim();

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: { "X-API-Key": apiKey(), "content-type": "application/json", ...(options.headers || {}) },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail ?? detail; } catch { /* not JSON */ }
    throw new Error(`${res.status} ${detail}`);
  }
  return res.status === 204 ? null : res.json();
}

function toast(message, isError = false) {
  const node = document.createElement("div");
  node.className = "toast" + (isError ? " err" : "");
  node.textContent = message;
  document.body.append(node);
  setTimeout(() => node.remove(), 5000);
}

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const clock = (iso) => {
  const d = iso ? new Date(iso) : new Date();
  return d.toLocaleTimeString([], { hour12: false });
};

const money = (n) => (n >= 0.01 ? `$${n.toFixed(2)}` : n > 0 ? `$${n.toFixed(4)}` : "$0");
const num = (n) => Number(n ?? 0).toLocaleString();

/** Runs in the rail often share a goal, so age is what tells them apart. */
function ago(iso) {
  const secs = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  for (const [limit, div, unit] of [[60, 1, "s"], [3600, 60, "m"], [86400, 3600, "h"]]) {
    if (secs < limit) return `${Math.floor(secs / div)}${unit} ago`;
  }
  return secs < 604800 ? `${Math.floor(secs / 86400)}d ago`
                       : new Date(iso).toLocaleDateString([], { month: "short", day: "numeric" });
}

// ---- run list --------------------------------------------------------------

async function loadRuns() {
  if (!apiKey()) { el.runs.innerHTML = '<li class="empty">Enter your API key to load runs.</li>'; return; }
  try {
    const runs = await api("/runs?limit=50");
    if (!runs.length) { el.runs.innerHTML = '<li class="empty">No runs yet. Start one above.</li>'; return; }
    el.runs.innerHTML = runs.map(renderRunRow).join("");
    for (const node of el.runs.querySelectorAll(".run")) {
      node.addEventListener("click", () => selectRun(node.dataset.id));
    }
    if (currentRunId) markCurrent(currentRunId);
  } catch (e) {
    el.runs.innerHTML = `<li class="empty">${esc(e.message)}</li>`;
  }
}

function renderRunRow(r) {
  return `<li class="run" data-id="${r.run_id}" tabindex="0">
    <div class="run-goal">${esc(r.goal)}</div>
    <div class="run-meta">
      <span class="pill ${esc(r.status)}">${esc(r.status)}</span>
      <span>${esc(r.phase)}</span>
      <span>${money(r.cost_usd)}</span>
      <span class="run-age">${esc(ago(r.created_at))}</span>
    </div>
  </li>`;
}

function markCurrent(runId) {
  for (const node of el.runs.querySelectorAll(".run")) {
    node.setAttribute("aria-current", String(node.dataset.id === runId));
  }
}

// ---- run detail ------------------------------------------------------------

async function selectRun(runId) {
  currentRunId = runId;
  markCurrent(runId);
  if (stream) { stream.close(); stream = null; }
  try {
    const data = await api(`/runs/${runId}/detail`);
    // a finished run's old questions are history, not a prompt
    pendingQuestions = TERMINAL.has(data.run.status) ? []
      : data.events.filter((e) => e.type === "awaiting_input").at(-1)?.payload.questions ?? [];
    renderDetail(data);
    // exactly one source fills the feed. A live stream replays the history itself before
    // it follows, so seeding from the detail payload too would show every event twice.
    if (TERMINAL.has(data.run.status)) for (const e of data.events) addEvent(e.type, e.payload, e.ts);
    else follow(runId);
  } catch (e) {
    el.detail.innerHTML = `<div class="placeholder"><h1>Could not load run</h1><p>${esc(e.message)}</p></div>`;
  }
}

function renderDetail(d) {
  const r = d.run;
  const failed = r.status === "failed" || r.status === "cancelled";
  el.detail.innerHTML = `
    <div class="head">
      <div>
        <h1>${esc(r.goal)}</h1>
        <div class="sub">
          ${esc(r.repo_url)} · <code>${esc(r.work_branch)}</code>
          ${r.pr_url ? ` · <a href="${esc(r.pr_url)}" target="_blank" rel="noopener">pull request</a>` : ""}
        </div>
      </div>
      <div class="head-actions">
        <span class="pill ${esc(r.status)}">${esc(r.status)}</span>
        ${TERMINAL.has(r.status) ? "" : '<button class="btn" id="cancel-run" type="button">Cancel</button>'}
      </div>
    </div>

    <div class="phases">${renderRail(r, failed)}</div>

    <dl class="stats">
      ${stat("tasks", `${d.tasks.filter((t) => t.status === "done").length} / ${d.tasks.length}`)}
      ${stat("tool calls", num(d.totals.tool_calls))}
      ${stat("model turns", num(d.totals.llm_calls))}
      ${stat("tokens in", num(d.totals.input_tokens))}
      ${stat("tokens out", num(d.totals.output_tokens))}
      ${stat("cost", money(d.totals.cost_usd))}
    </dl>

    ${r.error ? `<div class="ask" style="border-color:var(--rose);background:var(--rose-dim)">
      <h3 style="color:var(--rose)">Run ended with an error</h3>
      <div class="sub" style="overflow-wrap:anywhere">${esc(r.error)}</div></div>` : ""}

    <div id="ask-slot"></div>

    ${d.tasks.length ? `<h2 class="section">Tasks</h2>
      <div class="tasks">${d.tasks.map(renderTask).join("")}</div>` : ""}

    <h2 class="section">Live events <span id="feed-note">${TERMINAL.has(r.status) ? "replayed" : "streaming"}</span></h2>
    <div class="feed" id="feed"></div>

    ${d.tool_calls.length ? `<h2 class="section">Tool calls <span>${d.tool_calls.length}</span></h2>
      <div class="scroll"><table class="tbl">
        <thead><tr><th>#</th><th>tool</th><th>input</th><th class="num">ms</th></tr></thead>
        <tbody>${d.tool_calls.map(renderToolCall).join("")}</tbody>
      </table></div>` : ""}

    ${d.llm_calls.length ? `<h2 class="section">Model turns <span>${d.llm_calls.length}</span></h2>
      <div class="scroll"><table class="tbl">
        <thead><tr><th>#</th><th>model</th><th>stop</th><th class="num">in</th><th class="num">out</th><th class="num">ms</th><th class="num">cost</th></tr></thead>
        <tbody>${d.llm_calls.map((c) => `<tr>
          <td class="num">${c.seq}</td><td class="mono">${esc(c.model)}</td>
          <td class="mono">${esc(c.stop_reason ?? "-")}</td>
          <td class="num">${num(c.input_tokens)}</td><td class="num">${num(c.output_tokens)}</td>
          <td class="num">${num(c.latency_ms)}</td><td class="num">${money(c.cost_usd)}</td>
        </tr>`).join("")}</tbody>
      </table></div>` : ""}
  `;

  const cancelBtn = document.getElementById("cancel-run");
  if (cancelBtn) cancelBtn.addEventListener("click", () => cancelRun(r.run_id));
  // the event announcing the questions and the row flipping to awaiting_input do not land
  // together, so either one is enough to keep the form on screen across a re-render
  if (r.status === "awaiting_input" || pendingQuestions.length) renderAsk(r.run_id, pendingQuestions);
}

const stat = (label, value) => `<div class="stat"><dt>${label}</dt><dd>${value}</dd></div>`;

function renderRail(run, failed) {
  // The excursion, when there is one, is spliced in after the stage it belongs to, so
  // `indexOf` below finds the run's phase whatever it is and the rail keeps its meaning.
  const anchor = OFF_RAIL[run.phase];
  const rail = anchor
    ? [...PHASES.slice(0, PHASES.indexOf(anchor) + 1), run.phase,
       ...PHASES.slice(PHASES.indexOf(anchor) + 1)]
    : PHASES;
  // A terminal run reports phase "failed", which is a status and not a stage: mark the
  // last stage it actually reached rather than leaving every chip inert.
  const at = rail.indexOf(run.phase);
  return rail
    .map((phase, i) => {
      let cls = "phase";
      if (failed && i === at) cls += " fail";
      else if (i === at) cls += " now";
      else if (at >= 0 && i < at) cls += " past";
      else if (OFF_RAIL[phase]) cls += " off-rail";
      return `<span class="${cls}">${phase}</span>`;
    })
    .join("");
}

function renderTask(t) {
  return `<div class="task" data-status="${esc(t.status)}">
    <div class="task-top">
      <span class="task-id">${esc(t.id)}</span>
      <h3>${esc(t.title)}</h3>
      <span class="pill ${esc(t.status)}">${esc(t.status.replace("_", " "))}</span>
    </div>
    ${t.acceptance_criteria.length ? `<ul>${t.acceptance_criteria.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>` : ""}
    ${t.files.length ? `<div class="files">${t.files.map(esc).join(" · ")}</div>` : ""}
  </div>`;
}

function renderToolCall(c) {
  // the editor tool carries both, and "view" alone says nothing about what was viewed
  // a search carries both a pattern and a path; the pattern is what was being looked for
  const i = c.input ?? {};
  const summary = [i.command, i.pattern ?? i.path ?? i.selector ?? i.message]
    .filter(Boolean).join(" ") || Object.values(i).filter((v) => typeof v === "string")[0] || "";
  return `<tr>
    <td class="num">${c.seq}</td>
    <td class="mono${c.exit_code ? " bad" : ""}">${esc(c.name)}</td>
    <td class="mono">${esc(String(summary).slice(0, 90))}</td>
    <td class="num">${num(c.duration_ms)}</td>
  </tr>`;
}

// ---- events ----------------------------------------------------------------

function addEvent(type, payload, at) {
  const feed = document.getElementById("feed");
  if (!feed) return;
  const bad = type === "run_finished" && payload.status !== "done";
  const row = document.createElement("div");
  row.className = "ev" + (bad ? " err" : "");
  row.innerHTML = `<time>${clock(at)}</time><span class="type">${esc(type)}</span>
                   <span class="body">${esc(describe(type, payload))}</span>`;
  feed.append(row);
  feed.scrollTop = feed.scrollHeight;
}

function describe(type, p) {
  switch (type) {
    case "phase_changed": return p.tasks ? `${p.phase} (${p.tasks} tasks)` : p.phase;
    case "agent_started": return p.task_id ? `${p.agent} on ${p.task_id}` : p.agent ?? "";
    case "agent_finished": return p.error ? `error: ${p.error}` : "finished";
    case "tool_call": return `${p.name}${p.is_error ? " — failed" : ""} (${p.duration_ms}ms)`;
    case "test_report": {
      // a baseline is not a verdict on the agent's work: it is what it inherited
      const verdict = p.baseline ? "baseline" : p.passed ? "passed" : "failed";
      const excused = [[p.flaky, "flaky"], [p.pre_existing, "pre-existing"]]
        .filter(([ids]) => ids?.length).map(([ids, label]) => ` · ${ids.length} ${label}`).join("");
      return `${verdict} — ${p.total} tests, ${p.failed} failing${excused}`;
    }
    case "pr_opened": return p.pr_url ?? "";
    case "awaiting_input": return (p.questions ?? []).join(" · ");
    case "run_finished": return `${p.status}${p.cost_usd ? ` · ${money(p.cost_usd)}` : ""}`;
    default: return typeof p === "object" ? JSON.stringify(p).slice(0, 200) : String(p);
  }
}

function follow(runId) {
  // EventSource cannot set headers, so the key rides as a query parameter for the stream
  stream = new EventSource(`/runs/${runId}/events?key=${encodeURIComponent(apiKey())}`);
  for (const type of ["phase_changed", "agent_started", "agent_finished", "tool_call",
                      "tool_result", "test_report", "pr_opened", "awaiting_input", "log"]) {
    stream.addEventListener(type, (e) => {
      const payload = JSON.parse(e.data);
      addEvent(type, payload);
      if (type === "awaiting_input") {
        pendingQuestions = payload.questions ?? [];
        answered = { id: null, at: 0 }; // a fresh question deserves a fresh form
        renderAsk(runId, pendingQuestions);
      }
      // both of these change the run row, so the pill and the chips need re-reading
      if (type === "awaiting_input" || type === "phase_changed") scheduleRefresh(runId);
    });
  }
  stream.addEventListener("run_finished", (e) => {
    addEvent("run_finished", JSON.parse(e.data));
    stream.close(); stream = null;
    const note = document.getElementById("feed-note");
    if (note) note.textContent = "finished";
    selectRun(runId);
    loadRuns();
  });
  stream.onerror = () => { const n = document.getElementById("feed-note"); if (n) n.textContent = "reconnecting"; };
}

let refreshTimer = null;

/** Phases can change several times a second; one re-read afterwards is enough. */
function scheduleRefresh(runId) {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => refreshQuietly(runId), 700);
}

async function refreshQuietly(runId) {
  try {
    const d = await api(`/runs/${runId}/detail`);
    if (currentRunId !== runId) return;
    const feed = document.getElementById("feed");
    const rows = feed ? feed.innerHTML : "";
    renderDetail(d);
    const again = document.getElementById("feed");
    if (again && rows) { again.innerHTML = rows; again.scrollTop = again.scrollHeight; }
    if (!TERMINAL.has(d.run.status) && !stream) follow(runId);
  } catch { /* a transient failure should not clear the view */ }
}

// ---- awaiting input --------------------------------------------------------

function renderAsk(runId, questions) {
  const slot = document.getElementById("ask-slot");
  if (!slot) return;
  // the run stays parked until the worker reads the inbox, so re-rendering would put an
  // empty form back under someone who has already typed. Show what is actually happening.
  if (answered.id === runId && Date.now() - answered.at < 30000) {
    slot.innerHTML = `<div class="ask"><h3>Answer sent</h3>
      <div class="sub">Waiting for the run to pick it up.</div></div>`;
    return;
  }
  slot.innerHTML = `<div class="ask">
    <h3>The planner needs an answer</h3>
    ${questions.length ? `<ul>${questions.map((q) => `<li>${esc(q)}</li>`).join("")}</ul>` : ""}
    <form id="ask-form"><input name="text" required placeholder="Your answer" autocomplete="off">
    <button class="btn btn-primary" type="submit">Send</button></form>
  </div>`;
  document.getElementById("ask-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = new FormData(e.target).get("text");
    try {
      await api(`/runs/${runId}/answer`, { method: "POST", body: JSON.stringify({ text }) });
      pendingQuestions = [];
      answered = { id: runId, at: Date.now() };
      renderAsk(runId, []);
      toast("Answer sent — the run continues.");
    } catch (err) { toast(err.message, true); }
  });
}

async function cancelRun(runId) {
  try {
    await api(`/runs/${runId}/cancel`, { method: "POST" });
    toast("Cancel requested. The run stops at the next node.");
  } catch (e) { toast(e.message, true); }
}

// ---- wiring ----------------------------------------------------------------

el.key.value = localStorage.getItem(KEY_STORAGE) ?? "";
el.key.addEventListener("change", () => {
  localStorage.setItem(KEY_STORAGE, apiKey());
  loadRuns();
});
el.refresh.addEventListener("click", loadRuns);
el.newRun.addEventListener("click", () => { el.formError.hidden = true; el.dialog.showModal(); });

el.form.addEventListener("submit", async (e) => {
  if (e.submitter?.value !== "start") return;
  e.preventDefault();
  const form = new FormData(el.form);
  try {
    const body = JSON.stringify({
      repo_url: form.get("repo_url"),
      goal: form.get("goal"),
      base_branch: form.get("base_branch"),
    });
    const { run_id } = await api("/runs", { method: "POST", body });
    el.dialog.close();
    el.form.reset();
    await loadRuns();
    selectRun(run_id);
    toast("Run queued.");
  } catch (err) {
    el.formError.textContent = err.message;
    el.formError.hidden = false;
  }
});

loadRuns();
setInterval(() => { if (!stream) loadRuns(); }, 15000);
