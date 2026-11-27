/* 展厅播放器黑屏排查台前端逻辑（原生 JS，无构建依赖）。
 * 服务端保存案例状态；每次录入/重启/更正都是局部更新后重渲染，
 * 因而更正一条旧读数不会影响重启状态或其他读数。
 */
"use strict";

let DATA = null;

const $ = (id) => document.getElementById(id);

function esc(v) {
  return String(v == null ? "" : v)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function fmtDuration(sec) {
  if (sec == null) return "—";
  const s = Number(sec);
  if (!isFinite(s)) return "—";
  if (s < 60) return `${s} 秒`;
  const m = Math.floor(s / 60);
  const r = Math.round(s % 60);
  return r === 0 ? `${m} 分钟` : `${m} 分 ${r} 秒`;
}

function phaseLabel(p) {
  return p === "post" ? "重启后" : "重启前";
}

function toast(msg, isError) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast" + (isError ? " error" : "");
  t.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { t.hidden = true; }, 3200);
}

async function api(path, options) {
  const resp = await fetch(path, Object.assign({
    headers: { "Content-Type": "application/json" },
  }, options || {}));
  let payload = null;
  const text = await resp.text();
  if (text) {
    try { payload = JSON.parse(text); } catch (_e) { payload = { error: text }; }
  }
  if (!resp.ok) {
    const err = new Error((payload && (payload.error || payload.errors && payload.errors.join("；")))
      || `请求失败（${resp.status}）`);
    err.status = resp.status;
    err.payload = payload;
    throw err;
  }
  return payload;
}

async function refresh() {
  try {
    DATA = await api("/api/state");
    render();
  } catch (e) {
    $("matrix-slot").innerHTML =
      `<p class="loading" style="color:var(--red)">载入状态失败：${esc(e.message)}</p>`;
  }
}

/* ---------------- 渲染：顶部元信息 ---------------- */
function renderMeta() {
  const t = DATA.table;
  $("table-meta").textContent =
    `${t.title} · ${t.faults.length} 种故障 · ${t.checks.length} 项检查 · 重启耗时 ${fmtDuration(t.restart_seconds)}`;
  document.title = t.title || "黑屏排查";
}

/* ---------------- 渲染：状态横幅（无匹配 / 已查明） ---------------- */
function renderBanner() {
  const a = DATA.analysis;
  const slot = $("banner-slot");

  if (a.status === "single" && a.fault) {
    const f = a.fault;
    slot.innerHTML = `
      <div class="hero hero-green">
        <h2>✅ 只剩一种故障，已查明</h2>
        <div class="fault-result">
          <div><span class="fid">${esc(f.id)}</span><span class="fname">${esc(f.name)}</span></div>
          ${f.hint ? `<p class="hint">🛠 处置建议：${esc(f.hint)}</p>` : ""}
        </div>
        <div style="margin-top:12px">
          <button class="btn-solid" id="btn-new-case">🔄 开始下一台排查</button>
        </div>
      </div>`;
    $("btn-new-case").onclick = doReset;
    return;
  }

  if (a.status === "none") {
    const bad = (a.diagnostics || []).filter((d) => !d.matches_any);
    const conflict = bad.length === 0 && (a.diagnostics || []).length > 0;
    slot.innerHTML = `
      <div class="hero hero-red">
        <h2>⚠️ 实测读数与所有故障都不符，请核对</h2>
        <p class="lead">当前没有任何故障能同时满足已录入的读数。请检查测量方法、单位、取值或是否有录入错误（标红的输入框），可直接更正旧读数。</p>
        ${bad.length ? `<ul class="diagnostic-list">
          ${bad.map((d) => `<li class="bad">「${esc(d.check_id)}」录入的 <strong>${esc(d.value)}</strong> 不在该检查任何故障的预期读数中。</li>`).join("")}
        </ul>` : ""}
        ${conflict ? `<p class="lead" style="margin-top:8px">每条读数单独看都能对应某些故障，但没有故障能同时满足全部读数，存在相互矛盾的检查结果。</p>` : ""}
      </div>`;
    return;
  }

  slot.innerHTML = "";
}

/* ---------------- 渲染：推荐下一步 ---------------- */
function nextStepLabel(n) {
  if (!n) return "—";
  if (n.kind === "resolved" && n.fault) return `已查明：${n.fault.id} ${n.fault.name}`;
  if (n.kind === "check" && n.check) return `做 ${n.check.id}「${n.check.name}」（${fmtDuration(n.seconds)}）`;
  if (n.kind === "restart") return `登记重启（${fmtDuration(n.seconds)}）`;
  if (n.kind === "stop") return "结束检查，汇总分不清的故障组";
  return n.kind;
}

