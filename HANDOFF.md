# AutoGameTest 交接文件

更新時間：2026-07-14  
目前主分支：`main`  
最新已推送 commit：`44c8b1f [Hibari] 修正自主探索單輪逾時處理`

## 新聊天接手先讀

1. 先讀 `AGENTS.md`，它是本專案給 Codex 的工作規則。
2. 再讀 `README.md`，它包含目前控制台、Agent、腳本、QA、診斷、設定的完整使用方式。
3. 不要清理或 revert 目前工作區的未提交檔，裡面包含使用者/任務跑出的本機資料與圖片記憶。
4. 使用者偏好：每完成一個項目就自動 commit 並 push 到 Git，commit 訊息用 `[Hibari] ` 開頭。

## 專案定位

AutoGameTest 是 AI 遊戲測試/代打控制台。Web UI 由 `server.py` 提供，主要機械操作用 Python/ADB；遊戲認知與文件生成由 Codex CLI 執行。

核心目標：

- 讓使用者新增遊戲、建立遊戲 skill、建立 agent。
- 對 Android 模擬器遊戲用 ADB 截圖與 tap 操作，不佔用實體滑鼠鍵盤。
- 支援錄影生成腳本、模板比對執行腳本。
- 支援 QA 頁：企劃書轉 TestCase、破壞性測試、執行 TestCase 並回寫 Excel。
- 支援自主探索：AI 自己看圖探索遊戲，最後產出玩家視角 Excel 回饋報告。

## 啟動方式

一般使用者：

```bat
start.bat
```

開發者：

```bash
python server.py
```

控制台位址：

```text
http://127.0.0.1:8777
```

環境診斷：

```bash
python tools/doctor.py
```

本機路徑設定：

- 範本：`config.example.json`
- 本機覆寫：`config/local.json`，已被 git 忽略
- 支援 LDPlayer、BlueStacks、Codex CLI、ADB 路徑覆寫

## 重要安全規則

- 登入、帳密、第三方授權：一律停止，請使用者本人操作。
- 付費、購買、月卡、儲值、抽卡/轉蛋：不可代操作。
- PVP/排位/會影響真人玩家的匹配：不可自動執行。
- 每一步操作後要截圖驗證，不盲目連點。
- 高風險畫面只能寫成風險記憶，不建立安全 fast rule。

## 主要資料流

- `data/games.json`：遊戲與 agent 設定的單一事實來源。
- `.codex/skills/<game>/SKILL.md`：遊戲專屬 skill。
- `.codex/agents/<game>-player.md`：遊戲專屬 agent persona。
- `data/jobs/*.json`：背景任務狀態。
- `data/logs/`：任務 stdout/stderr。
- `data/artifacts/<job>/`：任務截圖產物。
- `data/visual_memory/<game>/memory.json`：圖片記憶。
- `data/fast_rules/<game>.json`：本地快速規則。
- `data/scripts/`：錄影生成的腳本。
- `TestCase/`：QA TestCase xlsx。
- `data/reports/autonomous/`：自主探索玩家回饋 Excel，已加入 `.gitignore`。

## 目前 AI 引擎設定

目前專案已移除 Claude Code 依賴，預設全走 Codex CLI。

預設設定：

- model：`gpt-5.5`
- reasoning effort：`high`
- 背景 AI timeout：預設 3600 秒，可在設定頁改；最近測試任務使用過 14400 秒

重要檔案：

- `tools/ai_runner.py`：尋找並呼叫 Codex CLI。
- `tools/run_agent.py`：執行 Agent / 快速逐圖 / 自主探索。
- `tools/run_autotune.py`：Agent 後效能調整，使用 read-only 子 Codex，Python runner 負責落檔。

## 最近已完成並推送的重點 commit

```text
44c8b1f [Hibari] 修正自主探索單輪逾時處理
ff20e04 [Hibari] 新增自主探索玩家回饋報告
12ba6a0 [Hibari] 調整自主探索改以時間限制
9c5d6c5 [Hibari] 修正Autotune唯讀落檔流程
94dea8d [Hibari] 修正遊戲Skill格式
2371f46 [Hibari] 修正快速逐圖截圖前檢查
b7e47ee [Hibari] 調整Agent效能建議評估圖片記憶
b9e4b29 [Hibari] 新增Agent自主探索模式
```

