#!/usr/bin/env node
/*
 * Headless UI smoke test using jsdom against the running stack.
 *
 * jsdom executes the real app.js and talks to the real UI proxy, so this catches
 * DOM/render errors that syntax checks miss (a full browser is not available in
 * this sandbox: no libatomic/root).
 *
 * Usage: node scripts/ui_smoke.mjs --base http://127.0.0.1:8800
 */
import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { JSDOM, VirtualConsole } = require("/tmp/uitest/node_modules/jsdom");

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");
const baseArg = process.argv.indexOf("--base");
const BASE = baseArg > 0 ? process.argv[baseArg + 1] : "http://127.0.0.1:8800";

const errors = [];
const checks = [];
const HARD_DEADLINE = setTimeout(() => {
  console.error("UI smoke timed out (global deadline)");
  process.exit(2);
}, 120000);
HARD_DEADLINE.unref();
function check(name, ok, detail = "") {
  checks.push({ name, ok, detail });
  console.log(`${ok ? "  ✓" : "  ✗"} ${name}${ok ? "" : " " + detail}`);
}

const virtualConsole = new VirtualConsole();
virtualConsole.on("jsdomError", (e) => errors.push(String(e && e.stack || e)));
virtualConsole.on("error", (...a) => errors.push(a.map(String).join(" ")));

const html = fs.readFileSync(path.join(ROOT, "app/ui/index.html"), "utf8");
const appjs = fs.readFileSync(path.join(ROOT, "app/ui/app.js"), "utf8");

const dom = new JSDOM(html, {
  url: BASE + "/",
  runScripts: "outside-only",
  pretendToBeVisual: true,
  virtualConsole,
});
const { window } = dom;

// Route fetch through the real server, resolving relative URLs against the page.
window.fetch = async (url, opts) => {
  const full = String(url).startsWith("http") ? url : BASE + url;
  return fetch(full, opts);
};
window.confirm = () => true;
window.alert = () => {};

try {
  window.eval(appjs);
} catch (e) {
  console.error("app.js threw during evaluation:", e);
  process.exit(1);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function waitFor(desc, predicate, timeout = 15000) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    try {
      if (predicate()) return true;
    } catch { /* transient */ }
    await sleep(100);
  }
  return false;
}

function clickTab(name) {
  const btn = window.document.querySelector(`#tabs button[data-tab="${name}"]`);
  btn.dispatchEvent(new window.Event("click", { bubbles: true }));
}

const view = () => window.document.querySelector("#view");

console.log("== UI smoke");
window.document.dispatchEvent(new window.Event("DOMContentLoaded"));
let ok = await waitFor("overview", () => view().textContent.includes("活动原值记录"));
check("overview renders", ok, view().textContent.slice(0, 300));
check("header shows generation pill", window.document.querySelector("#gen-pill").textContent.includes("规则"));
check("header shows raw count", /原值/.test(window.document.querySelector("#raw-pill").textContent));

// rules tab
clickTab("rules");
ok = await waitFor("rules", () => window.document.querySelector(".steps-editor"));
check("rules editor renders step rows", ok && window.document.querySelectorAll(".steps-editor .step").length > 0);
if (ok) {
  const rows = window.document.querySelectorAll("#gen-list tr").length;
  check("generation list populated", rows >= 1, String(rows));
  // The initially selected generation may be published (read-only). Fork a draft
  // via the UI so the editable path is exercised.
  if (!window.document.querySelector("#add-step") || window.document.querySelector("#add-step").disabled) {
    window.document.querySelector("#new-gen").dispatchEvent(new window.Event("click", { bubbles: true }));
    await sleep(200);
    check("'new draft' makes the editor editable",
      !!window.document.querySelector("#add-step") && !window.document.querySelector("#add-step").disabled);
  } else {
    check("'new draft' makes the editor editable", true, "already editable");
  }
  // Ensure at least three steps exist, then move a step down and verify state survives re-render.
  let guard = 0;
  while (window.document.querySelectorAll(".steps-editor .step").length < 3 && guard++ < 10) {
    // the editor re-renders on every change, so re-query the button each time
    window.document.querySelector("#add-step").dispatchEvent(new window.Event("click", { bubbles: true }));
    await sleep(120);
  }
  const steps = Array.from(window.document.querySelectorAll(".steps-editor .step"));
  const movable = steps.find((s) => {
    const btn = s.querySelector('[data-move="down"]');
    return btn && !btn.disabled;
  });
  if (movable) {
    const idx = Array.from(steps).indexOf(movable);
    const firstOp = movable.querySelector("select").value;
    const secondOp = steps[idx + 1].querySelector("select").value;
    movable.querySelector('[data-move="down"]').dispatchEvent(new window.Event("click", { bubbles: true }));
    await sleep(200);
    const after = Array.from(window.document.querySelectorAll(".steps-editor .step"));
    const movedOp = after[idx]?.querySelector("select")?.value;
    check("step reorder re-renders editor", after.length === steps.length && movedOp === secondOp,
      `${firstOp},${secondOp} -> ${movedOp}`);
    check("dirty badge shown after edit",
      window.document.querySelector("#draft-state").textContent.includes("未保存"));
  } else {
    check("step reorder re-renders editor", false, "no movable step");
    check("dirty badge shown after edit", false, "no movable step");
  }
}

