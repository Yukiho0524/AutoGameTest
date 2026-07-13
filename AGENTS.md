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
     - 若 `payload.source == "testcase"`，代表 QA 頁從 TestCase 文件發起測試；先讀 `payload.testcase_system_skill_path` 對應的系統理解 Skill（若有），再依 `payload.prompt` 進入指定遊戲系統逐條驗證，回覆 `RESULT: TC編號|PASS/FAIL/N/A|證據或原因`，目前結果只寫任務詳情/log，不回寫 Excel。
   - **testgen**：讀 `payload.doc_path` 指向的企劃書與 `payload.game_id`，依 `TESTCASE_SPEC.md` 用 `tools/run_testgen.py` 生成 `TestCase/<企劃書檔名>_TestCase.xlsx`，並在 `.codex/skills/<遊戲id>-system-<hash>/SKILL.md` 建立/更新該企劃書系統的理解 Skill；只做文件測試設計，不操作遊戲、不登入、不消費。
   - **autotune_agent**：讀 `payload.source_job_id` 指向的 Agent job、`performance_analysis`、Skill/Agent 檔，做保守的效能知識調整；不可操作遊戲、不可登入/付費/抽卡、不可憑空建立 fast rule、不可 git commit/push。
3. 完成後把任務檔改 `status: "done"`（失敗則 `error`），填入 `result` 摘要。
4. 回報使用者：做了什麼、獲得什麼、有無異常。

控制台的「學習」按鈕與新增遊戲時的「儲存後自動建立/更新 Skill」會背景呼叫 `tools/run_learn.py`，並直接使用 Codex。手動處理 `pending` learn job 時，仍需遵守上述格式與風險守則。

控制台「排程表」會把週排程存到 `data/schedules.json`。項目可綁 `agent_id`（AI 代打）或 `script_id`（腳本重放）；到點時 server 自動建立 `kind: "run_agent"` 或 `kind: "run_script"` 的 job 並背景執行。

控制台「QA」頁籤的「企劃書 → QA TestCase」會先要求選遊戲，再建立 `kind: "testgen"` job，背景呼叫 `tools/run_testgen.py`，並把產出的 xlsx、企劃書備份、遊戲綁定與系統 Skill 綁定資訊放在 `TestCase/`；`TestCase/_input/` 只放抽取暫存與 log，不進 git。已產出的 TestCase 會從 xlsx 讀出所屬遊戲與系統 Skill，直接建立 `source: "testcase"` 的 `run_agent` job，讓 Agent 進遊戲到該系統測前 25 條未填結果案例。

## 腳本（genscript / run_script）

「腳本」是從錄影生成的確定性重放流程，存於 `data/scripts/<id>.yaml`。分工：**生成需要你（Codex）、執行不需要**。

- **genscript job**（`tools/run_genscript.py`）：骨架已從錄影的 taps.json（getevent 實測觸控）確定性建好，你的工作**只有註解**——檢視 `data/artifacts/<job>/tapNN.png` 關鍵幀，為每步命名、建議 wait_after、把登入/付費/轉蛋/PVP 步驟標記 `risk: true`，以 `AUTOGAMETEST_SCRIPT_ANNOTATION` JSON 回覆。**不得更動座標或動作型別**。
- **run_script job**（`tools/run_script.py`）：純 ADB 重放，不呼叫 AI，你不會收到這種 job。

錄影（`core/recorder.py`）同時抓 getevent 觸控存 taps.json；錄影中控制台的 tap 會走 sendevent（核心層）以便入錄。

## 鐵則

- **每一步操作後截圖驗證畫面**，不符預期就停下重判，不要盲目連點。
- **登入是硬邊界**：帳密、第三方登入授權一律請使用者本人操作，絕不代輸。不讀取 app 的 auth 檔。
- **絕不代為消費**（購買卡包、月卡等），遇到付費畫面停止並回報。
- 線上遊戲只做低頻選單操作與 SOLO 單人模式，**不自動打排位對戰**。
- 每次代打學到的可重用修正 → 在最終輸出 `AUTOGAMETEST_SKILL_LESSONS`，runner 會追加寫進對應 SKILL.md 的「經驗教訓」段（附日期）。不要把逐步流水帳、帳密、token、購買資訊或一次性雜訊寫入 Skill。這是誤差遞減的核心。

## AI 引擎

`tools/ai_runner.py` 直接使用 Codex CLI 執行腳本化提示。也可用 CLI `python tools/ai_runner.py "..."`。

`tools/run_agent.py` 組自足 prompt（persona+skill+操作表+任務）後用 Codex 跑。CLI `python tools/run_agent.py --agent <id>` 或 `--job <id>`。控制台按「執行」會背景 spawn 這支。模擬器 agent（ADB shell）適合 Codex；桌面 agent 需 computer-use，headless 跑不了。

模擬器 agent 預設會先跑 `core/fast_agent.py` 快速判斷層：啟動遊戲、截圖、比對 `data/fast_rules/<game_id>.json` 的安全規則，以及 `data/visual_memory/<game_id>/memory.json` 中 `risk` 為 safe/low/routine 且帶安全 action 的圖片記憶；命中才直接 ADB 操作，未命中交給 Codex。Codex 若學到安全穩定的固定畫面操作，可在最終輸出 `AUTOGAMETEST_FAST_RULES` JSON，runner 會自動合併。安全圖片記憶可帶 `wait`、`complete`、`handoff`、`max_repeats`、`fast_match` / `fast_max_distance`，合併後會自動晉升成 fast rules。登入、付費、轉蛋、PVP 不建立快速規則。

圖片相關記憶存於 `data/visual_memory/<game_id>/memory.json` 與 `images/`。可用 `python tools/visual_memory.py add <game_id> <png> --label ... --note ...` 手動收錄測試截圖，也可用 `python tools/visual_memory.py promote <game_id>` 將安全可操作記憶晉升到 fast rules；`run_learn.py` 與 `run_agent.py` 會把這些畫面狀態、signature、區域與風險標籤放進 prompt。Codex 若確認新畫面，可輸出 `AUTOGAMETEST_VISUAL_MEMORY` JSON 讓 runner 自動合併。高風險畫面只能標為風險記憶，不可建立安全自動操作。

## 控制台

`python server.py` → http://127.0.0.1:8777 。啟動用 `.Codex/launch.json` 的 `control-panel`（可用 /run 或 preview_start）。零依賴（Python 標準庫）。

## 資料流

`data/games.json` 是遊戲/agent 設定的單一事實來源，控制台讀寫它；`.Codex/skills/` 與 `.Codex/agents/` 是給你讀的知識庫。兩者透過 `store.py` 保持一致。
