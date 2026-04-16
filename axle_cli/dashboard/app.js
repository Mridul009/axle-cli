const state = {
  token: window.localStorage.getItem("axleAdminToken") || "",
  runs: [],
  workers: [],
  queues: [],
  selectedRunId: null,
  selectedArtifact: "timeline",
};

const els = {
  tokenInput: document.querySelector("#admin-token"),
  authForm: document.querySelector("#auth-form"),
  runsList: document.querySelector("#runs-list"),
  workersList: document.querySelector("#workers-list"),
  queuesTable: document.querySelector("#queues-table"),
  runDetail: document.querySelector("#run-detail"),
  metricQueued: document.querySelector("#metric-queued"),
  metricRunning: document.querySelector("#metric-running"),
  metricWorkers: document.querySelector("#metric-workers"),
  metricFailed: document.querySelector("#metric-failed"),
};

function headers() {
  const value = state.token.trim();
  return value ? { Authorization: `Bearer ${value}` } : {};
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      ...headers(),
      ...(options.headers || {}),
    },
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `Request failed with ${response.status}`);
  }
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    return response.json();
  }
  return response.text();
}

function formatTime(value) {
  if (!value) return "none";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function badge(status) {
  const value = status || "unknown";
  return `<span class="badge ${escapeHtml(value)}">${escapeHtml(value)}</span>`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function setNotice(target, message) {
  target.innerHTML = `<div class="notice">${escapeHtml(message)}</div>`;
}

async function loadRuns() {
  try {
    const payload = await api("/api/runs");
    state.runs = payload.runs || [];
    renderRuns();
    renderMetrics();
    if (state.selectedRunId) {
      await selectRun(state.selectedRunId);
    }
  } catch (error) {
    setNotice(els.runsList, error.message);
  }
}

async function loadWorkers() {
  try {
    const payload = await api("/api/workers");
    state.workers = payload.workers || [];
    renderWorkers();
    renderMetrics();
  } catch (error) {
    setNotice(els.workersList, error.message);
  }
}

async function loadQueues() {
  try {
    const payload = await api("/api/queues");
    state.queues = payload.queues || [];
    renderQueues();
    renderMetrics();
  } catch (error) {
    els.queuesTable.innerHTML = `<tr><td colspan="7">${escapeHtml(error.message)}</td></tr>`;
  }
}

function renderMetrics() {
  const counts = state.runs.reduce((acc, run) => {
    acc[run.status] = (acc[run.status] || 0) + 1;
    return acc;
  }, {});
  els.metricQueued.textContent = counts.queued || 0;
  els.metricRunning.textContent = counts.running || 0;
  els.metricFailed.textContent = counts.failed || 0;
  els.metricWorkers.textContent = state.workers.length || 0;
}

function renderRuns() {
  if (!state.runs.length) {
    setNotice(els.runsList, "No runs found.");
    return;
  }
  els.runsList.innerHTML = state.runs
    .map((run) => {
      const active = run.run_id === state.selectedRunId ? " active" : "";
      return `
        <button class="run-item${active}" type="button" data-run-id="${escapeHtml(run.run_id)}">
          <span class="run-title">
            <strong>${escapeHtml(run.issue_key || run.run_id)}</strong>
            ${badge(run.status)}
          </span>
          <span class="meta">
            <span>${escapeHtml(run.run_id)}</span>
            <span>priority ${escapeHtml(run.priority || 0)}</span>
            <span>${escapeHtml(run.queue_name || "default")}</span>
          </span>
        </button>
      `;
    })
    .join("");
}

function renderWorkers() {
  if (!state.workers.length) {
    setNotice(els.workersList, "No workers registered.");
    return;
  }
  els.workersList.innerHTML = state.workers
    .map((worker) => `
      <article class="worker-item">
        <div class="run-title">
          <strong>${escapeHtml(worker.worker_id)}</strong>
          ${badge(worker.status || "online")}
        </div>
        <div class="meta">
          <span>${escapeHtml(worker.hostname || "unknown host")}</span>
          <span>last seen ${escapeHtml(formatTime(worker.last_seen_at))}</span>
          <span>${escapeHtml(worker.version || "unknown version")}</span>
        </div>
      </article>
    `)
    .join("");
}

function renderQueues() {
  if (!state.queues.length) {
    els.queuesTable.innerHTML = `<tr><td colspan="7">No queues found.</td></tr>`;
    return;
  }
  els.queuesTable.innerHTML = state.queues
    .map((queue) => `
      <tr>
        <td>${escapeHtml(queue.queue_name)}</td>
        <td>${escapeHtml(queue.queued || 0)}</td>
        <td>${escapeHtml(queue.paused || 0)}</td>
        <td>${escapeHtml(queue.running || 0)}</td>
        <td>${escapeHtml(queue.failed || 0)}</td>
        <td>${escapeHtml(queue.cancelled || 0)}</td>
        <td>${escapeHtml(queue.total || 0)}</td>
      </tr>
    `)
    .join("");
}

async function selectRun(runId) {
  state.selectedRunId = runId;
  renderRuns();
  try {
    const [run, timeline] = await Promise.all([
      api(`/api/runs/${encodeURIComponent(runId)}`),
      api(`/api/runs/${encodeURIComponent(runId)}/timeline`),
    ]);
    renderRunDetail(run, timeline);
  } catch (error) {
    setNotice(els.runDetail, error.message);
  }
}

function renderRunDetail(run, timeline) {
  els.runDetail.innerHTML = `
    <div class="run-title">
      <h2>${escapeHtml(run.issue_key || run.run_id)}</h2>
      ${badge(run.status)}
    </div>
    <div class="detail-grid">
      ${field("Run id", run.run_id)}
      ${field("Repository", run.repository)}
      ${field("Branch", run.base_branch)}
      ${field("Queue", `${run.queue_name || "default"} / priority ${run.priority || 0}`)}
      ${field("PR", run.pr_url ? `<a href="${escapeHtml(run.pr_url)}" target="_blank" rel="noreferrer">${escapeHtml(run.pr_url)}</a>` : "not created", true)}
      ${field("Updated", formatTime(run.updated_at))}
    </div>
    <div class="controls">
      <button class="secondary" data-action="pause">Pause</button>
      <button class="secondary" data-action="resume">Resume</button>
      <button class="danger" data-action="cancel">Cancel</button>
      <button class="secondary" data-action="retry">Retry</button>
      <input id="priority-input" type="number" value="${escapeHtml(run.priority || 0)}" aria-label="Priority">
      <input id="queue-input" type="text" value="${escapeHtml(run.queue_name || "default")}" aria-label="Queue">
      <button data-action="priority">Set priority</button>
    </div>
    <div class="subtabs" aria-label="Run views">
      ${artifactButton("timeline", "Timeline")}
      ${artifactButton("transcript", "Transcript")}
      ${artifactButton("diff", "Diff")}
      ${artifactButton("test-log", "Test log")}
    </div>
    <div id="run-view"></div>
  `;
  renderRunView(timeline);
}

function field(label, value, raw = false) {
  return `
    <div class="field">
      <span>${escapeHtml(label)}</span>
      <strong>${raw ? value : escapeHtml(value || "none")}</strong>
    </div>
  `;
}

function artifactButton(name, label) {
  const active = state.selectedArtifact === name ? " active" : "";
  return `<button class="subtab${active}" type="button" data-artifact="${escapeHtml(name)}">${escapeHtml(label)}</button>`;
}

function renderRunView(timeline) {
  const target = document.querySelector("#run-view");
  if (!target) return;
  if (state.selectedArtifact === "timeline") {
    const events = timeline.events || [];
    target.innerHTML = events.length
      ? `<ul class="timeline">${events.map((event) => `
          <li>
            <strong>${escapeHtml(event.kind)}</strong>
            <div>${escapeHtml(event.stage || "Run")} - ${escapeHtml(event.message || "")}</div>
            <div class="meta">${escapeHtml(formatTime(event.timestamp))}</div>
          </li>
        `).join("")}</ul>`
      : `<div class="notice">No timeline events recorded.</div>`;
    return;
  }
  target.innerHTML = `<pre class="viewer">Loading ${escapeHtml(state.selectedArtifact)}...</pre>`;
  loadArtifact(state.selectedRunId, state.selectedArtifact);
}

async function loadArtifact(runId, artifact) {
  if (!runId) return;
  const target = document.querySelector("#run-view");
  try {
    const content = await api(`/api/runs/${encodeURIComponent(runId)}/${encodeURIComponent(artifact)}`);
    target.innerHTML = `<pre class="viewer">${escapeHtml(content || "No content.")}</pre>`;
  } catch (error) {
    target.innerHTML = `<div class="notice">${escapeHtml(error.message)}</div>`;
  }
}

async function runAction(action) {
  if (!state.selectedRunId) return;
  const runId = encodeURIComponent(state.selectedRunId);
  const options = { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" };
  if (action === "pause" || action === "cancel") {
    options.body = JSON.stringify({ reason: "dashboard" });
  }
  if (action === "priority") {
    const priority = Number(document.querySelector("#priority-input")?.value || 0);
    const queueName = document.querySelector("#queue-input")?.value || "default";
    options.body = JSON.stringify({ priority, queue_name: queueName });
  }
  await api(`/api/runs/${runId}/${action}`, options);
  await Promise.all([loadRuns(), loadQueues()]);
}

document.addEventListener("click", async (event) => {
  const runButton = event.target.closest("[data-run-id]");
  if (runButton) {
    await selectRun(runButton.dataset.runId);
    return;
  }
  const tab = event.target.closest("[data-tab]");
  if (tab) {
    document.querySelectorAll(".tab, .panel").forEach((item) => item.classList.remove("active"));
    tab.classList.add("active");
    document.querySelector(`#panel-${tab.dataset.tab}`)?.classList.add("active");
    return;
  }
  const action = event.target.closest("[data-action]");
  if (action) {
    action.disabled = true;
    try {
      await runAction(action.dataset.action);
    } catch (error) {
      window.alert(error.message);
    } finally {
      action.disabled = false;
    }
    return;
  }
  const artifact = event.target.closest("[data-artifact]");
  if (artifact) {
    state.selectedArtifact = artifact.dataset.artifact;
    document.querySelectorAll(".subtab").forEach((item) => item.classList.remove("active"));
    artifact.classList.add("active");
    const timeline = await api(`/api/runs/${encodeURIComponent(state.selectedRunId)}/timeline`);
    renderRunView(timeline);
  }
});

els.authForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  state.token = els.tokenInput.value.trim();
  window.localStorage.setItem("axleAdminToken", state.token);
  await refreshAll();
});

document.querySelector("#refresh-runs").addEventListener("click", loadRuns);
document.querySelector("#refresh-workers").addEventListener("click", loadWorkers);
document.querySelector("#refresh-queues").addEventListener("click", loadQueues);

async function refreshAll() {
  await Promise.all([loadRuns(), loadWorkers(), loadQueues()]);
}

els.tokenInput.value = state.token;
refreshAll();