function renderRecommendation() {
  const a = DATA.analysis;
  const slot = $("recommend-slot");
  if (a.status !== "ready" || !a.recommendation) {
    slot.innerHTML = "";
    return;
  }
  const r = a.recommendation;
  const guaranteed = !!a.guaranteed;
  const cls = guaranteed ? "hero-blue" : "hero-amber";

  let actionHtml = "";
  if (r.kind === "check" && r.check) {
    const c = r.check;
    actionHtml = `
      <div class="rec-action">
        <div class="rec-icon">🔎</div>
        <div>
          <div class="rec-title">${esc(c.id)} · ${esc(c.name)}</div>
          <div class="rec-meta">
            <span class="badge ${c.phase === "post" ? "badge-post" : "badge-pre"}">${phaseLabel(c.phase)}</span>
            <span>预计耗时 ${fmtDuration(c.seconds)}</span>
          </div>
        </div>
        <div class="rec-time">
          <div class="num">${guaranteed ? fmtDuration(a.worst_remaining_seconds) : fmtDuration(a.policy_worst_seconds)}</div>
          <div class="cap">${guaranteed ? "最坏剩余耗时" : "最坏到结论/分组"}</div>
        </div>
      </div>
      <button class="btn-solid" id="btn-locate-${esc(c.id)}">👇 定位并录入该检查</button>`;
  } else if (r.kind === "restart") {
    actionHtml = `
      <div class="rec-action">
        <div class="rec-icon">🔁</div>
        <div>
          <div class="rec-title">登记并执行一次重启</div>
          <div class="rec-meta"><span>重启耗时 ${fmtDuration(r.seconds || DATA.table.restart_seconds)}</span>
          <span class="badge badge-warn">最多一次 · 重启后不能补做重启前检查</span></div>
        </div>
        <div class="rec-time">
          <div class="num">${guaranteed ? fmtDuration(a.worst_remaining_seconds) : fmtDuration(a.policy_worst_seconds)}</div>
          <div class="cap">${guaranteed ? "最坏剩余耗时（含重启）" : "最坏到结论/分组"}</div>
        </div>
      </div>
      <button class="btn-solid btn-restart" id="btn-restart-hero">🔴 登记重启（仅一次）</button>`;
  } else {
    actionHtml = `
      <div class="rec-action">
        <div class="rec-icon">🛑</div>
        <div><div class="rec-title">剩余检查已无法进一步区分当前故障</div>
        <div class="rec-meta">建议结束排查，按下方故障组安排人工复核/替换验证</div></div>
      </div>`;
  }

  let branchHtml = "";
  if (r.kind === "check" && Array.isArray(r.branches) && r.branches.length) {
    branchHtml = `
      <details class="branch-preview">
        <summary>查看适应性预案：不同读数后的下一步（${r.branches.length} 种预期结果）</summary>
        <ul>
          ${r.branches.map((b) => `
            <li>若读数为 <strong>${esc(b.reading)}</strong>
              <span class="muted">→ ${b.candidate_count} 种候选 [${b.candidate_ids.map(esc).join(", ")}]</span>
              <span class="arrow">⇒</span> ${esc(nextStepLabel(b.next))}
            </li>`).join("")}
        </ul>
      </details>`;
  } else if (r.kind === "restart" && r.next_after_restart) {
    branchHtml = `<p class="branch-preview" style="display:block">重启后的首选：<strong>${esc(nextStepLabel(r.next_after_restart))}</strong></p>`;
  }

  const heading = guaranteed
    ? "🧭 推荐下一步（最坏剩余耗时最短）"
    : "⚠️ 无法保证唯一查明 — 尽力缩小范围的推荐";
  const lead = guaranteed
    ? `极小极大计算：按推荐执行并随读数调整，无论实际是哪种故障，最坏再用 <strong>${fmtDuration(a.worst_remaining_seconds)}</strong> 即可唯一查明。`
    : `受阶段与“最多一次重启”限制，不存在保证唯一查明的检查顺序。下面优先把最坏情况下仍分不清的故障组降到最小，预计 <strong>${fmtDuration(a.policy_worst_seconds)}</strong> 后可得到确定结果或下方分组。`;

  slot.innerHTML = `
    <div class="hero ${cls}">
      <h2>${heading}</h2>
      <p class="lead">${lead}</p>
      ${actionHtml}
      ${branchHtml}
    </div>`;

  if (r.kind === "check" && r.check) {
    const btn = $(`btn-locate-${r.check.id}`);
    if (btn) btn.onclick = () => {
      const input = $(`input-${r.check.id}`);
      const row = $(`row-${r.check.id}`);
      if (row) row.scrollIntoView({ behavior: "smooth", block: "center" });
      if (input && !input.disabled) setTimeout(() => input.focus(), 250);
    };
  }
  const rb = $("btn-restart-hero");
  if (rb) rb.onclick = doRestart;
}

