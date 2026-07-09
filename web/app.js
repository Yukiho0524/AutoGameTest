"use strict";
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const api = async (path, opts) => {
  const r = await fetch(path, { cache: "no-store", ...(opts || {}) });
  const ct = r.headers.get("content-type") || "";
  return ct.includes("json") ? r.json() : r;
};

let GAMES = [];
let AGENTS = [];
let SCHEDULES = [];
let SELECTED_JOB_ID = null;
let SETTINGS = {};
let SCRIPTS = [];
let JOB_STATUS_CACHE = new Map();
let JOB_NOTIFY_POLL_STARTED = false;
let JOBS_SEEDED = false;
let JOBS_LOADING = false;
const DEFAULT_CODEX_MODEL = "gpt-5.5";
const DEFAULT_CODEX_REASONING_EFFORT = "high";
const DAYS = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"];

// ---------- tabs ----------
$$(".tab").forEach(t => t.onclick = () => {
  $$(".tab").forEach(x => x.classList.remove("active"));
  $$(".panel").forEach(x => x.classList.remove("active"));
  t.classList.add("active");
  $("#" + t.dataset.tab).classList.add("active");
  if (t.dataset.tab === "control") loadInstances();
  if (t.dataset.tab === "jobs") loadJobs();
  if (t.dataset.tab === "agents") loadAgents();
  if (t.dataset.tab === "scripts") loadScripts();
  if (t.dataset.tab === "schedule") loadSchedule();
  if (t.dataset.tab === "diagnostics") loadDiagnostics();
  if (t.dataset.tab === "settings") loadSettings();
});

// ---------- games ----------
async function loadGames() {
  const { games } = await api("/api/games");
  GAMES = games;
  const list = $("#game-list");
  list.innerHTML = "";
  games.forEach(g => {
    const div = document.createElement("div");
    div.className = "card";
    div.innerHTML = `
      <h3>${esc(g.name)}</h3>
      <div class="meta">
        <span class="badge">${platformLabel(g.platform)}</span>
        <span class="badge">${g.control === "emulator" ? "模擬器" : "桌面"}</span>
        ${g.verified ? '<span class="badge ok">✓ 已驗證</span>' : ""}
      </div>
      <div class="row">
        <button class="small" data-act="launch">▶ 啟動</button>
        <button class="small" data-act="edit">編輯</button>
        <button class="small danger" data-act="del">刪除</button>
      </div>`;
    div.querySelector('[data-act=launch]').onclick = () => launchGame(g.id);
    div.querySelector('[data-act=edit]').onclick = () => editGame(g);
    div.querySelector('[data-act=del]').onclick = () => delGame(g.id);
    list.appendChild(div);
  });
  fillAgentGameSelect();
}

function platformLabel(p) {
  return { steam: "Steam", epic: "Epic", xbox: "Xbox", pc: "PC", android: "Android" }[p] || p;
}

function emulatorLabel(e) {
  return { ldplayer: "LDPlayer", bluestacks: "BlueStacks" }[e] || e || "Emulator";
}

function defaultSerialFor(emulator, instance = 0) {
  return emulator === "bluestacks" ? "127.0.0.1:5555" : `emulator-${5554 + (+instance || 0) * 2}`;
}

async function launchGame(id) {
  const r = await api(`/api/games/${id}/launch`, { method: "POST" });
  alert(r.ok ? `啟動成功（${r.method}）\n${r.detail}` : `啟動失敗：${r.detail}`);
}

async function delGame(id) {
  if (!confirm("確定刪除這個遊戲？相關 Agent 也會一併移除。")) return;
  await api(`/api/games/${id}`, { method: "DELETE" });
  loadGames();
}

function editGame(g) {
  const f = $("#game-form");
  $("#form-title").textContent = "編輯遊戲";
  f.id.value = g.id;
  f.name.value = g.name || "";
  const lc = g.launch || {};
  f.exe_path.value = lc.exe_path || "";
  f.platform.value = g.platform || "pc";
  f.steam_appid.value = lc.steam_appid || "";
  f.epic_app_name.value = lc.epic_app_name || "";
  f.aumid.value = lc.aumid || "";
  f.window_title.value = lc.window_title || "";
  f.cu_app_name.value = lc.cu_app_name || "";
  f.emulator.value = lc.emulator || "ldplayer";
  f.instance.value = lc.instance ?? 0;
  f.serial.value = lc.serial || "";
  f.package.value = lc.package || "";
  f.learn_sources.value = (g.learn_sources || []).join("\n");
  f.auto_learn.checked = false;
  applyPlatformFields();
  $("#learn-box").hidden = false;
  $("#learn-sources").value = (g.learn_sources || []).join("\n");
  $("#learn-box").dataset.gid = g.id;
  window.scrollTo(0, 0);
}

$("#detect-btn").onclick = async () => {
  const exe = $("#game-form").exe_path.value.trim();
  if (!exe) return;
  const r = await api("/api/detect-platform", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ exe_path: exe }),
  });
  $("#game-form").platform.value = r.platform;
  if (r.hints && r.hints.steam_appid) $("#game-form").steam_appid.value = r.hints.steam_appid;
  $("#platform-hint").textContent =
    `偵測結果：${r.label}（${r.control === "emulator" ? "模擬器" : "桌面"}控制）` +
    (r.hints && r.hints.steam_appid ? `，AppID ${r.hints.steam_appid}` : "");
  applyPlatformFields();
};

