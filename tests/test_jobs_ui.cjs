"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const js = fs.readFileSync(path.join(__dirname, "../src/reference_topology/static/app.js"), "utf8");
const html = fs.readFileSync(path.join(__dirname, "../src/reference_topology/static/index.html"), "utf8");
for (const id of ["shutdown-workspace", "task-overview", "task-status-filter",
                  "task-filter-summary", "task-pagination", "task-prev", "task-next"]) {
  assert(html.includes('id="' + id + '"'), "Missing UI control " + id);
}
assert(js.includes('api("/api/shutdown"'));
assert(js.includes('addEventListener("click", shutdownWorkspace)'));
const start = js.indexOf("  // Coalesce requests only for the SAME status/page.");
const end = js.indexOf("  function renderJobs() {", start);
assert(start >= 0 && end > start, "Cannot locate job polling functions");
class Node {
  constructor() {
    this.textContent = "";
    this.children = [];
    this.hidden = false;
    this.disabled = false;
    this.value = "all";
    this.classList = { toggle: (_, active) => { this.warning = Boolean(active); } };
  }
  replaceChildren(...children) { this.children = children; }
}
const ids = ["nav-job-count", "stat-running", "task-overview",
  "task-status-filter", "task-filter-summary", "task-pagination",
  "task-page-summary", "task-prev", "task-next"];
const nodes = Object.fromEntries(ids.map((name) => [name, new Node()]));
nodes["task-status-filter"].options = [
  "all", "queued", "running", "succeeded", "partial",
  "failed", "interrupted", "cancelled"
].map((value) => ({ value, textContent: "" }));
const state = {
  view: "tasks", initialized: true, jobs: [], jobsCounts: {},
  jobsSignature: "", jobsTotal: 0, jobsWorker: null, shuttingDown: false,
  jobFilter: "all", jobOffset: 0, jobPageSize: 30, jobsFilteredTotal: 0,
};
const pending = [];
const urls = [];
let renders = 0, refreshed = 0;
const ctx = vm.createContext({
  state, URLSearchParams, Date,
  $: (key) => {
    assert(nodes[key], "Unknown node " + key);
    return nodes[key];
  },
  element: (_, css, value) => ({ className: css, textContent: value || "", children: [] }),
  STATUS: Object.fromEntries(nodes["task-status-filter"].options.map(o => [o.value, [o.value, ""]])),
  api: async (url) => {
    urls.push(url);
    return new Promise((resolve) => pending.push(resolve));
  },
  itemsOf: (result) => result.items,
  renderJobs: () => { renders++; },
  refreshAfterChange: async () => { refreshed++; },
});
vm.runInContext(js.slice(start, end), ctx);
const counts = { running: 1, queued: 90, succeeded: 14,
  failed: 34, partial: 0, cancelled: 0, interrupted: 1 };
const data = (items, status, offset, filteredTotal) => ({
  total: 140, items, status, offset, limit: 30, filtered_total: filteredTotal,
  counts, worker: { enabled: true, alive: true, active_job_id: "first" },
});
async function main() {
  const first = ctx.loadJobs(true);
  const second = ctx.loadJobs(true);
  const poll = ctx.loadJobs();
  assert.equal(urls.length, 1, "Concurrent refresh and poll must share a request");
  assert(urls[0].includes("status=all") && urls[0].includes("limit=30"));
  pending.shift()(data([
    { id: "first", status: "running", progress_current: 1, progress_total: 3 },
    { id: "done", status: "succeeded" }, { id: "waiting", status: "queued" },
  ], "all", 0, 140));
  await Promise.all([first, second, poll]);
  assert.equal(state.jobsTotal, 140);
  assert.equal(state.jobsFilteredTotal, 140);
  assert.equal(nodes["nav-job-count"].textContent, 91);
  assert(nodes["task-overview"].children[0].textContent.includes("总计 140"));
  assert(nodes["task-filter-summary"].textContent.includes("符合条件 140 项"));
  assert.equal(nodes["task-next"].disabled, false);
  assert.equal(nodes["task-prev"].disabled, true);
  assert(nodes["task-status-filter"].options[5].textContent.includes("34"));
  assert(renders > 0);
  assert.equal(refreshed, 0);
  await ctx.loadJobs(true);
  assert.equal(urls.length, 1, "Immediate refresh reuses recent response");

  state.jobFilter = "failed";
  state.jobOffset = 0;
  const filtered = ctx.loadJobs(true);
  assert.equal(urls.length, 2);
  assert(urls[1].includes("status=failed"));
  pending.shift()(data([{ id: "failed-1", status: "failed" }], "failed", 0, 34));
  await filtered;
  assert.deepEqual(state.jobs.map((job) => job.id), ["failed-1"]);
  assert(nodes["task-filter-summary"].textContent.includes("符合条件 34 项"));
  assert.equal(nodes["task-pagination"].hidden, false);
  assert.equal(nodes["task-page-summary"].textContent, "第 1 / 2 页");

  state.jobOffset = 30;
  const nextPage = ctx.loadJobs(true);
  assert(urls[2].includes("status=failed") && urls[2].includes("offset=30"));
  pending.shift()(data([{ id: "failed-31", status: "failed" }], "failed", 30, 34));
  await nextPage;
  assert.equal(nodes["task-prev"].disabled, false);
  assert.equal(nodes["task-next"].disabled, true);
  assert.equal(nodes["task-page-summary"].textContent, "第 2 / 2 页");

  state.jobFilter = "queued";
  state.jobOffset = 0;
  const old = ctx.loadJobs(true);
  state.jobFilter = "succeeded";
  const newest = ctx.loadJobs(true);
  assert.equal(urls.length, 5);
  const resolveOld = pending.shift();
  const resolveNewest = pending.shift();
  resolveNewest(data([{ id: "success-1", status: "succeeded" }], "succeeded", 0, 14));
  await newest;
  resolveOld(data([{ id: "old-queued", status: "queued" }], "queued", 0, 90));
  await old;
  assert.equal(state.jobFilter, "succeeded");
  assert.deepEqual(state.jobs.map((job) => job.id), ["success-1"]);
  assert.equal(nodes["task-status-filter"].value, "succeeded");
  assert.equal(nodes["task-pagination"].hidden, true);

  state.shuttingDown = true;
  await ctx.loadJobs(true);
  assert.equal(urls.length, 5);
  console.log("Task filtering, full counts, pagination and stale responses passed");
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