## 自主探索目前狀態

自主探索模式已完成下列能力：

- Agent 可勾選「自主探索模式」，prompt 可留空。
- runner 會自動使用快速逐圖架構，每輪新對話、看最新截圖、回 JSON 決策。
- 自主探索不使用固定輪數上限，改用使用者設定的總 AI timeout。
- 單輪 Codex 判斷逾時只記錄該輪 `timeout`，繼續下一輪，不讓整筆任務直接 error。
- 每輪要求輸出：
  - `observation`
  - `player_feedback`
  - `learned`
  - `next_state`
- 完成後自動產出玩家視角 Excel 報告：
  - 位置：`data/reports/autonomous/`
  - 任務詳情頁會顯示「下載玩家回饋 Excel」
  - 後端下載 route：`/api/reports/download?path=...`

重要檔案：

- `tools/run_agent.py`
- `core/player_reports.py`
- `core/visual_memory.py`
- `web/app.js`
- `web/index.html`
- `server.py`

## 2026-07-14 任務 #87fd14ae 問題紀錄

使用者詢問：

```text
🕹 執行 Agent error
#87fd14ae · 2026-07-14 10:53:04
```

結論：

- 不是 ADB 或模擬器錯。
- 不是 Excel 報告產出錯，報告已成功產出：
  `data/reports/autonomous/game_87fd14ae_player_feedback_20260714_105805.xlsx`
- 真正原因是第 4 輪 Codex 視覺判斷超過單輪 `180s` timeout。
- 第 4 張截圖其實已進入「菲菲新手教學對話」，右下有橘色繼續箭頭，右上有 `SKIP`。
- 當時程式把單輪 timeout 視為整筆 job error，已由 commit `44c8b1f` 修正。

後續改善已做：

- 自主探索單輪 timeout 不再讓整筆任務 error。
- 快速逐圖 / 自主探索 prompt 會注入圖片記憶。
- `core/visual_memory.py` 會優先列出安全、可操作、最近更新、有 priority 的圖片記憶。
- `run_autotune.py` 已把該畫面加入圖片記憶/fast rules，但這些資料目前是本機未提交狀態，見「目前工作區狀態」。

## Autotune 目前狀態

Agent 完成後可自動建立 `autotune_agent` job。

目前設計：

- 子 Codex 使用 read-only sandbox。
- 子 Codex 不直接改檔，只輸出結構化區塊：
  - `AUTOGAMETEST_SKILL_LESSONS`
  - `AUTOGAMETEST_VISUAL_MEMORY`
  - `AUTOGAMETEST_AUTOTUNE_SUMMARY`
- Python runner 負責真正落檔：
  - skill lessons 追加到對應遊戲 skill
  - visual memory 合併到 `data/visual_memory/`
  - safe/low/routine 且帶 actions 的圖片記憶會晉升成 fast rules

重要檔案：

- `tools/run_autotune.py`
- `core/store.py`
- `core/visual_memory.py`
- `core/fast_agent.py`

## 腳本功能目前狀態

錄影/腳本流程：

1. `core/recorder.py` 錄影與 getevent 觸控，產出 mp4 / `taps.json`。
2. `tools/run_genscript.py` 讀錄影與 taps，產生腳本。
3. 新版腳本支援：
   - `tap_image`
   - `tap_scene`
   - `wait_scene`
   - `anchor`
   - `until`
   - 模板比對
4. `tools/run_script.py` 執行腳本，不使用 AI。

先前修過的腳本重點：

- 生成腳本時改抓點擊前關鍵幀，避免抓到按下後的變化狀態。
- 補裁圖預覽。
- 找不到模板時可嘗試下一張，但使用者後來要求等待策略先維持原本。
- `App package` 欄位已改成選遊戲。

## QA 頁目前狀態

QA 頁已完成：

- 企劃書轉 QA TestCase xlsx。
- 生成前必須先選遊戲。
- 生成時同步建立該遊戲的「系統理解 Skill」到 `data/system_skills/`，避免污染 `.codex/skills/`。
- 已產出的 TestCase 可下載、刪除、執行。
- 可從標準 TestCase 生成破壞性測試 xlsx。
- 執行 TestCase 時只執行 SAFE 案例。
- Agent 回覆 `RESULT: TC編號|PASS/FAIL/N/A|證據或原因` 時會回寫 Excel。