$("#platform-sel").onchange = applyPlatformFields;
$("#emulator-sel").onchange = () => {
  const f = $("#game-form");
  f.serial.placeholder = `留空自動帶入：${defaultSerialFor(f.emulator.value, f.instance.value)}`;
};
function applyPlatformFields() {
  const p = $("#game-form").platform.value;
  const control = p === "android" ? "emulator" : "desktop";
  $$(".platform-fields").forEach(el => el.hidden = el.dataset.control !== control);
  $("#appid-field").hidden = p !== "steam";
  $("#epic-field").hidden = p !== "epic";
  $("#aumid-field").hidden = p !== "xbox";
}

$("#pkg-btn").onclick = async () => {
  const emulator = $("#game-form").emulator.value || "ldplayer";
  const inst = $("#game-form").instance.value || 0;
  const serial = $("#game-form").serial.value.trim() || defaultSerialFor(emulator, inst);
  const { packages } = await api(`/api/emulator/packages?emulator=${encodeURIComponent(emulator)}&serial=${encodeURIComponent(serial)}`);
  const dl = $("#pkg-list");
  dl.innerHTML = "";
  packages.forEach(p => { const o = document.createElement("option"); o.value = p; dl.appendChild(o); });
  $("#game-form").package.setAttribute("list", "pkg-list");
  $("#platform-hint").textContent = `已載入 ${packages.length} 個已安裝套件（點欄位有下拉建議）`;
};

$("#game-form").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target;
  const p = f.platform.value;
  const control = p === "android" ? "emulator" : "desktop";
  const launch = control === "emulator"
    ? { emulator: f.emulator.value, instance: +f.instance.value,
        serial: f.serial.value.trim() || defaultSerialFor(f.emulator.value, f.instance.value),
        package: f.package.value.trim() }
    : { exe_path: f.exe_path.value.trim(), steam_appid: f.steam_appid.value.trim(),
        epic_app_name: f.epic_app_name.value.trim(), aumid: f.aumid.value.trim(),
        window_title: f.window_title.value.trim(), cu_app_name: f.cu_app_name.value.trim() };
  const learnSources = f.learn_sources.value.split("\n").map(s => s.trim()).filter(Boolean);
  const autoLearn = f.auto_learn.checked;
  const game = { id: f.id.value || undefined, name: f.name.value.trim(),
                 platform: p, control, launch, learn_sources: learnSources };
  const saved = await api("/api/games", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(game),
  });
  let learnMsg = "";
  if (autoLearn) {
    const job = await api(`/api/games/${saved.id}/learn`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sources: learnSources, engine: "codex" }),
    });
    learnMsg = job.spawned ? `，已開始學習任務 #${job.id}` : `，但學習任務 #${job.id} 未能自動啟動`;
  }
  resetGameForm();
  loadGames();
  loadJobs();
  $("#platform-hint").textContent = `已儲存「${saved.name}」${learnMsg}`;
};

$("#form-reset").onclick = resetGameForm;
function resetGameForm() {
  $("#game-form").reset();
  $("#game-form").id.value = "";
  $("#game-form").auto_learn.checked = true;
  $("#form-title").textContent = "新增遊戲";
  $("#learn-box").hidden = true;
  applyPlatformFields();
}

$("#learn-btn").onclick = async () => {
  const gid = $("#learn-box").dataset.gid;
  const sources = $("#learn-sources").value.split("\n").map(s => s.trim()).filter(Boolean);
  if (!gid) { alert("請先儲存遊戲再學習"); return; }
  const job = await api(`/api/games/${gid}/learn`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sources, engine: "codex" }),
  });
  $("#learn-status").textContent = job.spawned
    ? `已開始學習任務 #${job.id}。完成後會更新該遊戲的 Skill。`
    : `已建立學習任務 #${job.id}，但無法自動啟動執行器。可在終端手動跑：python tools/run_learn.py --job ${job.id}`;
  loadJobs();
};

// ---------- emulator control ----------
async function loadInstances() {
  const { available, instances } = await api("/api/emulator/instances");
  const sel = $("#serial-sel");
  sel.innerHTML = "";
  if (!available) { $("#screen-status").textContent = "找不到可用模擬器/adb"; return; }
  instances.forEach(i => {
    const serial = i.serial || defaultSerialFor(i.emulator, i.index);
    const o = document.createElement("option");
    o.value = serial;
    o.dataset.emulator = i.emulator || "ldplayer";
    o.textContent = `${emulatorLabel(o.dataset.emulator)} [${i.index}] ${i.title} ${serial} ${i.running ? "▶ 執行中" : "⏸ 未啟動"}`;
    sel.appendChild(o);
  });
  loadRecordStatus();
}
$("#refresh-shot").onclick = refreshShot;
function refreshShot() {
  const serial = $("#serial-sel").value;
  if (!serial) return;
  const emulator = $("#serial-sel").selectedOptions[0]?.dataset.emulator || "";
  const img = $("#screen");
  img.onload = () => { img.style.display = "block"; $("#screen-status").textContent = ""; };
  img.onerror = () => { $("#screen-status").textContent = "截圖失敗（模擬器是否已開機？）"; };
  img.src = `/api/emulator/screenshot?serial=${encodeURIComponent(serial)}&emulator=${encodeURIComponent(emulator)}&t=${Date.now()}`;
}
$("#screen").onclick = async (e) => {
  const img = e.target;
  const rect = img.getBoundingClientRect();
  const x = Math.round((e.clientX - rect.left) / rect.width * img.naturalWidth);
  const y = Math.round((e.clientY - rect.top) / rect.height * img.naturalHeight);
  await api("/api/emulator/tap", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      serial: $("#serial-sel").value,
      emulator: $("#serial-sel").selectedOptions[0]?.dataset.emulator || "",
      x, y
    }),
  });
  setTimeout(refreshShot, 400);
};
let autoTimer = null;
$("#auto-refresh").onchange = (e) => {
  clearInterval(autoTimer);
  if (e.target.checked) autoTimer = setInterval(refreshShot, 2000);
};

