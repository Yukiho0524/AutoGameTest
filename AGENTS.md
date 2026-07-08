# AutoGameTest — Codex 專案指引

AI 遊戲代打系統。Web 控制台（`server.py`）處理機械操作；遊戲認知（學習、代打）由你（Codex）執行。完整說明見 [README.md](README.md)。

## 處理待辦任務

當使用者說「處理待辦任務」時：

1. 讀 `data/jobs/*.json`，找 `status: "pending"` 的任務，把它改為 `running`。
2. 依 `kind` 執行：
   - **learn**：抓 `payload.sources` 的網頁（WebFetch），理解遊戲玩法，讀取 `data/visual_memory/<game_id>/memory.json`（若有），生成或更新該遊戲 `skill_path`（預設 `.codex/skills/<game_id>/SKILL.md`，含遊戲概述、啟動流程、UI 地圖、例行任務、風險守則、圖片記憶、經驗教訓段）。
   - **run_agent**：載入 `.Codex/agents/<game_id>-player.md` 與該遊戲 skill，依遊戲的 `control` 模式操作：
     - `emulator`：用設定的模擬器 ADB（LDPlayer `adb.exe` 或 BlueStacks `HD-Adb.exe`）截圖 + `input tap`，見記憶 [[emulator-adb-pipeline]]。
     - `desktop`：用 computer-use，見記憶 [[masterduel-launch-facts]]。
3. 完成後把任務檔改 `status: "done"`（失敗則 `error`），填入 `result` 摘要。
4. 回報使用者：做了什麼、獲得什麼、有無異常。

控制台的「學習」按鈕與新增遊戲時的「儲存後自動建立/更新 Skill」會背景呼叫 `tools/run_learn.py`，並直接使用 Codex。手動處理 `pending` learn job 時，仍需遵守上述格式與風險守則。

控制台「排程表」會把週排程存到 `data/schedules.json`。到點時 server 會自動建立 `kind: "run_agent"`、`payload.source: "schedule"` 的 job 並背景執行；處理方式與手動執行 Agent 相同。

## 鐵則

- **每一步操作後截圖驗證畫面**，不符預期就停下重判，不要盲目連點。
- **登入是硬邊界**：帳密、第三方登入授權一律請使用者本人操作，絕不代輸。不讀取 app 的 auth 檔。
- **絕不代為消費**（購買卡包、月卡等），遇到付費畫面停止並回報。
- 線上遊戲只做低頻選單操作與 SOLO 單人模式，**不自動打排位對戰**。
- 每次代打學到的修正 → 追加寫進對應 SKILL.md 的「經驗教訓」段（附日期）。這是誤差遞減的核心。

## AI 引擎

`tools/ai_runner.py` 直接使用 Codex CLI 執行腳本化提示。也可用 CLI `python tools/ai_runner.py "..."`。

`tools/run_agent.py` 組自足 prompt（persona+skill+操作表+任務）後用 Codex 跑。CLI `python tools/run_agent.py --agent <id>` 或 `--job <id>`。控制台按「執行」會背景 spawn 這支。模擬器 agent（ADB shell）適合 Codex；桌面 agent 需 computer-use，headless 跑不了。

模擬器 agent 預設會先跑 `core/fast_agent.py` 快速判斷層：啟動遊戲、截圖、比對 `data/fast_rules/<game_id>.json` 的安全規則，以及 `data/visual_memory/<game_id>/memory.json` 中 `risk` 為 safe/low/routine 且帶安全 action 的圖片記憶；命中才直接 ADB 操作，未命中交給 Codex。Codex 若學到安全穩定的固定畫面操作，可在最終輸出 `AUTOGAMETEST_FAST_RULES` JSON，runner 會自動合併。登入、付費、轉蛋、PVP 不建立快速規則。

圖片相關記憶存於 `data/visual_memory/<game_id>/memory.json` 與 `images/`。可用 `python tools/visual_memory.py add <game_id> <png> --label ... --note ...` 手動收錄測試截圖；`run_learn.py` 與 `run_agent.py` 會把這些畫面狀態、signature、區域與風險標籤放進 prompt。Codex 若確認新畫面，可輸出 `AUTOGAMETEST_VISUAL_MEMORY` JSON 讓 runner 自動合併。高風險畫面只能標為風險記憶，不可建立安全自動操作。

## 控制台

`python server.py` → http://127.0.0.1:8777 。啟動用 `.Codex/launch.json` 的 `control-panel`（可用 /run 或 preview_start）。零依賴（Python 標準庫）。

## 資料流

`data/games.json` 是遊戲/agent 設定的單一事實來源，控制台讀寫它；`.Codex/skills/` 與 `.Codex/agents/` 是給你讀的知識庫。兩者透過 `store.py` 保持一致。
