"use strict";

// Execute the real single-flight /api/jobs loader with a small DOM harness.
// Neither polling nor a click on Refresh should issue a duplicate GET.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const js = fs.readFileSync(path.join(__dirname, "../src/reference_topology/static/app.js"), "utf8");
const html = fs.readFileSync(path.join(__dirname, "../src/reference_topology/static/index.html"), "utf8");
assert(html.includes('id="shutdown-workspace"'));
assert(html.includes('id="task-overview"'));
assert(js.includes('api("/api/shutdown"'));
assert(js.includes('addEventListener("click", shutdownWorkspace)'));

const start = js.indexOf("  // Single-flight job polling:");
const end = js.indexOf("  function renderJobs() {", start);
assert(start >= 0 && end > start, "Missing single-flight polling implementation");
class Node {
  constructor() {
    this.textContent = "";
    this.children = [];
    this.classList = { toggle: (key, active) => { this.warning = Boolean(active); } };
    this.hidden = false;
  }
  replaceChildren(...children) { this.children = children; }
}
const nodes = Object.fromEntries(["nav-job-count", "stat-running", "task-overview"].map((name) => [name, new Node()]));
const state = {
  view: "tasks", initialized: true, jobs: [], jobsCounts: {},
  jobsSignature: "", jobsTotal: 0, jobsWorker: null, shuttingDown: false,
};
let requests = 0, renders = 0, refreshed = 0, sendResult;
const ctx = vm.createContext({
  state,
  $: (key) => nodes[key],
  element: (_, css, value) => ({ className: css, textContent: value || "", children: [] }),
  api: async () => { requests++; return new Promise((resolve) => { sendResult = resolve; }); },
  itemsOf: (result) => result.items,
  renderJobs: () => { renders++; },
  refreshAfterChange: async () => { refreshed++; },
  Date,
});
vm.runInContext(js.slice(start, end), ctx);
async function main() {
  const first = ctx.loadJobs(true);
  const second = ctx.loadJobs(true);
  const poll = ctx.loadJobs();
  assert.equal(requests, 1, "Concurrent refresh and timer must share one HTTP request");
  sendResult({
    total: 140,
    items: [
      { id: "first", status: "running", progress_current: 1, progress_total: 3, updated_at: "2026-10-31" },
      { id: "done", status: "succeeded", updated_at: "2026-10-31" },
      { id: "waiting", status: "queued", updated_at: "2026-10-31" },
    ],
    counts: { running: 1, queued: 125, succeeded: 14, failed: 0, partial: 0, cancelled: 0, interrupted: 0 },
    worker: { enabled: true, alive: true, active_job_id: "first" },
  });
  await Promise.all([first, second, poll]);
  assert.equal(state.jobsTotal, 140);
  assert.equal(state.jobs.length, 3);
  assert.equal(nodes["nav-job-count"].textContent, 126);
  assert.equal(nodes["stat-running"].textContent, 126);
  assert.equal(nodes["task-overview"].children[0].textContent.includes("总计 140"), true);
  assert.equal(nodes["task-overview"].children[0].textContent.includes("正在解析 1"), true);
  assert.equal(nodes["task-overview"].children[1].textContent.includes("正在处理任务：first"), true);
  assert.equal(nodes["task-overview"].warning, false);
  assert.equal(refreshed, 0);
  assert(renders > 0);

  await ctx.loadJobs(true);
  assert.equal(requests, 1, "Immediate click after polling should reuse the fresh response");
  state.shuttingDown = true;
  await ctx.loadJobs(true);
  assert.equal(requests, 1, "Shutdown must suppress polling");
  console.log("Job polling, queue counts and shutdown UI regression passed");
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