// ---------- emulator recording ----------
let recTimer = null;

function fmtElapsed(sec) {
  const s = Math.max(0, Math.round(sec));
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

function setRecUI(recording) {
  const btn = $("#rec-toggle");
  btn.textContent = recording ? "⏹ 停止錄影" : "⏺ 開始錄影";
  btn.classList.toggle("recording", recording);
  $("#rec-dir").disabled = recording;
  $("#rec-touches").disabled = recording;
}

async function loadRecordStatus() {
  const serial = $("#serial-sel").value;
  const st = await api(`/api/emulator/record/status?serial=${encodeURIComponent(serial || "")}`);
  if (!$("#rec-dir").value && st.default_dir) $("#rec-dir").value = st.default_dir;
  setRecUI(st.recording);
  if (st.recording) {
    $("#rec-status").textContent = `錄影中 ${fmtElapsed(st.elapsed)}（第 ${st.parts} 段）→ ${st.save_dir}`;
    startRecPoll();
  } else if (st.error) {
    $("#rec-status").textContent = `錄影異常：${st.error}`;
  }
}

function startRecPoll() {
  clearInterval(recTimer);
  recTimer = setInterval(async () => {
    const serial = $("#serial-sel").value;
    const st = await api(`/api/emulator/record/status?serial=${encodeURIComponent(serial || "")}`);
    if (st.recording) {
      $("#rec-status").textContent = `錄影中 ${fmtElapsed(st.elapsed)}（第 ${st.parts} 段）→ ${st.save_dir}`;
    } else {
      clearInterval(recTimer);
      setRecUI(false);
      if (st.error) $("#rec-status").textContent = `錄影異常：${st.error}`;
    }
  }, 2000);
}

$("#rec-toggle").onclick = async () => {
  const serial = $("#serial-sel").value;
  if (!serial) { alert("請先選擇裝置"); return; }
  const emulator = $("#serial-sel").selectedOptions[0]?.dataset.emulator || "";
  const btn = $("#rec-toggle");
  if (btn.classList.contains("recording")) {
    btn.disabled = true;
    $("#rec-status").textContent = "收尾中（等待最後片段寫檔）…";
    const r = await api("/api/emulator/record/stop", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ serial }),
    });
    btn.disabled = false;
    clearInterval(recTimer);
    setRecUI(false);
    if (r.ok) {
      const where = r.video || r.dir;
      $("#rec-status").textContent =
        `已儲存（${fmtElapsed(r.elapsed)}，${r.n_parts} 段）：${where}`;
    } else {
      $("#rec-status").textContent = `錄影失敗：${r.error || "未知錯誤"}`;
    }
  } else {
    const r = await api("/api/emulator/record/start", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        serial, emulator,
        save_dir: $("#rec-dir").value.trim(),
        show_touches: $("#rec-touches").checked,
      }),
    });
    if (r.ok) {
      $("#rec-dir").value = r.save_dir;
      setRecUI(true);
      $("#rec-status").textContent = `錄影中 00:00 → ${r.save_dir}`;
      startRecPoll();
    } else {
      $("#rec-status").textContent = `無法開始：${r.error || "未知錯誤"}`;
    }
  }
};

$("#rec-open-dir").onclick = async () => {
  const r = await api("/api/emulator/record/open-folder", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ dir: $("#rec-dir").value.trim() }),
  });
  if (!r.ok) $("#rec-status").textContent = r.error || "無法開啟資料夾";
};

// ---------- agents ----------
function fillAgentGameSelect() {
  const sel = $("#agent-game");
  if (!sel) return;
  sel.innerHTML = "";
  GAMES.forEach(g => {
    const o = document.createElement("option"); o.value = g.id; o.textContent = g.name;
    sel.appendChild(o);
  });
}
async function loadAgents() {
  const { agents } = await api("/api/agents");
  AGENTS = agents;
  const list = $("#agent-list");
  list.innerHTML = "";
  agents.forEach(a => {
    const g = GAMES.find(x => x.id === a.game_id);
    const div = document.createElement("div");
    div.className = "card";
    div.innerHTML = `
      <h3>${esc(a.name)}</h3>
      <div class="meta">
        <span class="badge">${esc(g ? g.name : a.game_id)}</span>
        ${a.notify_on_done !== false ? '<span class="badge ok">完成通知</span>' : '<span class="badge">不通知</span>'}
      </div>
      <p class="hint">${esc(a.prompt)}</p>
      <div class="row">
        <button class="small" data-act="run">▶ 執行</button>
        <button class="small" data-act="edit">編輯</button>
        <button class="small danger" data-act="del">刪除</button>
      </div>`;
    div.querySelector('[data-act=run]').onclick = () => runAgent(a.id);
    div.querySelector('[data-act=edit]').onclick = () => editAgent(a);
    div.querySelector('[data-act=del]').onclick = () => delAgent(a.id);
    list.appendChild(div);
  });
}

