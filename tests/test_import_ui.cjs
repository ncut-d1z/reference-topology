"use strict";

// Minimal DOM harness with no npm dependency. Execute the actual browser
// selection and directory-browse functions from app.js, not a copied rewrite.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const script = fs.readFileSync(path.join(__dirname, "../src/reference_topology/static/app.js"), "utf8");
const html = fs.readFileSync(path.join(__dirname, "../src/reference_topology/static/index.html"), "utf8");
assert(html.includes('id="browse-select-all"'));
assert(script.includes('$("browse-select-all").addEventListener("click", toggleSelectAllVisible)'));

function extract(start, end) {
  const a = script.indexOf(start);
  const b = script.indexOf(end, a);
  assert(a >= 0 && b > a, "Browser function block was not found");
  return script.slice(a, b);
}

class FakeElement {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.dataset = {};
    this.attributes = {};
    this.events = {};
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this.textContent = "";
  }
  append(...children) {
    for (const child of children) {
      if (child.tag === "fragment") this.children.push(...child.children);
      else this.children.push(child);
    }
  }
  replaceChildren(...children) {
    this.children = [];
    this.append(...children);
  }
  querySelectorAll(selector) {
    assert.equal(selector, 'input[type="checkbox"][data-pdf-path]');
    const results = [];
    const visit = (node) => {
      if (node.tag === "input" && node.type === "checkbox" && Object.hasOwn(node.dataset, "pdfPath")) {
        results.push(node);
      }
      for (const child of node.children || []) visit(child);
    };
    visit(this);
    return results;
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(name, callback) { this.events[name] = callback; }
}

const nodes = Object.fromEntries(["file-browser", "browse-select-all", "browse-path",
  "browse-up", "import-selection", "submit-import"].map((id) => [id, new FakeElement("div")]));
nodes["browse-select-all"].disabled = true;

const state = {
  rootId: "root-1", browsePath: "", browseParent: null, browseRequest: 0,
  visiblePdfPaths: [], selectedFiles: new Set(), rebindFile: null,
  importMode: "linked", importBusy: false, uploads: [],
};
let lastError = null;

const context = vm.createContext({
  state, URLSearchParams, document: {
    createDocumentFragment: () => new FakeElement("fragment"),
    createElement: (tag) => new FakeElement(tag),
  },
  $: (id) => nodes[id],
  api: async () => ({ path: "", parent: null, items: [] }),
  element: (tag, css, text) => {
    const el = new FakeElement(tag);
    el.className = css;
    if (text !== undefined) el.textContent = String(text);
    return el;
  },
  button: (label, onClick) => {
    const el = new FakeElement("button");
    el.textContent = label;
    if (onClick) el.addEventListener("click", onClick);
    return el;
  },
  icon: (name) => new FakeElement("svg"),
  sizeText: (n) => String(n),
  emptyState: (node) => node.replaceChildren(new FakeElement("div")),
  showError: (_id, error) => { lastError = error; },
  clearError: () => { lastError = null; },
  itemsOf: (result) => result.items || [],
});
vm.runInContext(
  extract("  async function browseFiles(path) {", "  function addUploads(files) {") +
  extract("  function visiblePdfCheckboxes() {", "  // The API limits linked imports"),
  context,
);

async function main() {
  const data = {
    path: "", parent: null,
    items: [
      { type: "directory", name: "subfolder", path: "subfolder" },
      { type: "pdf", name: "A.pdf", path: "A.pdf", size: 111 },
      { type: "pdf", name: "B.pdf", path: "B.pdf", size: 222 },
    ],
  };
  context.api = async () => data;
  await context.browseFiles("");

  const control = nodes["browse-select-all"];
  assert.equal(control.disabled, false, "Available PDF entries must enable the button");
  assert.equal(control.textContent, "全选");
  assert.equal(state.visiblePdfPaths.length, 2);

  context.toggleSelectAllVisible();
  assert.deepEqual(Array.from(state.selectedFiles), ["A.pdf", "B.pdf"]);
  assert.equal(control.disabled, false, "Selected PDFs must not disable deselect-all");
  assert.equal(control.textContent, "取消全选");
  assert.equal(nodes["import-selection"].textContent, "已选择 2 份 PDF");
  assert(nodes["file-browser"].querySelectorAll('input[type="checkbox"][data-pdf-path]').every((box) => box.checked));

  context.toggleSelectAllVisible();
  assert.equal(state.selectedFiles.size, 0);
  assert.equal(control.textContent, "全选");
  assert(nodes["file-browser"].querySelectorAll('input[type="checkbox"][data-pdf-path]').every((box) => !box.checked));

  // Changing to a directory containing no PDFs must disable the action.
  context.api = async () => ({ path: "empty", parent: "", items: [{ type: "directory", path: "child", name: "child" }] });
  await context.browseFiles("empty");
  assert.equal(control.disabled, true);

  context.api = async () => data;
  await context.browseFiles("");
  state.rebindFile = "asset-id";
  context.updateSelectAllButton();
  assert.equal(control.hidden, true);
  assert.equal(control.disabled, true);
  state.rebindFile = null;
  context.updateSelectAllButton();
  assert.equal(control.disabled, false);

  state.importBusy = true;
  context.updateSelectAllButton();
  assert.equal(control.disabled, true);
  state.importBusy = false;

  // A late response from a previous directory must not override current state.
  let releaseOld;
  context.api = (url) => url.includes("path=old")
    ? new Promise((resolve) => { releaseOld = resolve; })
    : Promise.resolve({ path: "new", parent: "", items: [{ type: "pdf", path: "new.pdf", name: "new.pdf", size: 1 }] });
  const first = context.browseFiles("old");
  await Promise.resolve();
  const second = context.browseFiles("new");
  await second;
  releaseOld({ path: "old", parent: "", items: [{ type: "pdf", path: "old.pdf", name: "old.pdf", size: 1 }] });
  await first;
  assert.equal(state.browsePath, "new");
  assert.deepEqual(Array.from(state.visiblePdfPaths), ["new.pdf"]);
  assert.equal(control.disabled, false);

  context.api = async () => { throw new Error("network failure"); };
  await context.browseFiles("error");
  assert.equal(control.disabled, true);
  assert.equal(lastError.message, "network failure");

  console.log("Import select-all state and browse regression tests passed");
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
