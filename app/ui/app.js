/* NormLink operator console (vanilla JS, no build step). */
"use strict";

const API = {
  raw: (p) => "/api/raw" + p,
  norm: (p) => "/api/norm" + p,
  link: (p) => "/api/link" + p,
};

const state = {
  tab: "overview",
  header: { gen: null, raw: null, release: null },
  ruleDraft: null,
  selectGenerationId: null,
  buildPoll: null,
  releaseId: null,
  rulesDirty: false,
};

/* Decode \\uXXXX, \\n, \\t, \\r, \\\\ escapes so whitespace-sensitive aliases are expressible. */
function unescapeText(text) {
  return text.replace(/\\(u\{[0-9a-fA-F]+\}|u[0-9a-fA-F]{4}|n|r|t|\\)/g, (m, g) => {
    if (g === "n") return "\n";
    if (g === "r") return "\r";
    if (g === "t") return "\t";
    if (g === "\\") return "\\";
    if (g.startsWith("u{")) return String.fromCodePoint(parseInt(g.slice(2, -1), 16));
    return String.fromCharCode(parseInt(g.slice(1), 16));
  });
}

/* Split on the first unescaped "=>". */
function splitAlias(line) {
  for (let i = 0; i < line.length - 1; i++) {
    if (line[i] === "\\") { i++; continue; }
    if (line[i] === "=" && line[i + 1] === ">") return [line.slice(0, i), line.slice(i + 2)];
  }
  return null;
}