// ---------- scripts ----------
async function loadScripts() {
  const r = await api("/api/scripts");
  SCRIPTS = r.scripts || [];
  const list = $("#script-list");
  list.innerHTML = SCRIPTS.length ? "" :
    '<p class="hint">還沒有腳本。先在「模擬器操控」錄一段操作（錄影中直接點畫面），再從右邊生成。</p>';
  SCRIPTS.forEach(s => {
    const div = document.createElement("div");
    div.className = "card";
    const genBadge = s.generated_by === "codex"
      ? '<span class="badge ok">AI 已註解</span>'
      : '<span class="badge">草稿骨架</span>';
    const riskBadge = Number(s.risk_count || 0) > 0
      ? `<span class="badge warn">高風險 ${Number(s.risk_count || 0)}</span>`
      : "";
    const visionBadge = Number(s.vision_count || 0) > 0
      ? `<span class="badge ok">圖片匹配 ${Number(s.vision_count || 0)}</span>`
      : "";
    div.innerHTML = `
      <h3>${esc(s.name)}</h3>
      <div class="meta">
        ${genBadge}
        ${riskBadge}
        ${visionBadge}
        <span class="badge">${s.n_steps} 步</span>
        <span class="badge">${esc(s.emulator || "")}</span>
      </div>
      <p class="hint">${esc(s.description || "")}</p>
      <div class="meta">來源：${esc(baseName(s.source))} · ${esc(s.created || "")}</div>
      <div class="row">
        <button class="small" data-act="run">▶ 執行（無 AI）</button>
        <button class="small" data-act="view">查看/編輯</button>
        <button class="small danger" data-act="del">刪除</button>
      </div>`;
    div.querySelector('[data-act=run]').onclick = () => runScript(s.id);
    div.querySelector('[data-act=view]').onclick = () => viewScript(s.id);
    div.querySelector('[data-act=del]').onclick = () => delScript(s.id);
    list.appendChild(div);
  });
  loadRecordingsForGen();
}

function baseName(p) {
  return String(p || "").split(/[\\/]/).pop();
}

function jobKindLabel(kind) {
  return {
    learn: "📖 學習",
    run_agent: "🕹 執行 Agent",
    autotune_agent: "🛠 效能調整",
    genscript: "🎬 生成腳本（AI）",
    run_script: "▶ 執行腳本（ADB）",
  }[kind] || `🧩 ${kind}`;
}

async function loadRecordingsForGen() {
  const { recordings } = await api("/api/recordings");
  const sel = $("#gen-source");
  sel.innerHTML = "";
  const usable = recordings || [];
  if (!usable.length) {
    const o = document.createElement("option");
    o.value = ""; o.textContent = "（找不到錄影，先去「模擬器操控」錄一段）";
    sel.appendChild(o);
    return;
  }
  usable.forEach(r => {
    const o = document.createElement("option");
    o.value = r.path;
    o.disabled = !r.has_taps;
    o.textContent = `${r.label} · ${r.mtime_text}` +
      (r.has_taps ? ` · ${r.n_taps} 次觸控` : " · ✗ 無觸控紀錄");
    sel.appendChild(o);
  });
  const first = usable.find(r => r.has_taps);
  if (first) sel.value = first.path;
}

$("#gen-refresh").onclick = loadRecordingsForGen;

$("#genscript-form").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target;
  const source = $("#gen-source").value;
  if (!source) { $("#gen-status").textContent = "請先選擇一段有觸控紀錄的錄影。"; return; }
  const job = await api("/api/scripts/generate", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      source,
      name: f.name.value.trim(),
      package: f.package.value.trim(),
    }),
  });
  if (job.error) { $("#gen-status").textContent = `無法生成：${job.error}`; return; }
  $("#gen-status").textContent = job.spawned
    ? `已建立生成任務 #${job.id}（Codex 註解中）。完成後腳本會出現在左邊清單，進度見任務佇列。`
    : `任務 #${job.id} 未能自動啟動，請查看任務佇列。`;
  loadJobs();
};

async function runScript(id) {
  const script = SCRIPTS.find(s => s.id === id);
  const riskCount = Number(script?.risk_count || 0);
  if (riskCount > 0) {
    const ok = confirm(`這個腳本包含 ${riskCount} 個高風險步驟，可能會抽卡、消耗資源或進入購買流程。\n\n確認仍要執行？`);
    if (!ok) return;
  }
  const job = await api(`/api/scripts/${id}/run`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ allow_risk: riskCount > 0 }),
  });
  alert(job.spawned
    ? `已開始執行腳本（任務 #${job.id}，ADB 圖片/座標重放、不用 AI）。\n進度與每步截圖見任務佇列。`
    : `任務 #${job.id} 未能自動啟動執行器。`);
}

async function viewScript(id) {
  const r = await api(`/api/scripts/${id}`);
  if (!r.script) return;
  $("#script-viewer").hidden = false;
  $("#script-viewer").dataset.sid = id;
  $("#script-viewer-title").textContent = `腳本內容：${r.script.name || id}`;
  $("#script-yaml").value = r.text || "";
  $("#script-viewer-status").textContent = "";
}

$("#script-save").onclick = async () => {
  const id = $("#script-viewer").dataset.sid;
  if (!id) return;
  const r = await api(`/api/scripts/${id}`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: $("#script-yaml").value }),
  });
  $("#script-viewer-status").textContent = r.ok ? "已儲存。" : `儲存失敗：${r.error}`;
  if (r.ok) loadScripts();
};
$("#script-viewer-close").onclick = () => { $("#script-viewer").hidden = true; };

async function delScript(id) {
  if (!confirm("刪除這個腳本？（排程中引用它的項目會失效）")) return;
  await api(`/api/scripts/${id}`, { method: "DELETE" });
  loadScripts();
}

// ---------- schedule ----------
async function loadSchedule() {
  if (!AGENTS.length) {
    const r = await api("/api/agents");
    AGENTS = r.agents;
  }
  const sr = await api("/api/scripts");
  SCRIPTS = sr.scripts || [];
  const { schedules } = await api("/api/schedules");
  SCHEDULES = schedules || [];
  renderScheduleAgents();
  renderScheduleScripts();
  renderScheduleBoard();
}