重要檔案：

- `core/testcases.py`
- `tools/run_testgen.py`
- `server.py`
- `web/app.js`
- `web/index.html`

## 環境診斷與跨電腦支援

目前已支援：

- `start.bat` 啟動。
- `tools/start.ps1` 尋找 Python 3.10+，包含 `py` launcher、PATH、registry、Python.org 常見安裝路徑。
- `tools/doctor.py` 做環境自檢。
- 診斷頁顯示：
  - Python
  - 寫入權限
  - port 8777
  - LDPlayer / ADB
  - BlueStacks / ADB
  - Codex CLI
  - `config/local.json`
  - 最近 log
- `config/local.json` 支援不同電腦路徑覆寫，也支援 BlueStacks 常見別名 key。

## 目前工作區狀態

最後確認時，主分支已推送到 `44c8b1f`，但工作區仍有使用者/任務產物未提交。不要自動 revert。

目前已知未提交項目：

```text
 M .codex/skills/game/SKILL.md
 M .codex/skills/gget/SKILL.md
 M data/games.json
 M tmp/screens/gget_001.png
 M tmp/screens/gget_002.png
 M tmp/screens/gget_003.png
?? data/fast_rules/
?? data/scripts/
?? data/visual_memory/
?? screen_checkpoint_1.png ... screen_checkpoint_7.png
?? tmp/... 大量 GGET/腳本/截圖測試產物
```

說明：

- `.codex/skills/game/SKILL.md` 有 autotune 追加的教訓，例如 `87fd14ae` 的菲菲教學對話。
- `data/visual_memory/` 與 `data/fast_rules/` 有 autotune 生成的圖片記憶與快速規則。
- 這些目前沒有一起提交，因為工作區已有大量本機產物與使用者資料，前面修程式時刻意只 stage 程式檔。
- 新聊天若要把這些學習成果納入 git，請先檢查內容，避免把大量截圖/暫存產物混進 commit。

## 建議下一步

1. 使用者若要繼續測自主探索，先拉最新 `main`，確認已包含 `44c8b1f`。
2. 再跑一次自主探索，觀察是否會在單輪 timeout 後繼續下一輪。
3. 檢查任務詳情是否可下載玩家回饋 Excel。
4. 若 `data/visual_memory/game/memory.json` 與 `data/fast_rules/game.json` 確認乾淨，可考慮單獨提交「便利商店M 圖片記憶」。
5. 若自主探索仍慢，下一步可做：
   - 圖片記憶本地 pre-match，在呼叫 Codex 前直接判斷已知畫面。
   - 降低自主探索單輪模型 effort 或改成更小 prompt。
   - 對常見教學對話建立 fast rule，直接 tap 右下安全箭頭。
   - 把玩家回饋報告加入「摘要分頁」與更細的評分欄位。

## 常用命令

```bash
# 啟動控制台
python server.py

# 環境診斷
python tools/doctor.py

# 執行指定 agent
python tools/run_agent.py --agent <agent_id>

# 以遊戲 + 任務執行
python tools/run_agent.py --game <game_id> --task "任務內容"

# 自主探索，總時間 3 小時
python tools/run_agent.py --game <game_id> --autonomous --timeout 10800

# 看 agent prompt
python tools/run_agent.py --agent <agent_id> --print-prompt

# 執行 job
python tools/run_agent.py --job <job_id>

# 圖片記憶
python tools/visual_memory.py list <game_id>
python tools/visual_memory.py context <game_id>
python tools/visual_memory.py promote <game_id>

# Git 狀態
git status --short
git log --oneline -8
```

## 給下一個 Codex 的提醒

- 回答使用者請使用繁體中文。
- 使用者要的是工程師視角與實作導向，通常不要只提方案，要直接改。
- 修改檔案前先說明要改什麼。
- 完成後跑合理驗證，然後 commit/push。
- `git status` 有很多本機產物，commit 時務必只 stage 本次相關檔案。
- 不要使用 `git reset --hard` 或還原使用者未提交內容。
- 若使用者問「為什麼某 job error」，優先讀：
  1. `data/jobs/<job_id>.json`
  2. `data/logs/<job_id>.out.log`
  3. `data/logs/<job_id>.err.log`
  4. `data/artifacts/<job_id>/`
  5. 相關 autotune job