/* ---------------- 渲染：歧义组 ---------------- */
function renderGroups() {
  const groups = DATA.analysis.ambiguous_groups || [];
  const slot = $("groups-slot");
  if (!groups.length) { slot.innerHTML = ""; return; }
  slot.innerHTML = `
    <section class="hero hero-amber">
      <h2>🧩 仍分不清的故障组（${groups.length} 组）</h2>
      <p class="lead">即使把当前仍可做/仍可达的检查全部做完，同组故障的预期读数也完全一致，无法保证唯一查明：</p>
      <div class="group-list">
        ${groups.map((g, i) => `
          <div class="group-row">
            <div class="group-label">故障组 ${i + 1} · ${g.length} 种</div>
            <div class="chips">
              ${g.map((f) => `<span class="chip"><span class="cid-tag">${esc(f.id)}</span>${esc(f.name)}</span>`).join("")}
            </div>
          </div>`).join("")}
      </div>
    </section>`;
}

/* ---------------- 渲染：读数矩阵 + 录入 ---------------- */
function renderMatrix() {
  const t = DATA.table;
  const a = DATA.analysis;
  const readings = DATA.state.readings || {};
  const touched = new Set(DATA.state.touched || []);
  const restarted = !!DATA.state.restarted;
  const candidateIds = new Set(a.candidate_ids || []);
  const dimEnabled = a.status !== "none" && candidateIds.size > 0;
  const diagByCheck = {};
  (a.diagnostics || []).forEach((d) => { diagByCheck[d.check_id] = d; });

  const head = `
    <thead><tr>
      <th class="entry-head">检查项 / 实测录入</th>
      ${t.faults.map((f) => `
        <th class="fhead ${dimEnabled && !candidateIds.has(f.id) ? "dim" : ""}">
          <span class="fid">${esc(f.id)}</span>${esc(f.name)}
        </th>`).join("")}
    </tr></thead>`;

  const rows = t.checks.map((c) => {
    const val = readings[c.id] != null ? readings[c.id] : "";
    const diag = diagByCheck[c.id];
    const recorded = String(val).trim() !== "";
    const invalid = recorded && diag && !diag.matches_any;

    let disabled = false;
    let placeholder = "录入/选择实测读数";
    let lockBadge = "";
    if (c.phase === "post" && !restarted) {
      disabled = true;
      placeholder = "🔒 重启后才能做";
      lockBadge = `<span class="badge badge-lock">待重启</span>`;
    } else if (c.phase === "pre" && restarted && !touched.has(c.id)) {
      disabled = true;
      placeholder = "🔒 已重启，不能补做";
      lockBadge = `<span class="badge badge-lock">已错过</span>`;
    } else if (c.phase === "pre" && restarted && touched.has(c.id)) {
      lockBadge = `<span class="badge badge-done">可更正</span>`;
    }

    const matchIds = new Set(diag ? diag.matching_fault_ids : []);
    const warn = invalid
      ? `<div class="row-warn">该读数不在本检查的预期取值中，请核对（可直接更正）。</div>`
      : (recorded && c.phase === "pre" && restarted
          ? `<div class="row-hint-ok">已保留的重启前读数，可更正；重启状态与其他读数不受影响。</div>`
          : "");

    return `
      <tbody id="row-${esc(c.id)}" class="${recorded ? "row-recorded" : ""} ${invalid ? "row-invalid" : ""}">
        <tr>
          <td class="entry-cell">
            <div class="check-name">
              <span class="check-id">${esc(c.id)}</span>${esc(c.name)}
              <span class="badge ${c.phase === "post" ? "badge-post" : "badge-pre"}">${phaseLabel(c.phase)}</span>
              <span class="muted" style="font-weight:400;font-size:12px">${fmtDuration(c.seconds)}</span>
              ${lockBadge}
            </div>
            ${c.detail ? `<div class="check-detail">${esc(c.detail)}</div>` : ""}
            <div class="entry-row">
              <input list="dl-${esc(c.id)}" id="input-${esc(c.id)}" data-cid="${esc(c.id)}"
                value="${esc(val)}" placeholder="${esc(placeholder)}"
                ${disabled ? "disabled" : ""} autocomplete="off"
                class="${invalid ? "invalid" : ""}" />
              <datalist id="dl-${esc(c.id)}">
                ${(c.options || []).map((o) => `<option value="${esc(o)}"></option>`).join("")}
              </datalist>
              <button class="btn-clear" data-clear="${esc(c.id)}" title="清空该读数（保留其他读数）">✕</button>
            </div>
            ${warn}
          </td>
          ${t.faults.map((f) => {
            const isCand = !dimEnabled || candidateIds.has(f.id);
            const isHit = recorded && matchIds.has(f.id);
            return `<td class="exp-cell ${isCand ? "candidate" : "dim"} ${isHit ? "hit" : ""}">${esc(c.expected[f.id] || "—")}</td>`;
          }).join("")}
        </tr>
      </tbody>`;
  }).join("");

  $("matrix-slot").innerHTML = `<table class="matrix">${head}${rows}</table>`;

  // 绑定录入事件
  $("matrix-slot").querySelectorAll("input[data-cid]").forEach((inp) => {
    inp.addEventListener("change", () => saveReading(inp.dataset.cid, inp.value, false));
    inp.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") {
        ev.preventDefault();
        saveReading(inp.dataset.cid, inp.value, true);
      }
    });
  });
  $("matrix-slot").querySelectorAll("button[data-clear]").forEach((btn) => {
    btn.addEventListener("click", () => clearReading(btn.dataset.clear));
  });
}