function renderScheduleScripts() {
  const list = $("#schedule-script-list");
  if (!list) return;
  list.innerHTML = SCRIPTS.length ? "" : '<p class="hint">目前沒有腳本。</p>';
  SCRIPTS.forEach(s => {
    const div = document.createElement("div");
    div.className = "agent-drag script-drag";
    div.draggable = true;
    div.innerHTML = `<strong>🎬 ${esc(s.name)}</strong><span>${s.n_steps} 步 · 無 AI 重放</span>`;
    div.ondragstart = e => {
      e.dataTransfer.setData("text/plain", `script:${s.id}`);
      e.dataTransfer.effectAllowed = "copy";
    };
    list.appendChild(div);
  });
}

function renderScheduleAgents() {
  const list = $("#schedule-agent-list");
  if (!list) return;
  list.innerHTML = AGENTS.length ? "" : '<p class="hint">目前沒有 Agent。</p>';
  AGENTS.forEach(a => {
    const g = GAMES.find(x => x.id === a.game_id);
    const div = document.createElement("div");
    div.className = "agent-drag";
    div.draggable = true;
    div.dataset.agentId = a.id;
    div.innerHTML = `<strong>${esc(a.name)}</strong><span>${esc(g ? g.name : a.game_id)}</span>`;
    div.ondragstart = e => {
      e.dataTransfer.setData("text/plain", a.id);
      e.dataTransfer.effectAllowed = "copy";
    };
    list.appendChild(div);
  });
}

function renderScheduleBoard() {
  const board = $("#schedule-board");
  if (!board) return;
  board.innerHTML = "";
  board.appendChild(scheduleHeaderCell("時間"));
  DAYS.forEach(d => board.appendChild(scheduleHeaderCell(d)));
  for (let hour = 0; hour < 24; hour++) {
    const time = document.createElement("div");
    time.className = "schedule-time";
    time.textContent = `${String(hour).padStart(2, "0")}:00`;
    board.appendChild(time);
    for (let day = 0; day < 7; day++) {
      const cell = document.createElement("div");
      cell.className = "schedule-cell";
      cell.dataset.day = day;
      cell.dataset.hour = hour;
      cell.ondragover = e => {
        e.preventDefault();
        cell.classList.add("drop");
      };
      cell.ondragleave = () => cell.classList.remove("drop");
      cell.ondrop = e => {
        e.preventDefault();
        cell.classList.remove("drop");
        const payload = e.dataTransfer.getData("text/plain");
        if (payload.startsWith("script:")) {
          addScriptSchedule(payload.slice(7), day, hour);
        } else {
          addSchedule(payload, day, hour);
        }
      };
      SCHEDULES
        .filter(s => +s.day === day && +s.hour === hour)
        .forEach(s => cell.appendChild(scheduleChip(s)));
      board.appendChild(cell);
    }
  }
}

function scheduleHeaderCell(text) {
  const el = document.createElement("div");
  el.className = "schedule-day";
  el.textContent = text;
  return el;
}

function scheduleChip(s) {
  const chip = document.createElement("div");
  let label;
  if (s.script_id) {
    const sc = SCRIPTS.find(x => x.id === s.script_id);
    label = `🎬 ${sc ? sc.name : s.script_id}`;
    chip.className = "schedule-chip script-chip";
  } else {
    const agent = AGENTS.find(a => a.id === s.agent_id);
    label = agent ? agent.name : s.agent_id;
    chip.className = "schedule-chip";
  }
  chip.innerHTML = `<span>${esc(label)}</span><button title="刪除">×</button>`;
  chip.querySelector("button").onclick = () => {
    SCHEDULES = SCHEDULES.filter(x => x.id !== s.id);
    renderScheduleBoard();
    $("#schedule-status").textContent = "排程已移除，記得按儲存排程。";
  };
  return chip;
}

function addScriptSchedule(scriptId, day, hour) {
  if (!scriptId) return;
  const exists = SCHEDULES.some(s =>
    s.script_id === scriptId && +s.day === day && +s.hour === hour && +(s.minute || 0) === 0);
  if (exists) return;
  SCHEDULES.push({
    id: `script-${scriptId}-${day}-${hour}-${Date.now()}`,
    script_id: scriptId,
    day,
    hour,
    minute: 0,
    enabled: true,
  });
  renderScheduleBoard();
  $("#schedule-status").textContent = "腳本排程已加入（到點時純重放、不用 AI），按儲存排程後生效。";
}

function addSchedule(agentId, day, hour) {
  if (!agentId) return;
  const exists = SCHEDULES.some(s =>
    s.agent_id === agentId && +s.day === day && +s.hour === hour && +(s.minute || 0) === 0);
  if (exists) return;
  SCHEDULES.push({
    id: `${agentId}-${day}-${hour}-${Date.now()}`,
    agent_id: agentId,
    day,
    hour,
    minute: 0,
    enabled: true,
  });
  renderScheduleBoard();
  $("#schedule-status").textContent = "排程已加入，按儲存排程後才會生效。";
}

