"use strict";

// Exercise the actual report renderer and batch aggregator from app.js with a
// small DOM harness. This tests per-file evidence rather than mere HTML strings.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const script = fs.readFileSync(path.join(__dirname, "../src/reference_topology/static/app.js"), "utf8");
const html = fs.readFileSync(path.join(__dirname, "../src/reference_topology/static/index.html"), "utf8");
assert(html.includes('id="import-report"'), "The import dialog needs a report container");
function extract(start, end) {
  const a = script.indexOf(start);
  const b = script.indexOf(end, a);
  assert(a >= 0 && b > a, "Cannot extract app.js function block");
  return script.slice(a, b);
}
class Element {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.hidden = false;
    this.className = "";
    this.textContent = "";
    this.attributes = {};
    this.clicked = false;
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  click() { this.clicked = true; }
  close() { this.closed = true; }
  remove() {}
}
const report = new Element("section");
const body = new Element("body");
const dialog = new Element("dialog");
let csvBlob = null;
let selectedPaper = null;
const state = { selectedFiles: new Set(), importMode: "linked", rootId: "root-1" };
const ctx = vm.createContext({
  $: (id) => id === "import-report" ? report : id === "import-dialog" ? dialog : null,
  document: { createElement: (tag) => new Element(tag), body },
  Blob, URL: { createObjectURL: (blob) => { csvBlob = blob; return "blob:mock"; }, revokeObjectURL: () => {} },
  Date, window: { setTimeout: () => {} },
  state, crypto: { randomUUID: () => "1234" }, FormData,
  itemsOf: (data) => data.items || [],
  element: (tag, css, value) => { const node = new Element(tag); node.className = css; if (value !== undefined) node.textContent = String(value); return node; },
  button: (label, callback) => { const node = new Element("button"); node.textContent = label; node.onClick = callback; return node; },
  selectPaper: (id) => { selectedPaper = id; },
  errorMessage: (error) => error?.message || "未知错误",
  api: async () => ({}),
});
vm.runInContext(
  extract("  // Exact-content duplicate reports", "  // The API limits linked imports") +
  extract("  async function importSelectedBatches() {", "  async function submitImport() {"),
  ctx,
);
function textTree(node) {
  return [node.textContent, ...node.children.map(textTree)].join("\n");
}
const original = {
  file_id: "asset-1", paper_id: "paper-1",
  title: "Vision-based Robot Navigation", year: 2020, doi: "10.5555/example",
  file_name: "original.pdf", file_uri: "file:///C:/Papers/original.pdf",
  sha256: "a".repeat(64),
};
const reportData = {
  summary: { added: 173, duplicates: 2, failed: 1 },
  items: [
    { path: "copies/new.pdf", name: "new.pdf", duplicate: true, duplicate_reason: "identical_content", duplicate_of: original },
  ],
  skipped: [
    { path: "copies/repeated.pdf", name: "repeated.pdf", duplicate_reason: "repeated_path",
      duplicate_of: { paper_id: "paper-1", file_name: "repeated.pdf" } },
  ],
  errors: [{ path: "invalid/not-really.pdf", name: "not-really.pdf", code: "invalid_pdf",
    message: "文件不包含有效 PDF 文件头" }],
};
async function main() {
  ctx.renderImportReport(reportData);
  assert.equal(report.hidden, false);
  const allText = textTree(report);
  for (const expected of ["导入结果明细", "新增 173", "跳过重复 2", "失败/待核实 1",
    "copies/new.pdf", "copies/repeated.pdf", "Vision-based Robot Navigation",
    "10.5555/example", "original.pdf", "file:///C:/Papers/original.pdf",
    "not-really.pdf", "文件不包含有效 PDF 文件头", "invalid_pdf"]) {
    assert(allText.includes(expected), "Missing visible detail: " + expected);
  }
  assert.equal((allText.match(/查看已有文献/g) || []).length, 2);
  const button = report.children.flatMap((child) => child.children || [])
    .flatMap((child) => child.children || [])
    .flatMap((child) => child.children || [])
    .find((child) => child.textContent === "查看已有文献");
  assert(button && button.onClick, "Duplicate must link to the existing paper");
  button.onClick();
  assert.equal(selectedPaper, "paper-1");

  ctx.downloadImportReport(reportData);
  assert(csvBlob, "CSV download must create a Blob");
  const csv = await csvBlob.text();
  const csvBytes = new Uint8Array(await csvBlob.arrayBuffer());
  assert.deepEqual(Array.from(csvBytes.slice(0, 3)), [0xEF, 0xBB, 0xBF], "CSV should contain UTF-8 BOM for Excel");
  for (const expected of ["本次文件", "已有文件 URI", "copies/new.pdf",
    "copies/repeated.pdf", "file:///C:/Papers/original.pdf",
    "invalid/not-really.pdf", "invalid_pdf"]) {
    assert(csv.includes(expected), "CSV missing " + expected);
  }
  assert.equal(csv.split("\r\n").length, 4); // header + 2 duplicates + 1 failure

  ctx.clearImportReport();
  assert.equal(report.hidden, true);
  assert.equal(report.children.length, 0);

  // An interrupted batch must identify every affected path, not only "batch 2".
  const paths = Array.from({ length: 102 }, (_, n) => "pdf-" + n + ".pdf");
  state.selectedFiles = new Set(paths);
  let calls = 0;
  ctx.api = async () => {
    calls++;
    if (calls === 1) return { items: [], errors: [], skipped: [], summary: { added: 100, duplicates: 0, failed: 0 } };
    throw new Error("连接中断");
  };
  const batched = await ctx.importSelectedBatches();
  assert.equal(calls, 2);
  assert.equal(batched.errors.length, 2);
  assert.equal(batched.errors[0].path, "pdf-100.pdf");
  assert.equal(batched.errors[1].path, "pdf-101.pdf");
  assert.equal(batched.errors[1].code, "batch_status_unknown");
  assert(batched.errors[1].message.includes("是否已登记无法确认"));
  assert.equal(batched.summary.failed, 2);

  // Filenames that look like formulas must be escaped before CSV export.
  ctx.downloadImportReport({ errors: [{ path: "=1+1.pdf", code: "invalid_pdf", message: "failed" }] });
  const unsafeCSV = await csvBlob.text();
  assert(unsafeCSV.includes("\"'=1+1.pdf\""));
  console.log("Import duplicate and failure detail UI regression passed");
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