/* ---------------- 渲染：侧栏状态/耗时/候选 ---------------- */
function renderSide() {
  const t = DATA.table;
  const s = DATA.state;
  const a = DATA.analysis;
  const restarted = !!s.restarted;

  $("phase-card").innerHTML = `
    <h2>现场阶段</h2>
    <span class="phase-pill ${restarted ? "phase-post" : "phase-pre"}">
      ${restarted ? "🔁 已重启 · 重启后阶段" : "🕓 重启前阶段"}
    </span>
    <div class="restart-box">
      ${restarted ? `
        <div><span class="badge badge-done">已登记重启（仅一次）</span></div>
        <div class="kv"><span class="k">重启耗时</span><span class="val">${fmtDuration(t.restart_seconds)}</span></div>
        <div class="restart-clock" id="restart-tick"
          data-registered="${s.restart_registered_at || ""}"
          data-server-now="${DATA.server_now || ""}">重启已计时：00:00</div>
      ` : `
        <p class="muted" style="margin:6px 0 0">重启前检查请尽快记录；一旦重启，未录入的重启前检查将无法补做。</p>
        <button class="btn-solid btn-restart" id="btn-restart-side">🔴 登记重启（耗时 ${fmtDuration(t.restart_seconds)}，仅一次）</button>
      `}
    </div>`;
  const sideBtn = $("btn-restart-side");
  if (sideBtn) sideBtn.onclick = doRestart;

  const spent = DATA.spent_seconds || 0;
  const wr = a.worst_remaining_seconds;
  $("time-card").innerHTML = `
    <h2>耗时账</h2>
    <div class="kv"><span class="k">已发生标准耗时</span><span class="val">${fmtDuration(spent)}</span></div>
    <div class="kv"><span class="k">最坏剩余耗时</span>
      <span class="val">${wr == null ? "不能保证查明" : fmtDuration(wr)}</span></div>
    ${wr != null ? `<div class="kv"><span class="k">最坏总耗时（已用+剩余）</span>
      <span class="val">${fmtDuration(spent + wr)}</span></div>` : ""}
    <p class="muted" style="margin:8px 0 0;font-size:12px">已发生耗时＝已录入检查耗时＋${restarted ? "已发生的重启耗时" : "0"}；推荐只优化“剩余”部分。</p>`;

  const cand = a.candidates || [];
  $("candidate-card").innerHTML = `
    <h2>当前候选故障（${cand.length}）</h2>
    ${cand.length ? `<div class="chips">
      ${cand.map((f) => `<span class="chip"><span class="cid-tag">${esc(f.id)}</span>${esc(f.name)}</span>`).join("")}
    </div>` : `<p class="muted" style="margin:4px 0 0">无候选 — 请核对读数。</p>`}
    ${a.status === "ready" && a.guaranteed ? `<p style="margin:10px 0 0"><span class="badge badge-ok">✅ 可保证唯一查明</span></p>` : ""}
    ${a.status === "ready" && !a.guaranteed ? `<p style="margin:10px 0 0"><span class="badge badge-warn">⚠️ 无法保证唯一查明</span></p>` : ""}`;
}