$("#schedule-save").onclick = async () => {
  const r = await api("/api/schedules", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ schedules: SCHEDULES }),
  });
  SCHEDULES = r.schedules || [];
  renderScheduleBoard();
  $("#schedule-status").textContent = `已儲存 ${SCHEDULES.length} 筆排程。`;
};
$("#schedule-reload").onclick = loadSchedule;
function editAgent(a) {
  const f = $("#agent-form");
  f.id.value = a.id; f.game_id.value = a.game_id;
  f.name.value = a.name; f.prompt.value = a.prompt;
  f.notify_on_done.checked = a.notify_on_done !== false;
  window.scrollTo(0, 0);
}
async function runAgent(id) {
  const job = await api(`/api/agents/${id}/run`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ engine: "codex" }),
  });
  const msg = job.spawned
    ? `已開始執行任務 #${job.id}（使用 Codex）。\n進度與使用引擎會顯示在「任務佇列」分頁。`
    : `已建立任務 #${job.id}，但無法自動啟動執行器。可在終端手動跑：python tools/run_agent.py --job ${job.id}`;
  alert(msg);
  loadJobs();
}
async function delAgent(id) {
  if (!confirm("刪除這個 Agent？")) return;
  await api(`/api/agents/${id}`, { method: "DELETE" });
  loadAgents();
}
$("#agent-form").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target;
  await api("/api/agents", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: f.id.value || undefined, game_id: f.game_id.value,
                           name: f.name.value.trim(), prompt: f.prompt.value.trim(),
                           notify_on_done: f.notify_on_done.checked }),
  });
  f.reset(); f.id.value = ""; f.notify_on_done.checked = true;
  loadAgents();
};
$("#agent-reset").onclick = () => {
  $("#agent-form").reset();
  $("#agent-form").id.value = "";
  $("#agent-form").notify_on_done.checked = true;
};

// ---------- jobs ----------
async function loadJobs() {
  if (JOBS_LOADING) return;
  JOBS_LOADING = true;
  let jobs = [];
  try {
    const r = await api("/api/jobs");
    jobs = r.jobs || [];
  } finally {
    JOBS_LOADING = false;
  }
  handleJobNotifications(jobs);
  const list = $("#job-list");
  list.innerHTML = jobs.length ? "" : '<p class="hint">目前沒有任務。</p>';
  if (SELECTED_JOB_ID && !jobs.some(j => j.id === SELECTED_JOB_ID)) {
    SELECTED_JOB_ID = null;
    renderEmptyJobDetail();
  }
  jobs.forEach(j => {
    const div = document.createElement("div");
    div.className = `card job-card ${j.id === SELECTED_JOB_ID ? "selected" : ""}`;
    div.dataset.jobId = j.id;
    div.innerHTML = `
      <h3>${jobKindLabel(j.kind)} <span class="badge">${j.status}</span></h3>
      <div class="meta">#${j.id} · ${esc(j.created)}</div>
      <p class="hint">${esc(JSON.stringify(j.payload))}</p>
      ${j.result ? `<p class="hint">結果：${esc(j.result)}</p>` : ""}
      <div class="row">
        <button class="small" data-act="detail">詳情 / Log</button>
        <button class="small danger" data-act="del">刪除</button>
      </div>`;
    div.querySelector('[data-act=detail]').onclick = () => loadJobDetail(j.id);
    div.querySelector('[data-act=del]').onclick = () => delJob(j.id);
    div.onclick = (e) => {
      if (e.target.closest("button")) return;
      loadJobDetail(j.id);
    };
    list.appendChild(div);
  });
  if (!SELECTED_JOB_ID && jobs.length) {
    loadJobDetail(jobs[0].id);
  } else if (SELECTED_JOB_ID) {
    loadJobDetail(SELECTED_JOB_ID);
  }
}

function handleJobNotifications(jobs) {
  const finished = new Set(["done", "error"]);
  const next = new Map();
  jobs.forEach(j => {
    const prev = JOB_STATUS_CACHE.get(j.id);
    const status = j.status || "";
    const shouldNotify = j.kind === "run_agent" && j.payload?.notify_on_done !== false;
    if (JOBS_SEEDED && shouldNotify && finished.has(status) &&
        (prev === undefined || !finished.has(prev))) {
      showJobCompletionAlert(j);
    }
    next.set(j.id, status);
  });
  JOB_STATUS_CACHE = next;
  JOBS_SEEDED = true;
}

function showJobCompletionAlert(job) {
  const ok = job.status === "done";
  const agent = AGENTS.find(a => a.id === job.payload?.agent_id);
  const title = ok ? "任務完成" : "任務異常";
  const name = agent?.name || job.payload?.agent_id || job.id;
  const result = job.result ? `\n\n${String(job.result).slice(0, 600)}` : "";
  alert(`${title} #${job.id}\n${name}${result}`);
}

function startJobNotificationPolling() {
  if (JOB_NOTIFY_POLL_STARTED) return;
  JOB_NOTIFY_POLL_STARTED = true;
  loadJobs();
  setInterval(loadJobs, 10000);
}

async function delJob(id) {
  await api(`/api/jobs/${id}`, { method: "DELETE" });
  if (SELECTED_JOB_ID === id) {
    SELECTED_JOB_ID = null;
    renderEmptyJobDetail();
  }
  loadJobs();
}
$("#jobs-refresh").onclick = loadJobs;
$("#jobs-clear-finished").onclick = async () => {
  const r = await api("/api/jobs?scope=finished", { method: "DELETE" });
  alert(`已清除 ${r.removed} 筆已完成/失敗的任務。`);
  SELECTED_JOB_ID = null;
  renderEmptyJobDetail();
  loadJobs();
};
$("#jobs-clear-all").onclick = async () => {
  if (!confirm("清除所有任務？（執行中的任務會保留）")) return;
  const r = await api("/api/jobs?scope=all", { method: "DELETE" });
  alert(`已清除 ${r.removed} 筆任務。`);
  SELECTED_JOB_ID = null;
  renderEmptyJobDetail();
  loadJobs();
};