// preview panel (pure engine call)
if (ok) {
  const previewBtn = window.document.querySelector("#preview-rule");
  previewBtn.dispatchEvent(new window.Event("click", { bubbles: true }));
  await waitFor("preview panel", () => window.document.querySelector("#run-preview"));
  const run = window.document.querySelector("#run-preview");
  check("preview panel opens", !!run);
  if (run) {
    run.dispatchEvent(new window.Event("click", { bubbles: true }));
    const rendered = await waitFor("preview results", () => {
      const box = window.document.querySelector("#preview-results");
      return box && box.querySelectorAll(".step").length >= 5;
    });
    const box = window.document.querySelector("#preview-results");
    check("preview renders per-value evidence", rendered, box ? box.textContent.slice(0, 200) : "no box");
    check("preview shows codepoint evidence", box && /U\+FF21|U\+3000/.test(box.textContent));
  }
}

// conflicts tab
clickTab("conflicts");
ok = await waitFor("conflicts", () => view().textContent.includes("碰撞证据与人工裁决")
  || view().textContent.includes("尚无关联发布"));
check("conflicts view loads", ok, view().textContent.slice(0, 300));
const hasConflicts = window.document.querySelectorAll("[data-act]").length > 0;
check("collision cards render with adjudication buttons", hasConflicts || view().textContent.includes("没有碰撞"));

// builds tab
clickTab("builds");
ok = await waitFor("builds", () => view().textContent.includes("构建关联结果"));
check("builds view loads", ok, view().textContent.slice(0, 300));
check("build mode selector present", !!window.document.querySelector("#build-mode"));

// reports tab
clickTab("reports");
ok = await waitFor("reports", () => view().textContent.includes("关联报告") || view().textContent.includes("尚无关联发布"));
check("reports view loads", ok, view().textContent.slice(0, 400));
if (ok && window.document.querySelector("#class-list table")) {
  check("class table renders", window.document.querySelectorAll("#class-list tbody tr").length > 0);
  const firstDetail = window.document.querySelector("#class-list [data-class]");
  if (firstDetail) {
    firstDetail.dispatchEvent(new window.Event("click", { bubbles: true }));
    const modalOk = await waitFor("class modal", () => window.document.querySelector(".modal"));
    check("class detail modal opens", modalOk);
    const evBtn = window.document.querySelector(".modal [data-evidence]");
    if (evBtn) {
      evBtn.dispatchEvent(new window.Event("click", { bubbles: true }));
      const evOk = await waitFor("evidence modal", () =>
        Array.from(window.document.querySelectorAll(".modal")).some((m) => m.textContent.includes("逐字符证据")));
      check("per-character evidence modal opens from a member", evOk);
    } else {
      check("per-character evidence modal opens from a member", false, "no evidence button");
    }
  }
}

// intake tab
clickTab("intake");
ok = await waitFor("intake", () => view().textContent.includes("录入原值"));
check("intake view loads", ok, view().textContent.slice(0, 200));
check("sample data prefilled", (window.document.querySelector("#intake-text")?.value || "").includes("raw_value"));
check("raw list shows verbatim values", /ＡＣＭＥ|ACME/.test(window.document.querySelector("#raw-list")?.textContent || ""));

const failed = checks.filter((c) => !c.ok);
console.log(`\nUI RESULT: ${checks.length - failed.length} passed, ${failed.length} failed`);
if (errors.length) {
  console.log("\nJS errors:");
  errors.slice(0, 20).forEach((e) => console.log("  " + e.split("\n")[0]));
}
process.exit(failed.length === 0 && errors.length === 0 ? 0 : 1);
