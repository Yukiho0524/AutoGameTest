"use strict";
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const api = async (path, opts) => {
  const r = await fetch(path, opts);
  const ct = r.headers.get("content-type") || "";
  return ct.includes("json") ? r.json() : r;
};

let GAMES = [];
let AGENTS = [];
let SCHEDULES = [];
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
  if (t.dataset.tab === "schedule") loadSchedule();
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
  f.instance.value = lc.instance ?? 0;
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
function applyPlatformFields() {
  const p = $("#game-form").platform.value;
  const control = p === "android" ? "emulator" : "desktop";
  $$(".platform-fields").forEach(el => el.hidden = el.dataset.control !== control);
  $("#appid-field").hidden = p !== "steam";
  $("#epic-field").hidden = p !== "epic";
  $("#aumid-field").hidden = p !== "xbox";
}

$("#pkg-btn").onclick = async () => {
  const inst = $("#game-form").instance.value || 0;
  const serial = `emulator-${5554 + inst * 2}`;
  const { packages } = await api(`/api/emulator/packages?serial=${serial}`);
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
        serial: `emulator-${5554 + (+f.instance.value) * 2}`, package: f.package.value.trim() }
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
  if (!available) { $("#screen-status").textContent = "找不到雷電模擬器/adb"; return; }
  instances.forEach(i => {
    const serial = `emulator-${5554 + i.index * 2}`;
    const o = document.createElement("option");
    o.value = serial;
    o.textContent = `[${i.index}] ${i.title} ${i.running ? "▶ 執行中" : "⏸ 未啟動"}`;
    sel.appendChild(o);
  });
}
$("#refresh-shot").onclick = refreshShot;
function refreshShot() {
  const serial = $("#serial-sel").value;
  if (!serial) return;
  const img = $("#screen");
  img.onload = () => { img.style.display = "block"; $("#screen-status").textContent = ""; };
  img.onerror = () => { $("#screen-status").textContent = "截圖失敗（模擬器是否已開機？）"; };
  img.src = `/api/emulator/screenshot?serial=${serial}&t=${Date.now()}`;
}
$("#screen").onclick = async (e) => {
  const img = e.target;
  const rect = img.getBoundingClientRect();
  const x = Math.round((e.clientX - rect.left) / rect.width * img.naturalWidth);
  const y = Math.round((e.clientY - rect.top) / rect.height * img.naturalHeight);
  await api("/api/emulator/tap", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ serial: $("#serial-sel").value, x, y }),
  });
  setTimeout(refreshShot, 400);
};
let autoTimer = null;
$("#auto-refresh").onchange = (e) => {
  clearInterval(autoTimer);
  if (e.target.checked) autoTimer = setInterval(refreshShot, 2000);
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
      <div class="meta"><span class="badge">${esc(g ? g.name : a.game_id)}</span></div>
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

// ---------- schedule ----------
async function loadSchedule() {
  if (!AGENTS.length) {
    const r = await api("/api/agents");
    AGENTS = r.agents;
  }
  const { schedules } = await api("/api/schedules");
  SCHEDULES = schedules || [];
  renderScheduleAgents();
  renderScheduleBoard();
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
        const agentId = e.dataTransfer.getData("text/plain");
        addSchedule(agentId, day, hour);
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
  const agent = AGENTS.find(a => a.id === s.agent_id);
  const chip = document.createElement("div");
  chip.className = "schedule-chip";
  chip.innerHTML = `<span>${esc(agent ? agent.name : s.agent_id)}</span><button title="刪除">×</button>`;
  chip.querySelector("button").onclick = () => {
    SCHEDULES = SCHEDULES.filter(x => x.id !== s.id);
    renderScheduleBoard();
    $("#schedule-status").textContent = "排程已移除，記得按儲存排程。";
  };
  return chip;
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
                           name: f.name.value.trim(), prompt: f.prompt.value.trim() }),
  });
  f.reset(); f.id.value = "";
  loadAgents();
};
$("#agent-reset").onclick = () => { $("#agent-form").reset(); $("#agent-form").id.value = ""; };

// ---------- jobs ----------
async function loadJobs() {
  const { jobs } = await api("/api/jobs");
  const list = $("#job-list");
  list.innerHTML = jobs.length ? "" : '<p class="hint">目前沒有任務。</p>';
  jobs.forEach(j => {
    const div = document.createElement("div");
    div.className = "card";
    div.innerHTML = `
      <h3>${j.kind === "learn" ? "📖 學習" : "🕹 執行 Agent"} <span class="badge">${j.status}</span></h3>
      <div class="meta">#${j.id} · ${esc(j.created)}</div>
      <p class="hint">${esc(JSON.stringify(j.payload))}</p>
      ${j.result ? `<p class="hint">結果：${esc(j.result)}</p>` : ""}
      <div class="row"><button class="small danger" data-act="del">刪除</button></div>`;
    div.querySelector('[data-act=del]').onclick = () => delJob(j.id);
    list.appendChild(div);
  });
}

async function delJob(id) {
  await api(`/api/jobs/${id}`, { method: "DELETE" });
  loadJobs();
}
$("#jobs-refresh").onclick = loadJobs;
$("#jobs-clear-finished").onclick = async () => {
  const r = await api("/api/jobs?scope=finished", { method: "DELETE" });
  alert(`已清除 ${r.removed} 筆已完成/失敗的任務。`);
  loadJobs();
};
$("#jobs-clear-all").onclick = async () => {
  if (!confirm("清除所有任務？（執行中的任務會保留）")) return;
  const r = await api("/api/jobs?scope=all", { method: "DELETE" });
  alert(`已清除 ${r.removed} 筆任務。`);
  loadJobs();
};

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// init
applyPlatformFields();
loadGames();