$("#job-detail-refresh").onclick = () => {
  if (SELECTED_JOB_ID) loadJobDetail(SELECTED_JOB_ID);
};

async function loadJobDetail(id) {
  SELECTED_JOB_ID = id;
  $$(".job-card").forEach(card => card.classList.toggle(
    "selected", card.dataset.jobId === id));
  const { job, logs } = await api(`/api/jobs/${encodeURIComponent(id)}`);
  $("#job-detail-empty").hidden = true;
  $("#job-detail-body").hidden = false;
  $("#job-detail-title").textContent = `任務詳情 #${job.id}`;
  $("#job-detail-meta").innerHTML = `
    <span class="badge">${esc(job.kind)}</span>
    <span class="badge">${esc(job.status)}</span>
    <span>${esc(job.created || "")}</span>`;
  $("#job-payload").textContent = formatJson(job.payload);
  $("#job-result").textContent = job.result ? String(job.result) : "(尚無結果)";
  renderPerformanceAnalysis(job.performance_analysis || job.performance?.analysis);
  $("#job-performance").textContent = job.performance
    ? formatJson(job.performance)
    : "(尚無效能資料)";
  renderLog("stdout", logs.stdout);
  renderLog("stderr", logs.stderr);
}

function renderEmptyJobDetail() {
  $("#job-detail-title").textContent = "任務詳情";
  $("#job-detail-empty").hidden = false;
  $("#job-detail-body").hidden = true;
}

function renderPerformanceAnalysis(analysis) {
  const box = $("#job-performance-analysis");
  if (!analysis) {
    box.innerHTML = '<p class="hint">尚無效能診斷。</p>';
    return;
  }
  const bottleneck = analysis.bottleneck;
  const stages = Array.isArray(analysis.stages) ? analysis.stages.slice(0, 6) : [];
  const observations = Array.isArray(analysis.observations) ? analysis.observations : [];
  const recommendations = Array.isArray(analysis.recommendations) ? analysis.recommendations : [];
  box.innerHTML = `
    <div class="perf-summary status-${esc(analysis.status || "ok")}">
      <strong>${performanceStatusText(analysis.status || "ok")}</strong>
      <span>${Number(analysis.total_seconds || 0).toFixed(1)} 秒</span>
      ${bottleneck ? `<span>最慢：${esc(bottleneck.label || bottleneck.stage)} ${Number(bottleneck.seconds || 0).toFixed(1)} 秒</span>` : ""}
    </div>
    <div class="perf-grid">
      <div>
        <p class="perf-title">觀察</p>
        ${observations.length ? observations.map(x => `<p>${esc(x)}</p>`).join("") : '<p class="hint">沒有明顯慢點。</p>'}
      </div>
      <div>
        <p class="perf-title">建議</p>
        ${recommendations.length ? recommendations.map(x => `<p>${esc(x)}</p>`).join("") : '<p class="hint">持續累積圖片記憶與快速規則。</p>'}
      </div>
    </div>
    <div class="perf-stages">
      ${stages.map(s => `
        <div class="perf-stage">
          <span>${esc(s.label || s.stage)}</span>
          <strong>${Number(s.seconds || 0).toFixed(2)}s</strong>
          ${s.detail ? `<small>${esc(s.detail)}</small>` : ""}
        </div>`).join("")}
    </div>`;
}

function performanceStatusText(status) {
  return { ok: "速度正常", watch: "建議觀察", slow: "明顯偏慢" }[status] || status;
}

function renderLog(kind, log) {
  const meta = $(`#job-${kind}-meta`);
  const box = $(`#job-${kind}`);
  if (!log || !log.exists) {
    meta.textContent = "尚無 log";
    box.textContent = "";
    return;
  }
  meta.textContent = `${log.path} · ${formatBytes(log.size)} · ${log.mtime}${log.truncated ? " · 已截斷" : ""}`;
  box.textContent = log.tail || "(空白)";
}

// ---------- diagnostics ----------
async function loadDiagnostics() {
  $("#diagnostics-summary").innerHTML = '<span class="badge">checking</span>';
  try {
    const data = await fetchDiagnostics();
    renderDiagnostics(data);
  } catch (e) {
    renderDiagnostics({
      generated_at: new Date().toLocaleString(),
      project: location.href,
      summary: { status: "fail", counts: { ok: 0, warn: 0, fail: 1 } },
      checks: [{
        level: "fail",
        title: "診斷 API 失敗",
        detail: e.message || String(e),
        action: "請重啟控制台，或確認目前 server.py 是否為最新版",
      }],
      logs: [],
    });
  }
}

function renderDiagnostics(data) {
  data = normalizeDiagnostics(data);
  const summary = data.summary;
  const counts = summary.counts || {};
  const systemText = data.system?.server_started_at
    ? `${data.system?.platform || ""} · server ${data.system.server_started_at}`
    : data.system?.platform || "";
  $("#diagnostics-summary").innerHTML = `
    <div class="summary-card status-${esc(summary.status)}">
      <strong>${statusText(summary.status)}</strong>
      <span>OK ${counts.ok || 0}</span>
      <span>WARN ${counts.warn || 0}</span>
      <span>FAIL ${counts.fail || 0}</span>
      <small>${esc(data.generated_at || "")}</small>
    </div>
    <div class="summary-card">
      <strong>Project</strong>
      <span>${esc(data.project || "")}</span>
      <small>${esc(systemText)}</small>
    </div>`;

  const list = $("#diagnostics-list");
  list.innerHTML = "";
  (data.checks || []).forEach(c => {
    const div = document.createElement("div");
    div.className = `card diagnostic-card status-${c.level}`;
    div.innerHTML = `
      <div class="diagnostic-title">
        <h3>${esc(c.title)}</h3>
        <span class="badge">${esc(c.level).toUpperCase()}</span>
      </div>
      <p>${esc(c.detail)}</p>
      ${c.action ? `<p class="hint">${esc(c.action)}</p>` : ""}`;
    list.appendChild(div);
  });

  const logs = $("#diagnostics-logs");
  logs.innerHTML = (data.logs || []).length ? "" : '<p class="hint">尚無 log。</p>';
  (data.logs || []).forEach(l => {
    const div = document.createElement("div");
    div.className = "log-file";
    div.innerHTML = `<strong>${esc(l.name)}</strong><span>${esc(l.mtime)} · ${formatBytes(l.size)}</span>`;
    logs.appendChild(div);
  });
}