/* ------------------------------------------------------------------ utils */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function esc(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function fmt(n) {
  if (n === null || n === undefined) return "—";
  return Number(n).toLocaleString("zh-CN");
}

function shortHash(h, n = 12) {
  return h ? h.slice(0, n) : "—";
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("zh-CN", { hour12: false });
}

function badge(status) {
  const labels = {
    clean: "已归类",
    conflict: "碰撞隔离",
    conflict_needs_review: "需复核",
    hold: "人工保持隔离",
    empty: "空键",
    adjudicated_merge: "人工合并",
    adjudicated_split: "人工拆分",
    preview: "预览",
    active: "生效中",
    superseded: "已取代",
    staging: "暂存",
    completed: "完成",
    running: "运行中",
    queued: "排队",
    cancelled: "已取消",
    failed: "失败",
    stale: "过期",
    revoked: "已撤销",
  };
  return `<span class="badge ${esc(status)}">${esc(labels[status] || status)}</span>`;
}

function toast(message, kind = "") {
  const wrap = $("#toasts");
  const node = document.createElement("div");
  node.className = "toast " + kind;
  node.textContent = message;
  wrap.appendChild(node);
  setTimeout(() => node.remove(), kind === "bad" ? 9000 : 4500);
}

async function call(base, path, options = {}) {
  const opts = { method: options.method || "GET", headers: {} };
  if (options.body !== undefined) {
    opts.headers["Content-Type"] = "application/json; charset=utf-8";
    opts.body = typeof options.body === "string" ? options.body : JSON.stringify(options.body);
  }
  const resp = await fetch(base(path), opts);
  const ctype = resp.headers.get("Content-Type") || "";
  let data;
  if (ctype.includes("application/json")) data = await resp.json();
  else data = await resp.text();
  if (!resp.ok) {
    const msg = (data && data.error) || (typeof data === "string" ? data : `HTTP ${resp.status}`);
    const detail = data && data.detail ? `（${typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)}）` : "";
    throw new Error(msg + detail);
  }
  return data;
}

function download(path, filename) {
  const a = document.createElement("a");
  a.href = path;
  a.download = filename || "";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function modal(title, bodyHtml, footHtml = "") {
  const back = document.createElement("div");
  back.className = "modal-back";
  back.innerHTML = `
    <div class="modal" role="dialog" aria-modal="true">
      <header><h2>${esc(title)}</h2><button class="ghost close" aria-label="关闭">✕</button></header>
      <div class="body"></div>
      ${footHtml ? `<div class="body" style="border-top:1px solid var(--line)">${footHtml}</div>` : ""}
    </div>`;
  $(".body", back).innerHTML = bodyHtml;
  const close = () => back.remove();
  $(".close", back).addEventListener("click", close);
  back.addEventListener("click", (e) => { if (e.target === back) close(); });
  document.body.appendChild(back);
  return { node: back, close, body: $(".body", back) };
}

/* ----------------------------------------------------------------- header */
async function refreshHeader() {
  try {
    const gen = await call(API.norm, "/rules/current");
    state.header.gen = gen;
    $("#gen-pill").className = "pill ok";
    $("#gen-pill").innerHTML = `<span class="dot"></span>规则 #${gen.generation_id} · ${esc(gen.label)}`;
  } catch (e) {
    $("#gen-pill").className = "pill bad";
    $("#gen-pill").textContent = "规则世代不可用";
  }
  try {
    const snap = await call(API.raw, "/snapshot");
    state.header.raw = snap;
    $("#raw-pill").className = "pill " + (snap.active_count ? "" : "warn");
    $("#raw-pill").innerHTML = `<span class="dot"></span>原值 ${fmt(snap.active_count)} 条`;
  } catch (e) {
    $("#raw-pill").className = "pill bad";
    $("#raw-pill").textContent = "原值服务不可用";
  }
  try {
    const rel = await call(API.link, "/releases/active");
    state.header.release = rel;
    $("#rel-pill").className = "pill ok";
    $("#rel-pill").innerHTML = `<span class="dot"></span>发布 ${esc(rel.id)} · ${fmt(rel.class_count)} 类`;
  } catch (e) {
    state.header.release = null;
    $("#rel-pill").className = "pill warn";
    $("#rel-pill").textContent = "尚无已发布关联结果";
  }
}

/* ------------------------------------------------------------------ tabs */
function setTab(tab) {
  state.tab = tab;
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  const view = $("#view");
  view.innerHTML = `<div class="panel"><span class="spinner"></span> 加载中…</div>`;
  const renderer = {
    overview: renderOverview,
    rules: renderRules,
    conflicts: renderConflicts,
    builds: renderBuilds,
    reports: renderReports,
    intake: renderIntake,
  }[tab];
  Promise.resolve(renderer()).catch((e) => {
    view.innerHTML = `<div class="banner bad">加载失败：${esc(e.message)}</div>`;
  });
}

/* -------------------------------------------------------------- overview */
async function renderOverview() {
  const [snap, rules, ov, releases] = await Promise.all([
    call(API.raw, "/snapshot"),
    call(API.norm, "/rules"),
    call(API.link, "/overview"),
    call(API.link, "/releases?limit=8"),
  ]);
  const gen = rules.current;
  const active = ov.active_release;
  const conflict = active ? active.conflict_class_count : 0;
  const view = $("#view");
  view.innerHTML = `
    <div class="grid c4 mb">
      <div class="stat"><div class="label">活动原值记录</div><div class="value">${fmt(snap.active_count)}</div>
        <div class="note">${fmt(snap.sources.length)} 个来源系统 · ${fmt(snap.total_count)} 条历史修订</div></div>
      <div class="stat"><div class="label">当前规则世代</div><div class="value">#${esc(gen.generation_id)}</div>
        <div class="note">${esc(gen.label)} · ${shortHash(gen.steps_hash)}</div></div>
      <div class="stat"><div class="label">生效发布等价类</div><div class="value">${active ? fmt(active.class_count) : "—"}</div>
        <div class="note">${active ? esc(active.id) : "尚未发布关联结果"}</div></div>
      <div class="stat ${conflict ? "warn" : ""}"><div class="label">碰撞隔离类</div><div class="value">${active ? fmt(conflict) : "—"}</div>
        <div class="note">${active ? `涉及 ${fmt(active.member_conflict_count)} 条原值 · 未自动合并` : "—"}</div></div>
    </div>
    <div class="grid c2">
      <div class="panel">
        <h2>当前状态</h2>
        <p class="hint">三个服务独立运行；关联报告固定在发布时使用的规则世代上。</p>
        <dl class="kv">
          <dt>规则世代</dt><dd>#${esc(gen.generation_id)} · ${esc(gen.label)}</dd>
          <dt>步骤哈希</dt><dd>${esc(gen.steps_hash)}</dd>
          <dt>发布世代</dt><dd>${active ? "#" + esc(active.generation_id) + " · " + esc(active.generation_label) : "—"}</dd>
          <dt>发布步骤哈希</dt><dd>${active ? esc(active.generation_hash) : "—"}</dd>
          <dt>发布报告指纹</dt><dd>${active ? esc(active.report_sha256) : "—"}</dd>
          <dt>规则与发布一致</dt><dd>${active ? (active.generation_hash === gen.steps_hash ? "是" : "否（需重建或审阅激活）") : "—"}</dd>
          <dt>待生效裁决</dt><dd>${fmt(ov.pending_adjudications)}</dd>
          <dt>活动构建</dt><dd>${ov.active_jobs.length ? ov.active_jobs.map((j) => esc(j.id) + " " + esc(j.status)).join("<br>") : "无"}</dd>
        </dl>
        <div class="actions mt">
          <button class="primary" onclick="setTab('rules')">管理规则</button>
          <button onclick="setTab('builds')">构建 / 发布</button>
          <button onclick="setTab('conflicts')">审阅碰撞</button>
        </div>
      </div>
      <div class="panel">
        <h2>最近发布</h2>
        <p class="hint">每次发布对应一次固定世代的规范化运行；历史报告不随规则变化而改写。</p>
        <table>
          <thead><tr><th>发布</th><th>状态</th><th>世代</th><th class="num">类</th><th class="num">碰撞</th><th>时间</th></tr></thead>
          <tbody>
            ${releases.releases.map((r) => `
              <tr>
                <td><a href="#" onclick="openReport('${esc(r.id)}');return false" class="mono">${esc(r.id)}</a></td>
                <td>${badge(r.status)}</td>
                <td>#${esc(r.generation_id)}</td>
                <td class="num">${fmt(r.class_count)}</td>
                <td class="num">${fmt(r.conflict_class_count)}</td>
                <td class="small muted">${fmtTime(r.published_at || r.created_at)}</td>
              </tr>`).join("") || `<tr><td colspan="6" class="muted">暂无发布</td></tr>`}
          </tbody>
        </table>
      </div>
    </div>
    <div class="panel mt">
      <h2>使用顺序</h2>
      <div class="grid c4">
        <div><h3>1 · 录入原值</h3><p class="small muted">从各系统接收原始字符串，按来源系统与外部 ID 保存修订，不做任何规范化。</p></div>
        <div><h3>2 · 定规则并预览</h3><p class="small muted">编辑步骤、看逐字符证据；发布后世代不可编辑。关联表全部固定在同一世代。</p></div>
        <div><h3>3 · 构建与审阅</h3><p class="small muted">全量或增量构建；碰撞键进入隔离区，附证据与来源，不自动吞掉冲突行。</p></div>
        <div><h3>4 · 裁决与发布</h3><p class="small muted">人工合并/拆分/保持隔离；预览对比影响后激活发布。</p></div>
      </div>
    </div>`;
}

/* ----------------------------------------------------------------- rules */
const OPS = [
  ["unicode_nfkc", "Unicode NFKC 兼容折叠"],
  ["unicode_nfc", "Unicode NFC"],
  ["unicode_nfd", "Unicode NFD"],
  ["unicode_nfkd", "Unicode NFKD"],
  ["strip_diacritics", "去除变音符号"],
  ["strip_format", "去除格式字符（零宽等）"],
  ["strip_control", "去除控制/格式字符"],
  ["strip_punct", "去除标点"],
  ["trim", "去除首尾空白"],
  ["collapse_ws", "折叠连续空白"],
  ["remove_ws", "移除所有空白"],
  ["casefold", "Unicode casefold"],
  ["lower", "转小写"],
  ["upper", "转大写"],
  ["replace_map", "整值别名映射"],
  ["regex_replace", "正则替换"],
];

/* Encode only what would otherwise be unreadable or split a line. */
function escapeAlias(text) {
  return Array.from(String(text)).map((ch) => {
    if (ch === "\\") return "\\\\";
    if (ch === "\n") return "\\n";
    if (ch === "\r") return "\\r";
    if (ch === "\t") return "\\t";
    const code = ch.codePointAt(0);
    const isControl = code < 0x20 || (code >= 0x7f && code <= 0x9f);
    const isInvisible = ch.length && /[\p{Cf}\p{Zl}\p{Zp}]/u.test(ch);
    if (isControl || isInvisible) return "\\u" + code.toString(16).padStart(4, "0");
    return ch;
  }).join("");
}

function stepSummary(step) {
  if (step.op === "replace_map") return `${step.op} · ${Object.keys(step.map || {}).length} 条别名`;
  if (step.op === "regex_replace") return `${step.op} · /${step.pattern}/${step.flags || ""}`;
  if (step.op === "strip_punct") return `${step.op} · keep=${JSON.stringify(step.keep || "")}`;
  return step.op;
}

function stepEditorHtml(step, idx) {
  let params = "";
  if (step.op === "replace_map") {
    const lines = Object.entries(step.map || {})
      .map(([k, v]) => `${escapeAlias(k)} => ${escapeAlias(v)}`).join("\n");
    params = `<label class="field"><span>整值别名（每行 “原值 =&gt; 规范值”，支持 \\uXXXX / \\n / \\t 转义，仅匹配完整值）</span>
      <textarea data-param="map" rows="4" spellcheck="false">${esc(lines)}</textarea></label>`;
  } else if (step.op === "regex_replace") {
    params = `<div class="row mt">
      <label class="field grow"><span>pattern</span><input type="text" data-param="pattern" value="${esc(step.pattern || "")}"></label>
      <label class="field grow"><span>replacement</span><input type="text" data-param="replacement" value="${esc(step.replacement || "")}"></label>
      <label class="field" style="width:90px"><span>flags</span><input type="text" data-param="flags" value="${esc(step.flags || "")}" placeholder="imsx"></label>
    </div>`;
  } else if (step.op === "strip_punct") {
    params = `<div class="row mt">
      <label class="field grow"><span>保留字符</span><input type="text" data-param="keep" value="${esc(step.keep || "")}"></label>
      <label class="field"><span>同时去符号</span><select data-param="also_symbols">
        <option value="0"${step.also_symbols ? "" : " selected"}>否</option>
        <option value="1"${step.also_symbols ? " selected" : ""}>是</option></select></label>
    </div>`;
  }
  return `<div class="step" data-idx="${idx}">
    <div class="step-head">
      <span class="idx">#${idx + 1}</span>
      <select data-param="op">${OPS.map(([op, label]) => `<option value="${op}"${step.op === op ? " selected" : ""}>${label}</option>`).join("")}</select>
      <span class="spacer"></span>
      <button class="sm ghost" data-move="up" ${idx === 0 ? "disabled" : ""}>↑</button>
      <button class="sm ghost" data-move="down" ${idx === state.ruleDraft.steps.length - 1 ? "disabled" : ""}>↓</button>
      <button class="sm danger" data-del="1">删除</button>
    </div>
    ${params}
    <label class="field mt"><span>备注（可留空，写入证据）</span>
      <input type="text" data-param="note" value="${esc(step.note || "")}"></label>
  </div>`;
}

function collectSteps() {
  const draft = state.ruleDraft;
  const steps = [];
  for (const node of $$(".steps-editor .step")) {
    let step = { op: $("[data-param=op]", node).value };
    const note = $("[data-param=note]", node).value.trim();
    if (note) step.note = note;
    if (step.op === "replace_map") {
      const map = {};
      const text = $("[data-param=map]", node).value;
      for (const line of text.split("\n")) {
        if (!line.trim()) continue;
        const parts = splitAlias(line);
        if (!parts) throw new Error(`别名行缺少 “=>”: ${line}`);
        const key = unescapeText(parts[0].trim());
        const value = unescapeText(parts[1].trim());
        if (!key) throw new Error(`别名行原值为空: ${line}`);
        if (key in map && map[key] !== value) throw new Error(`别名原值重复且目标不一致: ${key}`);
        map[key] = value;
      }
      step.map = map;
    } else if (step.op === "regex_replace") {
      step.pattern = $("[data-param=pattern]", node).value;
      step.replacement = $("[data-param=replacement]", node).value;
      const flags = $("[data-param=flags]", node).value.trim();
      if (flags) step.flags = flags;
    } else if (step.op === "strip_punct") {
      step.keep = $("[data-param=keep]", node).value;
      if ($("[data-param=also_symbols]", node).value === "1") step.also_symbols = true;
    }
    steps.push(step);
  }
  return steps;
}

function ruleDraftFromGeneration(gen) {
  return {
    generation_id: gen.generation_id,
    status: gen.status,
    label: gen.label,
    note: gen.note || "",
    steps_hash: gen.steps_hash,
    steps: JSON.parse(JSON.stringify(gen.steps)),
    readonly: gen.status !== "draft",
    basedOn: gen.status === "draft" ? gen.generation_id : null,
  };
}

async function renderRules() {
  const data = await call(API.norm, "/rules");
  const selected = state.selectGenerationId
    ? data.generations.find((g) => g.generation_id === state.selectGenerationId)
    : null;
  state.ruleDraft = ruleDraftFromGeneration(selected || data.current);
  state.rulesDirty = false;
  const view = $("#view");
  view.innerHTML = `
    <div class="grid c2" style="grid-template-columns: 330px 1fr">
      <div class="panel">
        <div class="row mb"><h2 class="grow">规则世代</h2><button class="sm primary" id="new-gen">新建草稿</button></div>
        <p class="hint">已发布世代只读；编辑会另存为新草稿。发布后旧世代保留，历史发布不改变。</p>
        <table><thead><tr><th>#</th><th>标签</th><th>状态</th></tr></thead><tbody id="gen-list"></tbody></table>
      </div>
      <div class="panel">
        <div class="row mb">
          <div class="grow"><h2 id="editor-title">规则编辑</h2><p class="hint" id="editor-sub"></p></div>
          <span id="draft-state" class="pill"></span>
        </div>
        <div id="editor-body"></div>
        <div class="actions mt">
          <button class="primary" id="save-rule">保存草稿</button>
          <button id="fork-rule">另存为新草稿</button>
          <button id="publish-rule" class="danger">发布此世代</button>
          <span class="spacer"></span>
          <button id="preview-rule" class="ghost">预览映射</button>
        </div>
        <div id="preview-panel" class="mt"></div>
      </div>
    </div>`;

  const list = $("#gen-list");
  list.innerHTML = data.generations.map((g) => `
    <tr data-gen="${g.generation_id}" style="cursor:pointer">
      <td>${g.generation_id}</td>
      <td>${esc(g.label)}<div class="tiny muted">${fmtTime(g.published_at || g.created_at)}</div></td>
      <td>${g.status === "published" ? '<span class="badge active">当前发布</span>' : badge(g.status) }</td>
    </tr>`).join("");
  $$("#gen-list tr").forEach((tr) => tr.addEventListener("click", () => {
    state.selectGenerationId = Number(tr.dataset.gen);
    renderRules();
  }));

  $("#new-gen").addEventListener("click", () => {
    const base = state.ruleDraft || ruleDraftFromGeneration(data.current);
    state.selectGenerationId = null;
    state.ruleDraft = {
      generation_id: null, status: "draft", label: base.label + " 副本",
      note: "", steps: JSON.parse(JSON.stringify(base.steps)),
      readonly: false, basedOn: base.generation_id,
    };
    state.rulesDirty = true;
    paintEditor();
  });
  $("#save-rule").addEventListener("click", saveRule);
  $("#fork-rule").addEventListener("click", () => {
    state.ruleDraft = {
      generation_id: null, status: "draft",
      label: (state.ruleDraft?.label || "规则") + " 副本",
      note: "", steps: collectSteps(), readonly: false,
      basedOn: state.ruleDraft?.generation_id || null,
    };
    state.rulesDirty = true;
    paintEditor();
    toast("已复制为未保存的新草稿", "ok");
  });
  $("#publish-rule").addEventListener("click", publishRule);
  $("#preview-rule").addEventListener("click", previewRule);
  paintEditor();
}

function paintEditor() {
  const d = state.ruleDraft;
  if (!d) return;
  const title = d.generation_id ? `规则世代 #${d.generation_id}` : "新草稿（未保存）";
  $("#editor-title").textContent = title;
  $("#editor-sub").innerHTML = `${d.readonly
    ? "该世代已发布或已被取代，只读。需要调整请点击「另存为新草稿」。"
    : "草稿可自由编辑；保存后仍可继续修改，发布会冻结其内容。"}
     ${d.steps_hash ? `步骤哈希 <span class="mono">${esc(shortHash(d.steps_hash))}</span>` : ""}`;
  const st = $("#draft-state");
  st.className = "pill " + (state.rulesDirty ? "warn" : "");
  st.textContent = state.rulesDirty ? "有未保存修改" : (d.readonly ? "只读" : "草稿");
  const body = $("#editor-body");
  body.innerHTML = `
    <div class="row">
      <label class="field grow"><span>标签</span><input type="text" id="rule-label" value="${esc(d.label)}" ${d.readonly ? "disabled" : ""}></label>
      <label class="field grow"><span>备注</span><input type="text" id="rule-note" value="${esc(d.note || "")}" ${d.readonly ? "disabled" : ""}></label>
    </div>
    <div class="steps-editor" id="steps-editor">
      ${d.steps.map(stepEditorHtml).join("")}
    </div>
    <div class="actions">
      <button class="sm" id="add-step" ${d.readonly ? "disabled" : ""}>+ 添加步骤</button>
      <span class="tiny muted">步骤按顺序执行；每一步的输入/输出与码点都会写进证据。</span>
    </div>`;
  if (!d.readonly) {
    $("#rule-label").addEventListener("input", (e) => { d.label = e.target.value; state.rulesDirty = true; });
    $("#rule-note").addEventListener("input", (e) => { d.note = e.target.value; state.rulesDirty = true; });
    $("#add-step").addEventListener("click", () => {
      d.steps = collectSteps();
      d.steps.push({ op: "trim" });
      state.rulesDirty = true;
      paintEditor();
    });
    $$(".steps-editor .step").forEach((node) => {
      $$("[data-param]", node).forEach((input) => input.addEventListener("change", () => {
        if (input.dataset.param === "op") {
          d.steps = collectSteps();
          state.rulesDirty = true;
          paintEditor();
          return;
        }
        state.rulesDirty = true;
        const pill = $("#draft-state");
        pill.className = "pill warn";
        pill.textContent = "有未保存修改";
      }));
      $("[data-del]", node)?.addEventListener("click", () => {
        d.steps = collectSteps();
        d.steps.splice(Number(node.dataset.idx), 1);
        state.rulesDirty = true;
        paintEditor();
      });
      node.querySelectorAll("[data-move]").forEach((btn) => btn.addEventListener("click", () => {
        d.steps = collectSteps();
        const i = Number(node.dataset.idx);
        const j = btn.dataset.move === "up" ? i - 1 : i + 1;
        if (j < 0 || j >= d.steps.length) return;
        [d.steps[i], d.steps[j]] = [d.steps[j], d.steps[i]];
        state.rulesDirty = true;
        paintEditor();
      }));
    });
  }
}

async function saveRule() {
  const d = state.ruleDraft;
  const steps = d.readonly ? d.steps : collectSteps();
  const label = d.readonly ? d.label : $("#rule-label").value.trim();
  const note = d.readonly ? d.note : $("#rule-note").value.trim();
  try {
    let gen;
    if (d.readonly || !d.generation_id) {
      gen = await call(API.norm, "/rules/generations", {
        method: "POST",
        body: { label, steps, note, based_on: d.generation_id || d.basedOn || null },
      });
      toast(`已创建草稿世代 #${gen.generation_id}`, "ok");
    } else {
      gen = await call(API.norm, `/rules/generations/${d.generation_id}`, {
        method: "PUT", body: { label, steps, note },
      });
      toast(`草稿 #${gen.generation_id} 已保存`, "ok");
    }
    state.ruleDraft = ruleDraftFromGeneration(gen);
    state.ruleDraft.steps_hash = gen.steps_hash;
    state.ruleDraft.readonly = false;
    state.rulesDirty = false;
    state.selectGenerationId = gen.generation_id;
    if (gen.probe && gen.probe.warnings && gen.probe.warnings.length) {
      gen.probe.warnings.forEach((w) => toast("规则提示：" + w));
    }
    await refreshHeader();
    await renderRules();
  } catch (e) {
    toast(e.message, "bad");
  }
}

async function publishRule() {
  const d = state.ruleDraft;
  let gid = d.generation_id;
  try {
    if (state.rulesDirty || !gid || d.readonly) {
      await saveRule();
      gid = state.ruleDraft.generation_id;
    }
    if (!gid) { toast("没有可发布的草稿", "bad"); return; }
    const gen = await call(API.norm, `/rules/generations/${gid}`, { method: "GET" });
    if (!confirm(`发布规则世代 #${gid}「${gen.label}」？\n\n发布后所有新建关联必须使用该世代；进行中的构建会因世代不一致而放弃发布。`)) return;
    const res = await call(API.norm, `/rules/generations/${gid}/publish`, { method: "POST", body: {} });
    toast(res.changed ? `已发布世代 #${gid}` : `世代 #${gid} 已是当前发布`, "ok");
    state.selectGenerationId = gid;
    await refreshHeader();
    await renderRules();
  } catch (e) {
    toast(e.message, "bad");
  }
}

async function previewRule() {
  const d = state.ruleDraft;
  let steps;
  try { steps = d.readonly ? d.steps : collectSteps(); } catch (e) { toast(e.message, "bad"); return; }
  const panel = $("#preview-panel");
  panel.innerHTML = `
    <div class="panel" style="background:var(--panel-2)">
      <h3>映射预览（不落库）</h3>
      <p class="hint">每行一个原始值；逐字符证据会展示全角、零宽、大小写等处理结果。</p>
      <textarea id="preview-values" rows="5" spellcheck="false">ACME GmbH
ＡＣＭＥ　ＧｍｂＨ
ACME​ GmbH
  acme   gmbh
Müller GmbH
北京 华为 技术</textarea>
      <div class="actions mt"><button class="primary sm" id="run-preview">运行预览</button></div>
      <div id="preview-results" class="mt"></div>
    </div>`;
  $("#run-preview").addEventListener("click", async () => {
    const values = $("#preview-values").value.split("\n");
    try {
      const res = await call(API.norm, "/normalize/preview", { method: "POST", body: { steps, values } });
      $("#preview-results").innerHTML = res.results.map((r, i) => `
        <div class="step">
          <div class="row"><strong class="mono">#${i + 1}</strong><span class="raw-cell">${esc(r.raw)}</span></div>
          <div class="escaped">原值转义：${esc(r.raw_escaped)}</div>
          <div class="cp">原值码点：${r.raw_codepoints.map((c) => `${esc(c.codepoint)} ${esc(c.name)}`).join(" · ") || "全部为可打印 ASCII"}</div>
          <div class="row mt"><span class="pill">键</span><span class="mono">${esc(r.key)}</span>
            <span class="escaped">${esc(r.key_escaped)}</span>
            <span class="${r.empty ? "badge conflict" : "badge clean"}">${r.empty ? "空键" : "有效"}</span></div>
          <div class="mt">${r.steps.map((s) => evidenceStepHtml(s)).join("")}</div>
        </div>`).join("");
    } catch (e) {
      $("#preview-results").innerHTML = `<div class="banner bad">${esc(e.message)}</div>`;
    }
  });
}

/* -------------------------------------------------------------- evidence */
function evidenceStepHtml(step) {
  if (!step || step.input === undefined) {
    return `<div class="evidence-step"><span class="op">${esc(step?.op || "?")}</span> <span class="tiny muted">（未记录文本）</span></div>`;
  }
  const changes = (step.changes || []).map((c) => `
    <div class="chg">“${esc(c.before_escaped)}” → “${esc(c.after_escaped)}”</div>
    <div class="cp">${(c.before_codepoints || []).map((cp) => `${esc(cp.codepoint)} ${esc(cp.name)}`).join(" · ") || "ASCII"}</div>`).join("");
  return `<div class="evidence-step ${step.changed ? "changed" : ""}">
    <div class="row"><span class="op">#${step.index} ${esc(step.op)}</span>
      <span class="badge ${step.changed ? "adjudicated_merge" : "clean"}">${step.changed ? "有变化" : "无变化"}</span>
      ${step.note ? `<span class="tiny muted">${esc(step.note)}</span>` : ""}</div>
    <div class="pair">
      <div><div class="tiny muted">输入</div><span class="mono">${esc(step.input)}</span><div class="escaped">${esc(step.input_escaped)}</div></div>
      <div><div class="tiny muted">输出</div><span class="mono">${esc(step.output)}</span><div class="escaped">${esc(step.output_escaped)}</div></div>
    </div>
    ${changes ? `<div class="mt">${changes}</div>` : ""}
  </div>`;
}

/* ------------------------------------------------------------- conflicts */
async function renderConflicts() {
  const releases = await call(API.link, "/releases?limit=100");
  const adj = await call(API.link, "/adjudications?limit=200");
  const selected = state.releaseId || (state.header.release && state.header.release.id) || (releases.releases[0] && releases.releases[0].id);
  state.releaseId = selected || null;
  const view = $("#view");
  if (!selected) {
    view.innerHTML = `<div class="banner">尚无关联发布。请先到「录入数据」导入原值，再到「构建与发布」构建。</div>`;
    return;
  }
  const conflicts = await call(API.link, `/releases/${selected}/conflicts?page=1&page_size=100`);
  const gaps = await call(API.link, `/releases/${selected}/adjudication-gaps`);
  const rel = await call(API.link, `/releases/${selected}`);
  view.innerHTML = `
    <div class="panel mb">
      <div class="toolbar">
        <h2>碰撞证据与人工裁决</h2>
        <span class="spacer"></span>
        <select id="rel-select" style="width:auto">
          ${releases.releases.map((r) => `<option value="${esc(r.id)}" ${r.id === selected ? "selected" : ""}>
            ${esc(r.id)} · ${esc(r.status)} · 世代 #${esc(r.generation_id)} · ${fmt(r.conflict_class_count)} 碰撞类</option>`).join("")}
        </select>
        <button class="sm" id="reload-conflicts">刷新</button>
      </div>
      <p class="hint">一个规范键对应多个不同原值时视为碰撞。系统保留全部成员与来源，不自动合并。
        裁决只对同一批原值生效；若该键新增了不同原值，裁决会显示为需复核，而不会被悄悄套用。</p>
      <div class="grid c4">
        <div class="stat"><div class="label">发布</div><div class="value small" style="font-size:14px">${esc(rel.id)}</div>
          <div class="note">${esc(rel.generation_label)} · ${shortHash(rel.generation_hash)}</div></div>
        <div class="stat"><div class="label">碰撞隔离类</div><div class="value">${fmt(rel.conflict_class_count)}</div></div>
        <div class="stat warn"><div class="label">实质差异类</div><div class="value">${fmt(conflicts.summary ? conflicts.summary.substantive : 0)}</div>
          <div class="note">抹平不可见字符/大小写/全半角/空白/变音/标点后仍不同</div></div>
        <div class="stat"><div class="label">写法变体类</div><div class="value">${fmt(conflicts.summary ? conflicts.summary.variant : 0)}</div>
          <div class="note">差异可由上述某个写法因素完整解释</div></div>
      </div>
      ${gaps.gaps.length ? `<div class="banner mt">有 ${gaps.gaps.length} 条生效中裁决因原值集合变化而未应用，需要复核：${gaps.gaps.map((g) => `<span class="mono">#${g.adjudication_id}</span> ${esc(g.normalized_key)}`).join("；")}</div>` : ""}
    </div>
    <div class="panel mb">
      <h2>隔离键（${fmt(conflicts.total)}，其中实质差异 ${fmt(conflicts.summary ? conflicts.summary.substantive : 0)}）</h2>
      <p class="hint">“实质差异”表示依次抹平不可见字符、大小写/全半角、空白、变音符号和标点后仍然不同；
        这类合并更可能把不同实体并到一起，建议优先裁决。“写法变体”的差异可由其中某个因素完整解释，通常可安全合并。</p>
      <div id="conflict-list" class="mt">
        ${conflicts.items.map(conflictClassHtml).join("") || `<p class="muted">该发布没有碰撞隔离键。</p>`}
      </div>
    </div>
    <div class="panel">
      <h2>裁决记录</h2>
      <table><thead><tr><th>#</th><th>动作</th><th>键</th><th>原值数</th><th>状态</th><th>备注</th><th>创建</th><th></th></tr></thead>
      <tbody>
        ${adj.items.map((a) => `<tr>
          <td>${a.id}</td><td>${esc(a.action)}</td>
          <td class="mono">${esc(a.normalized_key)}<div class="escaped">${esc(a.key_escaped)}</div></td>
          <td class="num">${a.raw_shas.length}</td>
          <td>${badge(a.status)}</td>
          <td class="small">${esc(a.note || "")}<div class="tiny muted">${esc(a.operator || "")}</div></td>
          <td class="small muted">${fmtTime(a.created_at)}</td>
          <td>${a.status === "active" ? `<button class="sm danger" data-revoke="${a.id}">撤销</button>` : ""}</td>
        </tr>`).join("") || `<tr><td colspan="8" class="muted">暂无裁决</td></tr>`}
      </tbody></table>
    </div>`;
  $("#rel-select").addEventListener("change", (e) => { state.releaseId = e.target.value; renderConflicts(); });
  $("#reload-conflicts").addEventListener("click", () => renderConflicts());
  $$("[data-revoke]").forEach((btn) => btn.addEventListener("click", async () => {
    if (!confirm("撤销该裁决？之后需要重新构建才会反映到关联结果。")) return;
    try {
      await call(API.link, `/adjudications/${btn.dataset.revoke}/revoke`, { method: "POST", body: { note: "由操作台撤销" } });
      toast("已撤销裁决", "ok");
      renderConflicts();
    } catch (e) { toast(e.message, "bad"); }
  }));
  $$("[data-act]").forEach((btn) => btn.addEventListener("click", () => openAdjudicate(btn.dataset.act, selected, btn.dataset.cls)));
}

function conflictClassHtml(cls) {
  const typeLabel = {
    invisible: "不可见字符",
    case_or_width: "大小写/全半角",
    whitespace: "空白差异",
    diacritic: "变音符号",
    punctuation: "标点差异",
    characters: "字符不同",
    blank: "空/纯空白",
  };
  const severityLabel = { substantive: "实质差异", variant: "写法变体", empty_key: "空键" };
  const diff = cls.difference || { severity: "none", types: [], pairs: [] };
  return `<div class="step" data-class="${esc(cls.class_id)}">
    <div class="row">
      <strong class="mono">${esc(cls.normalized_key)}</strong>
      <span class="badge ${diff.severity === "substantive" ? "conflict" : (diff.severity === "empty_key" ? "empty" : "hold")}">${severityLabel[diff.severity] || diff.severity}</span>
      ${(diff.types || []).map((t) => `<span class="badge empty">${esc(typeLabel[t] || t)}</span>`).join("")}
      <span class="spacer"></span>
      <span class="tiny muted">${fmt(cls.member_count)} 成员 · ${fmt(cls.distinct_raw_count)} 种原值 · ${fmt(cls.source_count)} 来源</span>
    </div>
    <div class="escaped">${esc(cls.key_escaped)}</div>
    ${diff.severity === "substantive" ? `<div class="banner bad mt" style="margin-bottom:6px">
      该键合并了仅靠大小写/空白/标点无法解释的原值，合并后可能把不同实体并在一起，请逐条核对：
      ${(diff.worst_member ? [diff.worst_member] : diff.pairs.filter((p) => p.explained_by === "characters").map((p) => p.b))
        .slice(0, 4).map((raw) => `「${esc(raw)}」`).join("；")}
    </div>` : ""}
    <table class="mt"><thead><tr><th>来源</th><th>外部 ID</th><th>原始值（按字符保留）</th><th>码点</th><th></th></tr></thead><tbody>
      ${cls.members.map((m) => `<tr>
        <td class="nowrap">${esc(m.source_system)}</td>
        <td class="mono">${esc(m.source_id)}</td>
        <td><span class="raw-cell">「${esc(m.raw_value)}」</span><div class="escaped">${esc(m.raw_escaped)}</div></td>
        <td class="tiny muted">${m.raw_codepoints.map((c) => esc(c.codepoint)).join(" ") || "ASCII"}</td>
        <td><button class="sm ghost" data-member="${m.raw_id}">证据</button></td>
      </tr>`).join("")}
    </tbody></table>
    <div class="actions mt">
      <button class="sm primary" data-act="accept_merge" data-cls="${esc(cls.class_id)}">接受合并</button>
      <button class="sm" data-act="split" data-cls="${esc(cls.class_id)}">拆分…</button>
      <button class="sm" data-act="hold" data-cls="${esc(cls.class_id)}">保持隔离</button>
    </div>
  </div>`;
}

async function openAdjudicate(action, releaseId, classId) {
  const detail = await call(API.link, `/releases/${releaseId}/classes/${classId}`);
  const members = detail.members;
  const bySha = new Map();
  for (const m of members) {
    if (!bySha.has(m.raw_sha256)) bySha.set(m.raw_sha256, { sha: m.raw_sha256, raw: m.raw_value, escaped: m.raw_escaped, sources: [] });
    bySha.get(m.raw_sha256).sources.push(`${m.source_system}/${m.source_id}`);
  }
  const rows = Array.from(bySha.values());
  let body;
  if (action === "split") {
    body = `
      <p class="hint">把该键下的不同原值分组，每组会生成一个独立的等价类。所有原值都必须归组。</p>
      <table><thead><tr><th>原值</th><th>来源</th><th style="width:120px">分组号</th></tr></thead><tbody>
        ${rows.map((r, i) => `<tr>
          <td><span class="raw-cell">「${esc(r.raw)}」</span><div class="escaped">${esc(r.escaped)}</div></td>
          <td class="small">${esc(r.sources.join("、"))}</td>
          <td><input type="number" min="1" max="20" value="${i + 1}" data-sha="${esc(r.sha)}" class="split-group"></td>
        </tr>`).join("")}
      </tbody></table>`;
  } else if (action === "hold") {
    body = `<p class="hint">保持隔离：该键继续以碰撞状态出现在报告里，不做任何合并，后续可重新裁决。</p>
      ${rows.map((r) => `<div class="mono">「${esc(r.raw)}」 <span class="escaped">${esc(r.escaped)}</span></div>`).join("")}`;
  } else {
    body = `<p class="hint">接受合并：确认这些不同原值在业务上是同一实体。报告会标记为人工合并并保留全部成员与证据。</p>
      ${rows.map((r) => `<div class="mono">「${esc(r.raw)}」 <span class="escaped">${esc(r.escaped)}</span></div>`).join("")}`;
  }
  body += `<label class="field mt"><span>裁决备注（写入报告）</span><input type="text" id="adj-note" placeholder="例如：已与业务方确认，全角与半角为同一供应商"></label>
    <label class="field"><span>操作人</span><input type="text" id="adj-operator" value="operator"></label>`;
  const m = modal(`人工裁决 · ${action}`, body,
    `<div class="actions"><span class="muted small grow">裁决将在下一次构建时应用，不会改写已有发布。</span>
      <button class="ghost" id="adj-cancel">取消</button><button class="primary" id="adj-submit">提交裁决</button></div>`);
  $("#adj-cancel", m.node).addEventListener("click", m.close);
  $("#adj-submit", m.node).addEventListener("click", async () => {
    let groups;
    if (action === "split") {
      const inputs = $$(".split-group", m.node);
      const map = new Map();
      for (const input of inputs) {
        const g = Number(input.value);
        if (!Number.isInteger(g) || g < 1) { toast("分组号必须是正整数", "bad"); return; }
        if (!map.has(g)) map.set(g, []);
        map.get(g).push(input.dataset.sha);
      }
      groups = Array.from(map.values());
      if (groups.length < 2) { toast("拆分至少需要两个分组", "bad"); return; }
      const total = groups.reduce((n, g) => n + g.length, 0);
      if (total !== rows.length) { toast("分组必须覆盖所有原值", "bad"); return; }
    }
    try {
      await call(API.link, "/adjudications", {
        method: "POST",
        body: {
          release_id: releaseId, class_id: classId, action,
          groups, note: $("#adj-note", m.node).value, operator: $("#adj-operator", m.node).value,
        },
      });
      m.close();
      toast("裁决已记录；请重新构建以应用", "ok");
      renderConflicts();
    } catch (e) { toast(e.message, "bad"); }
  });
}

/* ---------------------------------------------------------------- builds */
async function renderBuilds() {
  const [rules, jobs, releases, ov] = await Promise.all([
    call(API.norm, "/rules"),
    call(API.link, "/jobs?limit=20"),
    call(API.link, "/releases?limit=30"),
    call(API.link, "/overview"),
  ]);
  const gen = rules.current;
  const view = $("#view");
  const previews = releases.releases.filter((r) => r.status === "preview");
  view.innerHTML = `
    <div class="grid c2">
      <div class="panel">
        <h2>构建关联结果</h2>
        <p class="hint">构建会抓取活动原值、用当前发布世代规范化、生成等价类。发布写入一次指针切换，失败或取消不会影响现有发布。</p>
        <div class="banner">将使用规则世代 <strong>#${esc(gen.generation_id)} · ${esc(gen.label)}</strong>
          <span class="mono tiny">${esc(shortHash(gen.steps_hash))}</span></div>
        <label class="field"><span>模式</span>
          <select id="build-mode">
            <option value="full">全量重建（规则或数据有变化时首选）</option>
            <option value="incremental">增量维护受影响等价类（仅规则世代相同）</option>
          </select></label>
        <label class="field"><span>备注</span><input type="text" id="build-note" placeholder="可选"></label>
        <label class="row small" style="margin:8px 0 14px"><input type="checkbox" id="build-publish" style="width:auto">
          构建成功后直接发布（否则生成预览，审阅影响后再激活）</label>
        <div class="actions"><button class="primary" id="start-build">开始构建</button>
          <button class="ghost" id="refresh-builds">刷新</button></div>
        <div id="live-job" class="mt"></div>
        <div class="banner mt">
          增量与全量：增量只重算发生变化的原值所在键；当规则世代变化时必须全量重建，
          因为旧键可能被合并或拆开，无法只靠局部维护得到正确结果。
        </div>
      </div>
      <div class="panel">
        <h2>构建任务</h2>
        <table><thead><tr><th>任务</th><th>模式</th><th>状态</th><th class="num">进度</th><th>开始</th><th></th></tr></thead>
        <tbody id="job-list">
          ${jobs.jobs.map(jobRowHtml).join("") || `<tr><td colspan="6" class="muted">暂无构建任务</td></tr>`}
        </tbody></table>
      </div>
    </div>
    <div class="panel mt">
      <h2>预览发布（尚未生效）</h2>
      <p class="hint">预览发布已固化一个世代与一次规范化运行；激活前请先查看与当前发布的差异。</p>
      <table><thead><tr><th>发布</th><th>世代</th><th class="num">类</th><th class="num">碰撞</th><th>构建</th><th></th></tr></thead>
      <tbody>
        ${previews.map((r) => `<tr>
          <td class="mono">${esc(r.id)}</td>
          <td>#${esc(r.generation_id)} ${esc(r.generation_label)}</td>
          <td class="num">${fmt(r.class_count)}</td>
          <td class="num">${fmt(r.conflict_class_count)}</td>
          <td class="small muted">${fmtTime(r.created_at)}</td>
          <td><button class="sm" data-preview="${esc(r.id)}">影响对比</button>
            <button class="sm primary" data-activate="${esc(r.id)}" data-hash="${esc(r.generation_hash)}">激活发布</button></td>
        </tr>`).join("") || `<tr><td colspan="6" class="muted">没有预览发布</td></tr>`}
      </tbody></table>
    </div>
    <div class="panel mt">
      <h2>发布历史</h2>
      <table><thead><tr><th>发布</th><th>状态</th><th>世代</th><th class="num">成员</th><th class="num">类</th><th class="num">碰撞类</th><th class="num">人工裁决</th><th>发布时间</th><th></th></tr></thead>
      <tbody>
        ${releases.releases.map((r) => `<tr>
          <td class="mono">${esc(r.id)}</td><td>${badge(r.status)}</td>
          <td>#${esc(r.generation_id)} <span class="tiny muted">${esc(shortHash(r.generation_hash))}</span></td>
          <td class="num">${fmt(r.item_count)}</td><td class="num">${fmt(r.class_count)}</td>
          <td class="num">${fmt(r.conflict_class_count)}</td><td class="num">${fmt(r.adjudicated_class_count)}</td>
          <td class="small muted">${fmtTime(r.published_at || r.created_at)}</td>
          <td><button class="sm ghost" data-preview="${esc(r.id)}">查看</button></td>
        </tr>`).join("")}
      </tbody></table>
    </div>`;

  $("#start-build").addEventListener("click", startBuild);
  $("#refresh-builds").addEventListener("click", () => renderBuilds());
  $$("[data-preview]").forEach((b) => b.addEventListener("click", () => showDiff(b.dataset.preview)));
  $$("[data-activate]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("激活该预览发布？激活后所有读取关联结果的调用都会使用它。")) return;
    try {
      await call(API.link, `/releases/${b.dataset.activate}/activate`, {
        method: "POST",
        body: { expected_generation_hash: b.dataset.hash },
      });
      toast("已激活发布", "ok");
      await refreshHeader();
      renderBuilds();
    } catch (e) { toast(e.message, "bad"); }
  }));
  const activeJob = jobs.jobs.find((j) => ["queued", "running", "cancelling"].includes(j.status));
  if (activeJob) pollJob(activeJob.id);
}

function jobRowHtml(job) {
  const pct = job.progress_total ? Math.min(100, Math.round((job.progress_current / job.progress_total) * 100)) : 0;
  return `<tr data-job="${esc(job.id)}">
    <td class="mono tiny">${esc(job.id)}</td>
    <td>${job.mode === "full" ? "全量" : "增量"}${job.publish ? " · 直发" : ""}</td>
    <td>${badge(job.status)}<div class="tiny muted">${esc(job.stage || "")}${job.error ? " · " + esc(job.error) : ""}</div></td>
    <td class="num"><div class="progress"><div style="width:${pct}%"></div></div><span class="tiny muted">${fmt(job.progress_current)}/${fmt(job.progress_total)}</span></td>
    <td class="small muted">${fmtTime(job.started_at || job.created_at)}</td>
    <td>${["queued", "running", "cancelling"].includes(job.status) ? `<button class="sm danger" data-cancel="${esc(job.id)}">中断</button>` : ""}</td>
  </tr>`;
}

async function startBuild() {
  const mode = $("#build-mode").value;
  const publish = $("#build-publish").checked;
  const note = $("#build-note").value.trim();
  const gen = state.header.gen;
  try {
    const job = await call(API.link, "/builds", { method: "POST", body: { mode, publish, note, generation_id: gen ? gen.generation_id : null } });
    toast(`构建任务 ${job.id} 已启动`, "ok");
    pollJob(job.id);
    renderBuilds();
  } catch (e) {
    toast(e.message, "bad");
  }
}

function pollJob(jobId) {
  if (state.buildPoll) clearInterval(state.buildPoll);
  const tick = async () => {
    try {
      const job = await call(API.link, `/jobs/${jobId}`);
      const live = $("#live-job");
      if (live && state.tab === "builds") {
        const pct = job.progress_total ? Math.min(100, Math.round((job.progress_current / job.progress_total) * 100)) : 0;
        live.innerHTML = `
          <div class="panel" style="background:var(--panel-2)">
            <div class="row"><strong class="mono">${esc(job.id)}</strong> ${badge(job.status)}
              <span class="spacer"></span><span class="small muted">${esc(job.stage || "")}</span></div>
            <div class="progress mt"><div style="width:${pct}%"></div></div>
            <div class="small muted mt">${fmt(job.progress_current)} / ${fmt(job.progress_total)}
              ${job.affected_item_count ? ` · 受影响原值 ${fmt(job.affected_item_count)}` : ""}
              ${job.duration_ms ? ` · ${job.duration_ms} ms` : ""}</div>
            ${job.error ? `<div class="banner bad mt">${esc(job.error)}</div>` : ""}
            ${job.impact ? `<div class="codebox mt">${esc(JSON.stringify(job.impact, null, 2))}</div>` : ""}
            <div class="actions mt">
              ${["queued", "running", "cancelling"].includes(job.status) ? `<button class="sm danger" id="cancel-job">中断构建</button>` : ""}
              ${job.release_id ? `<button class="sm" id="view-release">查看发布 ${esc(job.release_id)}</button>` : ""}
            </div>
          </div>`;
        if ($("#cancel-job")) $("#cancel-job").addEventListener("click", async () => {
          try {
            await call(API.link, `/jobs/${jobId}/cancel`, { method: "POST", body: {} });
            toast("已请求中断；暂存数据会被清理，现有发布不受影响", "ok");
          } catch (e) { toast(e.message, "bad"); }
        });
        if ($("#view-release")) $("#view-release").addEventListener("click", () => showDiff(job.release_id));
      }
      if (!["queued", "running", "cancelling"].includes(job.status)) {
        clearInterval(state.buildPoll);
        state.buildPoll = null;
        await refreshHeader();
        if (state.tab === "builds") renderBuilds();
        if (job.status === "completed") toast("构建完成" + (job.impact && job.impact.published ? "并已发布" : "（预览）"), "ok");
        else if (job.status === "failed") toast("构建失败：" + (job.error || ""), "bad");
        else if (job.status === "cancelled") toast("构建已中断", "");
      }
    } catch (e) {
      clearInterval(state.buildPoll);
      state.buildPoll = null;
      toast(e.message, "bad");
    }
  };
  tick();
  state.buildPoll = setInterval(tick, 700);
}

/* ----------------------------------------------------------------- diff */
async function showDiff(candidateId) {
  try {
    const active = state.header.release;
    if (!active) { toast("没有当前生效发布可用于对比", "bad"); return; }
    if (active.id === candidateId) { toast("该发布就是当前生效发布"); return; }
    const diff = await call(API.link, `/diff?base=${encodeURIComponent(active.id)}&candidate=${encodeURIComponent(candidateId)}&examples=3`);
    const section = (title, items, render) => `
      <h3>${title}（${items.length}）</h3>
      ${items.length ? items.slice(0, 20).map(render).join("") : `<p class="muted small">无</p>`}`;
    const m = modal("发布影响对比", `
      <div class="grid c4 mb">
        <div class="stat"><div class="label">共同成员</div><div class="value">${fmt(diff.common_members)}</div></div>
        <div class="stat"><div class="label">迁移成员</div><div class="value">${fmt(diff.moved_member_count)}</div></div>
        <div class="stat warn"><div class="label">合并</div><div class="value">${fmt(diff.impact.merged_new_classes)}</div>
          <div class="note">旧类 → 同一新类</div></div>
        <div class="stat warn"><div class="label">拆分</div><div class="value">${fmt(diff.impact.split_old_classes)}</div>
          <div class="note">旧类 → 多个新类</div></div>
      </div>
      <p class="hint">基准：${esc(active.id)}（世代 #${esc(active.generation_id)}） → 候选：${esc(candidateId)}
        （世代 #${esc(diff.candidate.generation.generation_id)}）</p>
      ${section("合并", diff.merges, (x) => `<div class="step"><div class="mono">${esc(x.new_class.normalized_key)}</div>
        <div class="tiny muted">合并了 ${x.old_class_ids.length} 个旧类，共同 ${fmt(x.new_class.member_count)} 成员</div>
        ${x.sample.map((s) => `<div class="evidence-step"><span class="raw-cell">「${esc(s.raw_value)}」</span>
          <span class="tiny muted">${esc(s.source_system)} · ${esc(s.old_key)} → ${esc(s.new_key)}</span></div>`).join("")}</div>`)}
      ${section("拆分", diff.splits, (x) => `<div class="step"><div class="mono">${esc(x.old_class.normalized_key)}</div>
        <div class="tiny muted">拆成 ${x.new_class_ids.length} 个新类</div>
        ${x.sample.map((s) => `<div class="evidence-step"><span class="raw-cell">「${esc(s.raw_value)}」</span>
          <span class="tiny muted">${esc(s.source_system)} · ${esc(s.old_key)} → ${esc(s.new_key)}</span></div>`).join("")}</div>`)}
      ${section("新增等价类", diff.added, (x) => `<div class="step"><div class="mono">${esc(x.normalized_key)}</div>
        <div class="tiny muted">${fmt(x.member_count)} 成员 · ${esc(x.status)}</div></div>`)}
      ${section("消失等价类", diff.removed, (x) => `<div class="step"><div class="mono">${esc(x.normalized_key)}</div>
        <div class="tiny muted">${fmt(x.member_count)} 成员 · ${esc(x.status)}</div></div>`)}
    `);
    $(".modal", m.node).style.width = "min(1100px, 100%)";
  } catch (e) {
    toast(e.message, "bad");
  }
}

async function openReport(releaseId) {
  state.releaseId = releaseId;
  setTab("reports");
}

/* -------------------------------------------------------------- reports */
async function renderReports() {
  const releases = await call(API.link, "/releases?limit=100");
  const selected = state.releaseId || (state.header.release && state.header.release.id) || (releases.releases[0] && releases.releases[0].id);
  state.releaseId = selected || null;
  const view = $("#view");
  if (!selected) {
    view.innerHTML = `<div class="banner">尚无关联发布。请先录入数据并构建发布。</div>`;
    return;
  }
  const rel = await call(API.link, `/releases/${selected}`);
  const gaps = await call(API.link, `/releases/${selected}/adjudication-gaps`);
  view.innerHTML = `
    <div class="panel mb">
      <div class="toolbar">
        <h2>关联报告</h2>
        <span class="spacer"></span>
        <select id="report-select" style="width:auto">
          ${releases.releases.map((r) => `<option value="${esc(r.id)}" ${r.id === selected ? "selected" : ""}>
            ${esc(r.id)} · ${esc(r.status)} · 世代 #${esc(r.generation_id)}</option>`).join("")}
        </select>
        <button class="sm" id="refresh-report">刷新</button>
      </div>
      <p class="hint">报告读取固定发布；发布时的规则世代、运行 ID 与报告指纹一并展示与下载。之后修改规则不会改写本报告。</p>
      <div class="grid c3">
        <div class="stat"><div class="label">发布</div><div class="value small" style="font-size:14px">${esc(rel.id)}</div>
          <div class="note">${badge(rel.status)} · ${esc(rel.kind === "full" ? "全量" : "增量")}</div></div>
        <div class="stat"><div class="label">等价类</div><div class="value">${fmt(rel.class_count)}</div>
          <div class="note">碰撞 ${fmt(rel.conflict_class_count)} · 人工裁决 ${fmt(rel.adjudicated_class_count)}</div></div>
        <div class="stat"><div class="label">成员</div><div class="value">${fmt(rel.item_count)}</div>
          <div class="note">空键 ${fmt(rel.empty_item_count)} · 冲突成员 ${fmt(rel.member_conflict_count)}</div></div>
      </div>
      <dl class="kv mt">
        <dt>规则世代</dt><dd>#${esc(rel.generation_id)} · ${esc(rel.generation_label)}</dd>
        <dt>世代哈希</dt><dd>${esc(rel.generation_hash)}</dd>
        <dt>规范化运行</dt><dd>${esc(rel.normalizer_run_id || "—")}</dd>
        <dt>报告指纹</dt><dd>${esc(rel.report_sha256 || "—")}</dd>
        <dt>父发布</dt><dd>${esc(rel.parent_release || "—")}</dd>
      </dl>
      <div class="actions mt">
        <a class="btn" href="/api/link/releases/${esc(rel.id)}/report.json">下载完整报告 JSON</a>
        <a class="btn" href="/api/link/releases/${esc(rel.id)}/classes.csv">下载等价类 CSV</a>
        <a class="btn" href="/api/link/releases/${esc(rel.id)}/members.csv">下载成员 CSV</a>
        ${rel.normalizer_run_id ? `<a class="btn" href="/api/norm/runs/${esc(rel.normalizer_run_id)}/evidence.csv">下载逐字符证据 CSV</a>
        <a class="btn" href="/api/norm/runs/${esc(rel.normalizer_run_id)}/mapping.csv">下载映射 CSV</a>
        <a class="btn" href="/api/norm/runs/${esc(rel.normalizer_run_id)}/rules.json">下载规则快照</a>` : ""}
        <button class="ghost" id="show-rules">查看规则步骤</button>
      </div>
      ${gaps.gaps.length ? `<div class="banner mt">${gaps.gaps.length} 条裁决因原值变化需复核：${gaps.gaps.map((g) => " #" + g.adjudication_id).join("")}</div>` : ""}
    </div>
    <div class="panel">
      <div class="toolbar">
        <h2>等价类</h2>
        <span class="spacer"></span>
        <input type="search" id="class-search" placeholder="搜索规范键…" style="width:220px">
        <select id="class-status" style="width:auto">
          <option value="">全部状态</option>
          <option value="conflict">碰撞/隔离</option>
          <option value="clean">已归类</option>
          <option value="empty">空键</option>
          <option value="adjudicated">人工裁决</option>
        </select>
      </div>
      <div id="class-list"><span class="spinner"></span></div>
    </div>`;
  $("#report-select").addEventListener("change", (e) => { state.releaseId = e.target.value; renderReports(); });
  $("#refresh-report").addEventListener("click", () => renderReports());
  $("#show-rules").addEventListener("click", () => {
    modal(`规则世代 #${rel.generation_id} · ${rel.generation_label}`, `
      <div class="codebox">${esc(JSON.stringify(rel.steps, null, 2))}</div>
      <p class="hint mt">步骤哈希 ${esc(rel.generation_hash)}</p>`);
  });
  const loadClasses = async () => {
    const status = $("#class-status").value;
    const q = $("#class-search").value.trim();
    const data = await call(API.link, `/releases/${selected}/classes?page=1&page_size=200&status=${encodeURIComponent(status)}&q=${encodeURIComponent(q)}`);
    $("#class-list").innerHTML = `<table><thead><tr><th>规范键</th><th>状态</th><th class="num">成员</th><th class="num">原值种数</th>
      <th class="num">来源</th><th class="num">冲突成员</th><th>等价类</th></tr></thead><tbody>
      ${data.items.map((c) => `<tr>
        <td class="mono">${esc(c.normalized_key) || "<空键>"}<div class="escaped">${esc(c.key_escaped)}</div></td>
        <td>${badge(c.status)}${c.split_group !== null && c.split_group !== undefined ? `<div class="tiny muted">拆分分组 ${c.split_group}</div>` : ""}</td>
        <td class="num">${fmt(c.member_count)}</td><td class="num">${fmt(c.distinct_raw_count)}</td>
        <td class="num">${fmt(c.source_count)}</td><td class="num">${fmt(c.conflict_member_count)}</td>
        <td><button class="sm ghost" data-class="${esc(c.class_id)}">成员与证据</button></td>
      </tr>`).join("") || `<tr><td colspan="7" class="muted">无匹配等价类</td></tr>`}
      </tbody></table>
      <p class="tiny muted mt">共 ${fmt(data.total)} 个等价类，显示前 ${fmt(data.items.length)} 个。</p>`;
    $$("[data-class]").forEach((btn) => btn.addEventListener("click", () => openClass(selected, btn.dataset.class)));
  };
  await loadClasses();
  $("#class-status").addEventListener("change", loadClasses);
  $("#class-search").addEventListener("input", () => {
    clearTimeout(loadClasses._t);
    loadClasses._t = setTimeout(loadClasses, 250);
  });
}

async function openClass(releaseId, classId) {
  const detail = await call(API.link, `/releases/${releaseId}/classes/${classId}`);
  const cls = detail.class;
  const m = modal(`等价类 ${cls.normalized_key}`, `
    <dl class="kv mb">
      <dt>规范键</dt><dd>${esc(cls.normalized_key)}</dd>
      <dt>转义</dt><dd>${esc(cls.key_escaped)}</dd>
      <dt>状态</dt><dd>${badge(cls.status)}</dd>
      <dt>成员</dt><dd>${fmt(cls.member_count)} · 不同原值 ${fmt(cls.distinct_raw_count)} · 来源 ${fmt(cls.source_count)}</dd>
      <dt>分裂组</dt><dd>${cls.split_group ?? "—"}</dd>
      <dt>裁决</dt><dd>${detail.adjudication ? `#${detail.adjudication.id} ${esc(detail.adjudication.action)}` : "—"}</dd>
    </dl>
    <table><thead><tr><th>来源</th><th>外部 ID</th><th>原始值</th><th>sha256</th><th>冲突</th><th></th></tr></thead><tbody>
      ${detail.members.map((mem) => `<tr>
        <td class="nowrap">${esc(mem.source_system)}</td>
        <td class="mono">${esc(mem.source_id)}<div class="tiny muted">rev ${mem.revision}</div></td>
        <td><span class="raw-cell">「${esc(mem.raw_value)}」</span><div class="escaped">${esc(mem.raw_escaped)}</div></td>
        <td class="tiny mono">${esc(shortHash(mem.raw_sha256, 16))}</td>
        <td>${mem.conflict ? badge("conflict") : ""}</td>
        <td><button class="sm ghost" data-evidence="${mem.raw_id}">逐字符证据</button></td>
      </tr>`).join("")}
    </tbody></table>`);
  $(".modal", m.node).style.width = "min(1150px, 100%)";
  $$("[data-evidence]", m.node).forEach((btn) => btn.addEventListener("click", async () => {
    try {
      const item = await call(API.link, `/releases/${releaseId}/members/${btn.dataset.evidence}`);
      const ev = item.evidence && item.evidence.evidence;
      if (!ev) throw new Error(item.evidence?.error || "证据不可用");
      const evSteps = ev.steps || [];
      const em = modal(`逐字符证据 · raw_id ${btn.dataset.evidence}`, `
        <dl class="kv mb">
          <dt>原始值</dt><dd>「${esc(ev.raw)}」</dd>
          <dt>原值转义</dt><dd>${esc(ev.raw_escaped)}</dd>
          <dt>原值 SHA256</dt><dd>${esc(ev.raw_sha256)}</dd>
          <dt>规范键</dt><dd>${esc(ev.key)}</dd>
          <dt>规范键转义</dt><dd>${esc(ev.key_escaped)}</dd>
          <dt>规则世代</dt><dd>#${esc(item.evidence.run.generation.generation_id)} · ${esc(item.evidence.run.generation.label)}</dd>
          <dt>世代哈希</dt><dd>${esc(item.evidence.run.generation.steps_hash)}</dd>
          <dt>运行</dt><dd>${esc(item.evidence.run.run_id)}</dd>
        </dl>
        <h3>原值码点</h3>
        <div class="codebox">${ev.raw_codepoints.map((c) => `${c.codepoint}  ${c.name}  「${esc(c.char)}」`).join("\n") || "全部为可打印 ASCII"}</div>
        <h3>逐步骤证据</h3>
        ${evSteps.map(evidenceStepHtml).join("")}
        <p class="hint mt">该证据保存在规范化服务的不可变运行 ${esc(item.evidence.run.run_id)} 中，与发布世代 ${esc(item.evidence.run.generation.steps_hash)} 绑定。</p>`);
      $(".modal", em.node).style.width = "min(1150px, 100%)";
    } catch (e) { toast(e.message, "bad"); }
  }));
}

/* --------------------------------------------------------------- intake */
const SAMPLE_CSV = `source_system,source_id,raw_value
ERP,E-1001,ACME GmbH
ERP,E-1002,ＡＣＭＥ　ＧｍｂＨ
ERP,E-1003,ＡＣＭＥ${"​"}　ＧｍｂＨ
CRM,C-77,acme gmbh
CRM,C-78,Acme  GmbH
ERP,E-1004,  ACME GmbH
Ledger,L-9,ACME GMBH.
MDM,M-4,Müller GmbH
MDM,M-5,MÜLLER GMBH
MDM,M-6,Muller GmbH
CRM,C-90,Ｍüｌｌｅｒ GmbH
MDM,M-7,北京华为技术有限公司
CRM,C-91,北京 华为 技术 有限公司
ERP,E-2001,北京华为技术有限公司
CRM,C-92,ACME-GmbH
ERP,E-2002,
ERP,E-2003,
`;

async function renderIntake() {
  const [snap, recent] = await Promise.all([
    call(API.raw, "/snapshot"),
    call(API.raw, "/records?limit=30&active=1"),
  ]);
  const view = $("#view");
  view.innerHTML = `
    <div class="grid c2" style="grid-template-columns: 1fr 1fr">
      <div class="panel">
        <h2>录入原值</h2>
        <p class="hint">原值服务按 UTF-8 原样保存，不裁剪、不改大小写、不做 Unicode 归一。相同来源与外部 ID 的新值会产生新修订，旧修订保留。</p>
        <label class="field"><span>格式</span>
          <select id="intake-format"><option value="csv">CSV（含表头）</option><option value="json">JSON 行数组</option></select></label>
        <label class="field"><span>默认来源系统（CSV 无 source_system 列时使用）</span>
          <input type="text" id="intake-source" placeholder="例如 ERP"></label>
        <label class="field"><span>备注</span><input type="text" id="intake-note" placeholder="可选"></label>
        <label class="field"><span>数据</span>
          <textarea id="intake-text" rows="14" spellcheck="false"></textarea></label>
        <div class="actions">
          <button class="primary" id="intake-submit">接收数据</button>
          <button class="ghost" id="intake-sample">填入示例数据</button>
          <span class="spacer"></span>
          <span class="tiny muted">也支持 curl POST /ingest（text/csv 或 application/json）</span>
        </div>
        <div id="intake-result" class="mt"></div>
        <div class="banner mt">可直接粘贴含全角、零宽字符、不同大小写与首尾空白的原值。请勿手工“清洗”，保留原始字符正是后续证据的来源。</div>
      </div>
      <div class="panel">
        <h2>原值快照</h2>
        <div class="grid c3">
          <div class="stat"><div class="label">活动记录</div><div class="value">${fmt(snap.active_count)}</div></div>
          <div class="stat"><div class="label">来源系统</div><div class="value">${fmt(snap.sources.length)}</div></div>
          <div class="stat"><div class="label">接收批次</div><div class="value">${fmt(snap.batch_count)}</div></div>
        </div>
        <h3>来源</h3>
        <table><thead><tr><th>来源系统</th><th class="num">活动记录</th><th>最近接收</th></tr></thead><tbody>
          ${snap.sources.map((s) => `<tr><td>${esc(s.source_system)}</td><td class="num">${fmt(s.active)}</td>
            <td class="small muted">${fmtTime(s.last_received)}</td></tr>`).join("") || `<tr><td colspan="3" class="muted">暂无数据</td></tr>`}
        </tbody></table>
        <h3>最近 30 条活动原值</h3>
        <div class="toolbar"><input type="search" id="raw-search" placeholder="搜索原始值…" style="width:220px">
          <a class="btn sm" href="/api/raw/export.csv">导出活动原值 CSV</a></div>
        <div id="raw-list" class="scroll-y"></div>
      </div>
    </div>`;
  $("#intake-text").value = SAMPLE_CSV;
  $("#intake-sample").addEventListener("click", () => {
    $("#intake-format").value = "csv";
    $("#intake-text").value = SAMPLE_CSV;
  });
  $("#intake-submit").addEventListener("click", submitIntake);
  const rawList = async (q = "") => {
    const data = await call(API.raw, `/records?limit=30&active=1&q=${encodeURIComponent(q)}`);
    $("#raw-list").innerHTML = `<table><thead><tr><th>ID</th><th>来源</th><th>原始值</th></tr></thead><tbody>
      ${data.items.map((r) => `<tr>
        <td class="mono tiny">${r.raw_id}</td>
        <td class="small">${esc(r.source_system)}<div class="tiny muted mono">${esc(r.source_id)} · r${r.revision}</div></td>
        <td><span class="raw-cell">「${esc(r.raw_value)}」</span><div class="escaped">${esc(r.raw_escaped)}</div></td>
      </tr>`).join("") || `<tr><td colspan="3" class="muted">无记录</td></tr>`}
    </tbody></table>`;
  };
  await rawList();
  $("#raw-search").addEventListener("input", () => {
    clearTimeout(rawList._t);
    rawList._t = setTimeout(() => rawList($("#raw-search").value.trim()), 250);
  });
}

async function submitIntake() {
  const format = $("#intake-format").value;
  const text = $("#intake-text").value;
  const source = $("#intake-source").value.trim();
  const note = $("#intake-note").value.trim();
  const result = $("#intake-result");
  try {
    let payload;
    if (format === "csv") {
      const rows = parseCsv(text);
      if (!rows.length) throw new Error("CSV 没有数据行");
      payload = {
        source_system: source || null,
        note: note || null,
        rows: rows.map((row) => ({
          source_system: row.source_system || source || null,
          source_id: row.source_id || row.external_id,
          raw_value: row.raw_value,
        })),
      };
    } else {
      let rows = JSON.parse(text);
      if (!Array.isArray(rows)) rows = rows.rows || rows.records || [];
      payload = { source_system: source || null, note: note || null, rows };
    }
    const res = await call(API.raw, "/ingest", { method: "POST", body: payload });
    result.innerHTML = `<div class="banner ok">批次 ${esc(res.batch_id)}：新增 ${res.inserted}，修订 ${res.revised}，
      未变 ${res.unchanged}，拒绝 ${res.rejected}</div>`;
    if (res.rejected_rows && res.rejected_rows.length) {
      result.innerHTML += `<div class="codebox">${esc(JSON.stringify(res.rejected_rows, null, 2))}</div>`;
    }
    await refreshHeader();
    renderIntake();
  } catch (e) {
    result.innerHTML = `<div class="banner bad">${esc(e.message)}</div>`;
  }
}

/* RFC4180-ish parser, keeps quotes and embedded newlines. */
function parseCsv(text) {
  const rows = [];
  let row = [];
  let field = "";
  let quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quoted) {
      if (c === '"') {
        if (text[i + 1] === '"') { field += '"'; i++; }
        else quoted = false;
      } else field += c;
    } else if (c === '"') {
      quoted = true;
    } else if (c === ",") {
      row.push(field); field = "";
    } else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i++;
      row.push(field); field = "";
      if (row.some((v) => v !== "")) rows.push(row);
      row = [];
    } else {
      field += c;
    }
  }
  row.push(field);
  if (row.some((v) => v !== "")) rows.push(row);
  if (!rows.length) return [];
  const header = rows.shift().map((h) => h.trim().toLowerCase());
  return rows.map((cells) => {
    const obj = {};
    header.forEach((h, idx) => { obj[h] = cells[idx] !== undefined ? cells[idx] : ""; });
    return obj;
  });
}

/* ------------------------------------------------------------------ init */
document.addEventListener("DOMContentLoaded", async () => {
  $$("#tabs button").forEach((b) => b.addEventListener("click", () => {
    if (state.rulesDirty && state.tab === "rules" && b.dataset.tab !== "rules") {
      if (!confirm("规则页有未保存修改，离开将丢失。继续？")) return;
      state.rulesDirty = false;
    }
    setTab(b.dataset.tab);
  }));
  await refreshHeader();
  setTab("overview");
  setInterval(async () => {
    await refreshHeader();
    if (state.tab === "overview") renderOverview().catch(() => {});
  }, 6000);
});