function render() {
  if (!DATA) return;
  renderMeta();
  renderBanner();
  renderRecommendation();
  renderGroups();
  renderMatrix();
  renderSide();
}

/* ---------------- 操作 ---------------- */
async function saveReading(cid, value, refocus) {
  try {
    DATA = await api(`/api/readings/${encodeURIComponent(cid)}`, {
      method: "PUT",
      body: JSON.stringify({ value }),
    });
    render();
    if (refocus) {
      const inp = $(`input-${cid}`);
      if (inp && !inp.disabled) {
        inp.focus();
        const len = inp.value.length;
        try { inp.setSelectionRange(len, len); } catch (_e) {}
      }
    }
  } catch (e) {
    toast(e.message, true);
    refresh();
  }
}

async function clearReading(cid) {
  try {
    DATA = await api(`/api/readings/${encodeURIComponent(cid)}`, { method: "DELETE" });
    render();
    const inp = $(`input-${cid}`);
    if (inp && !inp.disabled) inp.focus();
  } catch (e) {
    toast(e.message, true);
  }
}

async function doRestart() {
  try {
    DATA = await api("/api/restart", { method: "POST" });
    render();
    toast("已登记重启：重启耗时已计入，重启后检查已解锁。");
  } catch (e) {
    toast(e.message, true);
    if (e.status === 409) refresh();
  }
}

async function doReset() {
  try {
    DATA = await api("/api/reset", { method: "POST" });
    render();
    toast("已重置，可以开始下一台排查。");
  } catch (e) {
    toast(e.message, true);
  }
}

/* ---------------- JSON 载入 ---------------- */
$("btn-upload").addEventListener("click", () => $("file-json").click());

$("file-json").addEventListener("change", async (ev) => {
  const file = ev.target.files && ev.target.files[0];
  if (!file) return;
  const errBox = $("load-error");
  errBox.hidden = true;
  try {
    const text = await file.text();
    let table;
    try { table = JSON.parse(text); }
    catch (e) { throw new Error(`JSON 解析失败：${e.message}`); }
    const payload = await api("/api/table/load", {
      method: "POST",
      body: JSON.stringify(table),
    });
    DATA = payload;
    render();
    toast(`已载入故障表：${DATA.table.title}`);
  } catch (e) {
    let msg = e.message;
    if (e.payload && e.payload.errors) msg = e.payload.errors.join("\n");
    errBox.textContent = "故障表载入失败：\n" + msg;
    errBox.hidden = false;
  } finally {
    ev.target.value = "";
  }
});

$("btn-restore").addEventListener("click", async () => {
  try {
    const raw = await (await fetch("/api/table/sample")).json();
    DATA = await api("/api/table/load", { method: "POST", body: JSON.stringify(raw) });
    render();
    toast("已恢复为服务端默认故障表。");
  } catch (e) {
    toast("恢复失败：" + e.message, true);
  }
});

$("btn-reset").addEventListener("click", doReset);

/* ---------------- 重启计时器（补充展示，不参与计算） ---------------- */
setInterval(() => {
  const tick = $("restart-tick");
  if (!tick) return;
  const registered = parseFloat(tick.dataset.registered);
  const serverNow = parseFloat(tick.dataset.serverNow);
  if (!registered) return;
  const offset = isFinite(serverNow) ? serverNow - Date.now() / 1000 : 0;
  let sec = Math.max(0, Math.floor(Date.now() / 1000 + offset - registered));
  const mm = String(Math.floor(sec / 60)).padStart(2, "0");
  const ss = String(sec % 60).padStart(2, "0");
  tick.textContent = `⏱ 重启已计时 ${mm}:${ss}（标准重启耗时 ${fmtDuration(DATA ? DATA.table.restart_seconds : null)} 已计入）`;
}, 1000);

refresh();