$("#diagnostics-refresh").onclick = loadDiagnostics;

async function fetchDiagnostics() {
  const r = await fetch("/api/diagnostics", { cache: "no-store" });
  const text = await r.text();
  if (!r.ok) {
    throw new Error(`HTTP ${r.status}: ${text.slice(0, 220)}`);
  }
  try {
    return JSON.parse(text);
  } catch {
    throw new Error(`診斷端點沒有回傳 JSON：${text.slice(0, 220)}`);
  }
}

function normalizeDiagnostics(data) {
  const checks = Array.isArray(data?.checks) ? data.checks : [];
  const counts = { ok: 0, warn: 0, fail: 0, info: 0 };
  checks.forEach(c => {
    const level = ["ok", "warn", "fail", "info"].includes(c?.level) ? c.level : "fail";
    counts[level] += 1;
  });
  const fallbackStatus = counts.fail ? "fail" : counts.warn ? "warn" : checks.length ? "ok" : "fail";
  const summary = data?.summary && typeof data.summary === "object" ? data.summary : {};
  summary.status = ["ok", "warn", "fail", "info"].includes(summary.status)
    ? summary.status
    : fallbackStatus;
  summary.counts = summary.counts && typeof summary.counts === "object" ? summary.counts : counts;
  if (!checks.length) {
    checks.push({
      level: "fail",
      title: "診斷資料格式異常",
      detail: "前端沒有收到 checks 陣列，因此無法顯示各項檢查。",
      action: "請重啟控制台，或確認 server.py 是否為最新版",
    });
    summary.status = "fail";
    summary.counts = { ok: 0, warn: 0, fail: 1, info: 0 };
  }
  return {
    generated_at: data?.generated_at || "",
    project: data?.project || "",
    system: data?.system || {},
    summary,
    checks,
    logs: Array.isArray(data?.logs) ? data.logs : [],
  };
}

function statusText(status) {
  return { ok: "環境正常", warn: "需要確認", fail: "需要修復" }[status] || status;
}

// ---------- settings ----------
async function loadSettings() {
  const { settings } = await api("/api/settings");
  SETTINGS = settings || {};
  const seconds = Number(SETTINGS.ai_timeout_seconds || 3600);
  $("#settings-timeout-minutes").value = Math.round(seconds / 60);
  $("#settings-codex-model").value = SETTINGS.codex_model || DEFAULT_CODEX_MODEL;
  $("#settings-codex-reasoning-effort").value =
    SETTINGS.codex_reasoning_effort || DEFAULT_CODEX_REASONING_EFFORT;
  $("#settings-auto-tune-after-agent").checked =
    SETTINGS.auto_tune_after_agent !== false;
  renderSettings();
}

function renderSettings() {
  const seconds = Number(SETTINGS.ai_timeout_seconds || 3600);
  const model = SETTINGS.codex_model || DEFAULT_CODEX_MODEL;
  const reasoning = SETTINGS.codex_reasoning_effort || DEFAULT_CODEX_REASONING_EFFORT;
  $("#settings-summary").innerHTML = `
    <p><strong>AI 任務 timeout</strong></p>
    <p>${Math.round(seconds / 60)} 分鐘</p>
    <p class="hint">${seconds} 秒</p>
    <p><strong>Codex</strong></p>
    <p>${esc(model)} + ${esc(reasoning)}</p>
    <p><strong>Agent 效能調整</strong></p>
    <p>${SETTINGS.auto_tune_after_agent !== false ? "自動啟用" : "停用"}</p>
    <p class="hint">Agent 完成後會建立效能調整任務</p>`;
}

$("#settings-form").onsubmit = async (e) => {
  e.preventDefault();
  const minutes = Number($("#settings-timeout-minutes").value || 60);
  const seconds = Math.max(60, Math.min(86400, Math.round(minutes * 60)));
  const model = ($("#settings-codex-model").value || DEFAULT_CODEX_MODEL).trim();
  const reasoning = $("#settings-codex-reasoning-effort").value || DEFAULT_CODEX_REASONING_EFFORT;
  const autoTune = $("#settings-auto-tune-after-agent").checked;
  const r = await api("/api/settings", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      ai_timeout_seconds: seconds,
      codex_model: model,
      codex_reasoning_effort: reasoning,
      auto_tune_after_agent: autoTune,
    }),
  });
  SETTINGS = r.settings || {};
  $("#settings-status").textContent =
    `已儲存：${SETTINGS.codex_model || model} + ${SETTINGS.codex_reasoning_effort || reasoning}`;
  renderSettings();
};

$("#settings-reload").onclick = loadSettings;

function formatJson(value) {
  return JSON.stringify(value ?? null, null, 2);
}

function formatBytes(value) {
  const n = Number(value || 0);
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// init
applyPlatformFields();
loadGames();
startJobNotificationPolling();
