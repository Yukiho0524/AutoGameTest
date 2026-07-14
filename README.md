# AutoGameTest — AI 遊戲代打系統

讓 AI 學習並代打指定 PC / 模擬器遊戲的自動化系統。每款遊戲有獨立的 skill（知識庫）與專屬 agent（代打人格），透過每日重複執行與事後反思持續降低操作誤差。

## 控制台（Web UI）

零依賴，只需 Python 3。

一般使用者建議直接雙擊：

```bat
start.bat
```

`start.bat` 會先執行 `tools/doctor.py` 檢查 Python、資料夾寫入權限、8777 port、LDPlayer/ADB、Codex CLI 等環境狀態，再啟動控制台。
它會透過 `tools/start.ps1` 搜尋 Python 3.10+，包含 `py` launcher、`python/python3` 指令、Windows registry，以及 Python.org 常見安裝目錄，所以 Python 未加入 PATH 時也有機會自動找到。
若啟動失敗，視窗會提示錯誤並把啟動紀錄寫到 `data/logs/startup.log`。

如果每台電腦的軟體安裝位置不同，請複製 `config.example.json` 成 `config/local.json`，填入本機路徑。例如：

```json
{
  "ldplayer_dir": "D:\\LDPlayer\\LDPlayer9",
  "ldconsole_path": "",
  "adb_path": "",
  "bluestacks_dir": "C:\\Program Files\\BlueStacks_nxt",
  "bluestacks_player_path": "",
  "bluestacks_adb_path": "",
  "bluestacks_serial": "127.0.0.1:5555",
  "bluestacks_instance": "",
  "codex_path": "",
  "codex_model": "gpt-5.5",
  "codex_reasoning_effort": "high"
}
```

BlueStacks 路徑可填真正放 `HD-Player.exe` / `HD-Adb.exe` 的資料夾，例如 `D:\\Bluestack\\BlueStacks_nxt`；也可以只填外層資料夾，例如 `D:\\Bluestack`，系統會自動嘗試往下找 `BlueStacks_nxt` / `BlueStacks`。設定 key 也支援常見別名：`bluestack_dir`、`bluestack_player_path`、`bluestack_adb_path`。

`config/local.json` 是 JSON 格式，Windows 路徑建議使用 `D:\\Bluestack\\BlueStacks_nxt` 或 `D:/Bluestack/BlueStacks_nxt`。若誤填成 `D:\Bluestack\BlueStacks_nxt`，診斷頁會以寬容模式嘗試讀取並提示修正。

也可以用環境變數覆寫：`AUTOGAMETEST_LDPLAYER_DIR`、`AUTOGAMETEST_LDCONSOLE_PATH`、`AUTOGAMETEST_ADB_PATH`、`AUTOGAMETEST_BLUESTACKS_DIR`、`AUTOGAMETEST_BLUESTACKS_PLAYER_PATH`、`AUTOGAMETEST_BLUESTACKS_ADB_PATH`、`AUTOGAMETEST_BLUESTACKS_SERIAL`、`AUTOGAMETEST_BLUESTACKS_INSTANCE`、`AUTOGAMETEST_CODEX_PATH`、`AUTOGAMETEST_CODEX_MODEL`、`AUTOGAMETEST_CODEX_REASONING_EFFORT`。

開發時也可直接啟動：

```bash
python server.py
```

也可以單獨檢查環境：

```bash
python tools/doctor.py
```

然後開 http://127.0.0.1:8777 。九個分頁：

- **遊戲庫**：新增/編輯遊戲。填 exe 路徑按「偵測平台」會自動判斷 Steam/Epic/Xbox/PC 並讀出 Steam AppID；模擬器遊戲則選 Android、按「列出已安裝」挑 package。每款遊戲可貼攻略網址送出「學習」任務。
- **模擬器操控**：即時顯示模擬器畫面，**點畫面就等於送 tap 到模擬器**（透過 ADB，不佔用你的實體滑鼠鍵盤）。可開自動更新。下方有**錄影列**：按「⏺ 開始錄影」把模擬器畫面錄成 mp4（`adb screenrecord`，單段上限 180 秒、超過自動無縫接段），存檔位置可自訂並會記住（留空 = `data\recordings`），可勾選是否錄下觸控點，「開啟資料夾」直接跳到存檔位置。單段輸出 `rec_<時間>.mp4`；超過 180 秒輸出 `rec_<時間>/part01.mp4...` + `session.json`（與 GameTestAi 抽幀工具相容的格式）。錄影同時會用 `getevent` 擷取**實際觸控座標**存成 `taps.json`（腳本生成的資料來源）；錄影中從操控分頁點畫面會自動改走核心輸入層（sendevent），點擊照樣生效且會被記錄。
- **Agent**：建立綁定某遊戲的代打 agent（預設玩家人格 + 指令），可儲存、重複執行。
- **任務佇列**：學習、執行 agent、生成/執行腳本產生的任務清單。可點單筆查看詳情（payload、結果、stdout/stderr log），並手動清除單筆／已完成／全部。
- **腳本**：把錄影轉成可重放的確定性腳本。左側列出已生成腳本（可執行/查看編輯/刪除）；右側選一段**帶觸控紀錄**的錄影按「生成」——生成需要 AI（Codex 看關鍵幀為每步命名、補等待、標記風險步驟），**執行不需要 AI**（純 ADB 座標重放，每步截圖存 artifacts）。執行狀態同樣顯示在任務佇列。
- **排程表**：週一到週日、24 小時直條行事曆。右側有兩個來源：**Agent（AI 代打）**與**腳本（無 AI 重放）**，都可拖到指定星期與整點；按「儲存排程」後，只要控制台保持執行，未來每週固定時間會自動建立並執行對應任務。
- **QA**：懶人檢查台。彙整環境、任務、腳本與 TestCase 狀態；生成 TestCase 前需先選擇對應遊戲，再把企劃書（docx / pdf / xlsx / txt / md）丟給 Codex 轉成 QA 用 TestCase xlsx，並建立/更新該遊戲的「系統理解 Skill」。已產出的標準 TestCase 可再生成破壞性測試 xlsx，依 `SAFE / GUARDED / MANUAL` 分級；執行時只會讓 Agent 跑 SAFE 案例。TestCase 可下載、刪除，或直接按「執行」建立 `run_agent` 測試任務。
- **診斷**：環境自檢，逐項顯示 Python、資料夾寫入、8777 port、LDPlayer/ADB、BlueStacks、Codex CLI、`config/local.json` 狀態，並列出最近的執行 log，方便排查問題。
- **設定**：調整背景 AI 任務 timeout、Codex model 與推理強度等本機設定。

## 控制模式（兩種）

| 控制模式 | 適用平台 | 機制 | 佔用實體滑鼠鍵盤？ |
|---|---|---|---|
| `desktop` | Steam / Epic / Xbox / PC | computer-use 截圖+點擊 | 是（AI 操作時你不能同時用電腦）|
| `emulator` | Android 模擬器（雷電 / BlueStacks）| ADB 截圖+input tap | **否（可同時作業）** |

A 方案 = emulator 模式，是目前主推架構。已驗證管線見 `data/` 與記憶檔。

## 平台啟動方式

| 平台 | 啟動方式 | 需要資訊 |
|---|---|---|
| Steam | `steam://rungameid/<AppID>` | AppID（可從 appmanifest 自動偵測）|
| Epic | `com.epicgames.launcher://apps/<AppName>?action=launch` | Epic AppName |
| Xbox/UWP | `shell:AppsFolder\<PFN>!<AppId>` | AUMID |
| PC 單機 | 直接執行 exe | exe 路徑 |
| Android | `adb shell monkey -p <package>` | package 名 |

## 目錄結構

```
start.bat                # 一般使用者雙擊啟動（先 doctor 檢查再開控制台）
server.py                # 控制台後端（Python 標準庫，零依賴）
core/
  store.py               # games/agents JSON 儲存、skill 檔讀寫、任務佇列
  platforms.py           # 從 exe 路徑偵測平台 + 讀 Steam AppID
  launcher.py            # 依平台啟動遊戲（協定 / exe / ADB）
  adb.py                 # 模擬器（LDPlayer / BlueStacks）ldconsole + adb 封裝
  recorder.py            # 模擬器畫面錄影（screenrecord 分段自動接續，搬自 GameTestAi）
  config.py              # 讀 config/local.json 與環境變數（本機路徑覆寫）
  fast_agent.py          # 模擬器 agent 的本地快速判斷層（比對安全規則秒處理）
  image_match.py         # 腳本執行用的 stdlib PNG 模板比對
  testcases.py           # 企劃書 → QA TestCase xlsx / 系統理解 Skill 產生器
  visual_memory.py       # 圖片記憶（畫面 signature、狀態、風險標記）
tools/
  ai_runner.py           # 呼叫 Codex CLI 執行腳本化提示
  run_agent.py           # 組自足 prompt → Codex 代打；處理 run_agent job
  run_learn.py           # 學習：抓資料 → 生成/更新 SKILL.md
  run_genscript.py       # 腳本生成（AI）：taps.json 骨架 + Codex 看幀註解
  run_script.py          # 腳本執行（無 AI）：ADB 圖片模板 / 座標重放
  run_testgen.py         # QA：企劃書 → TestCase job runner
  doctor.py              # 環境自檢（Python/port/模擬器/ADB/Codex/config）
  fast_rules.py          # 快速規則與截圖 signature 工具
  visual_memory.py       # 圖片記憶 CLI（add / list / context）
  start.ps1  ai.ps1      # 啟動 / AI 執行的 PowerShell 包裝
web/
  index.html app.js style.css   # 前端（9 分頁控制台）
TESTCASE_SPEC.md         # 企劃書轉 QA TestCase 的撰寫規範
TestCase/                # 產出的 TestCase xlsx 與企劃書備份（_input/ 不進 git）
config/
  local.json             # 本機路徑設定（git 忽略）；範本見 config.example.json
data/
  games.json             # 遊戲與 agent 設定（單一事實來源）
  jobs/                  # 學習/執行任務狀態
  schedules.json         # 週排程
  fast_rules/<game>.json # 各遊戲的安全快速規則
  visual_memory/<game>/  # 圖片記憶（memory.json + images/）
  artifacts/<job>/       # 每次執行的截圖產物
  recordings/            # 模擬器錄影輸出（預設位置，可在錄影列自訂）
  scripts/<id>.yaml      # 從錄影生成的重放腳本
  scripts/assets/         # 腳本用按鈕模板與關鍵幀
  logs/                  # 執行 stdout/stderr 與診斷日誌
.codex/
  skills/<遊戲名>/SKILL.md    # 遊戲知識庫
data/system_skills/<遊戲id>/        # 企劃書生成的系統理解 Skill 與索引
  skills/mobile-game-controls/SKILL.md # 共用手機遊戲操作詞彙
  agents/<遊戲名>-player.md   # 綁定該遊戲的代打 agent
AGENTS.md                # Codex 專案指引（處理待辦任務、鐵則、資料流）
```

## AI 任務如何執行（learn / run_agent / testgen）

機械操作由 Python 控制台處理；**學習、代打與 QA 文件生成由 Codex 執行**。
控制台的「學習」「執行 Agent」「生成 TestCase」按鈕會在 `data/jobs/` 寫入任務檔，並立即背景執行：
1. learn：由 `tools/run_learn.py` 讀取遊戲設定與 `sources`，必要時請 AI 自行搜尋公開網路資料，生成/更新 `.codex/skills/<遊戲>/SKILL.md`
2. run_agent：由 `tools/run_agent.py` 載入該遊戲 skill + agent → 依控制模式操作遊戲 → 完成後回報
3. testgen：由 `tools/run_testgen.py` 抽取企劃書文字，依 `TESTCASE_SPEC.md` 交給 Codex 設計 CASE 與系統理解，輸出 `TestCase/<企劃書檔名>_TestCase.xlsx`，並在 `data/system_skills/<遊戲id>/<遊戲id>-system-<hash>/SKILL.md` 建立/更新該系統要做什麼、入口、核心流程、狀態與風險；同時更新 `data/system_skills/<遊戲id>/index.json`
4. destructive testgen：QA 頁可從已產出的標準 TestCase 生成 `<原名>_DestructiveTestCase.xlsx`，每條案例標 `SAFE / GUARDED / MANUAL`；SAFE 才允許交給 Agent 執行
5. testcase run：QA 頁執行 TestCase 時，後端會從 xlsx 讀出綁定遊戲、系統 Skill 與主表案例，依 UI 選擇的 10 / 25 / 50 / 全部批次取未填 PASS/FAIL 的案例組成 `run_agent` 任務；破壞性 TestCase 只挑 SAFE 案例；Agent 輸出的 `RESULT: TC編號|PASS/FAIL/N/A|證據或原因` 會回寫 Excel 的 PASS/FAIL 與備註欄，下一次執行會接著跑下一批 pending
6. 任務檔會標記 `running` / `done` / `error`，並填入 `result`、`engine_used`、`attempts`

新增遊戲時若勾選「儲存後自動建立/更新 Skill」，系統會自動建立學習任務。可填攻略/wiki/官方網站網址；若留空，AI 會嘗試自行查找公開資料。

### 圖片記憶

遊戲測試除了文字 skill，也可以記「畫面長什麼樣」。圖片記憶存在 `data/visual_memory/<game_id>/memory.json`，必要截圖會複製到 `data/visual_memory/<game_id>/images/`。內容包含：

- 截圖路徑與 signature（sha256 / ahash）
- 畫面狀態、標籤、風險標記
- UI 區域座標與安全動作提示

手動加入測試截圖：

```bash
python tools/visual_memory.py add gget tmp\screens\home.png --label "主畫面" --state home --tags "home,safe" --note "可進入任務、活動、信箱"
python tools/visual_memory.py add gget tmp\screens\home.png --label "主畫面" --actions-json "[{\"type\":\"tap\",\"x\":1000,\"y\":620,\"wait\":0.8}]" --fast-match --fast-max-distance 2
python tools/visual_memory.py promote gget
python tools/visual_memory.py list gget
python tools/visual_memory.py context gget
```

`tools/run_learn.py` 會把圖片記憶整理進 Skill 的「圖片記憶」章節；`tools/run_agent.py` 會把圖片記憶放進執行 prompt，讓 AI 更快辨識畫面。Agent 完成後若輸出 `AUTOGAMETEST_VISUAL_MEMORY` JSON，runner 會自動合併新記憶；其中 `risk: "safe"` / `low` / `routine` 且帶安全 actions 的項目會自動晉升成 fast rules，下一次同畫面先由本地層處理。

登入、付款、轉蛋、PVP 畫面可以記成高風險狀態，但不要記成可自動執行的安全動作。

## 腳本（錄影 → 生成 → 無 AI 重放）

「腳本」把一段你親手示範的操作變成可重複執行的流程，**生成用 AI、執行不用**：

1. **錄影**：在「模擬器操控」按 ⏺ 錄影，期間直接操作遊戲（在模擬器視窗用滑鼠、或在操控分頁點畫面都可以）。停止後除了 mp4，還會存下 `taps.json`——`getevent` 實測的每一次觸控（座標/時長/滑動），這是腳本正確性的來源。
2. **生成（AI，job kind `genscript`）**：`tools/run_genscript.py` 用 cv2 從影片抽出每步觸控前的關鍵幀，並裁出點擊附近的按鈕模板到 `data/scripts/assets/`，再把 **taps.json 原始資料 + 關鍵幀 + 模板 + 腳本規格全部交給 Codex 完整計算**：步驟取捨（過場誤點轉成 wait）、等待秒數、具體命名、風險標記（登入/付費/轉蛋/PVP 加 ⚠）都由 Codex 決定。Python 只做**安全驗證**：座標必須出自 taps.json 實測值（AI 發明座標會被整份拒絕）、動作限白名單、等待上限。Codex 失敗或輸出未過驗證時，若已成功裁出模板，會先退回含 `tap_image` 的圖片草稿；若沒有模板（例如缺 cv2/opencv 或影片讀取失敗），才退回純座標草稿，錄影不會白費。
3. **執行（無 AI，job kind `run_script`）**：`tools/run_script.py` 用 ADB 重放。新版腳本可用 `tap_image` / `tap_scene` 透過模板比對點擊按鈕，並用 `anchor` / `scene` / `until` 驗證操作前後畫面；舊腳本仍支援正規化座標換算後 `tap/swipe/long_press`。每步截圖存 `data/artifacts/<job>/`，狀態回寫任務佇列。也可 CLI 直接跑：`python tools/run_script.py --script <id>`。

腳本 YAML 範例：

```yaml
steps:
  - action: tap_image
    name: 點擊 進入遊戲
    image: data/scripts/assets/rec_xxx/templates/tap00_template.png
    record_pos: [0.0, 0.31]   # Airtest-like：相對畫面中心的位置
    resolution: [1280, 720]   # 錄製時解析度，用於推測搜尋區域
    target_pos: 5             # 1~9 九宮格點擊位置，5=中心
    rgb: false                # 預設灰階比對，較不怕亮度/顏色小變化
    threshold: 0.72
    timeout: 60
    until: data/scripts/assets/rec_xxx/templates/tap01_template.png
    wait_after: 2.0
```

若遊戲常卡在 Loading、下載資源或結算轉場，可在腳本頂層加長預設等待：

```yaml
defaults:
  visual_timeout: 60     # tap_image / tap_scene 找模板最多等待
  until_timeout: 120     # until / wait_scene 最多等待
  stable_timeout: 45     # 有 wait_after 的操作後，最多等待畫面穩定
  match_interval: 1.0
  match_threshold: 0.72  # 圖片比對門檻，執行時限制在 0.6~0.8
```

執行器參考 Airtest 的 `Template` 思路：模板可帶 `record_pos` / `resolution`，會先在錄製時點擊位置附近搜尋，找不到才退回全圖；`target_pos` 用來指定點模板九宮格中的哪個位置；`templates` 可放多張候選圖，任一命中即可。

適合固定不變的例行流程（每日簽到、領獎、掃蕩）；畫面會變動、需要判斷的任務仍交給 Agent（AI 代打）。腳本 YAML 可在腳本分頁直接查看/編輯（會做格式驗證）。

注意：腳本生成需要 `opencv-python`（抽關鍵幀與裁模板）與 `PyYAML`；缺 cv2 時仍可生成，但只會有座標草稿或低品質 AI 註解，不會有圖片預覽/模板。缺 PyYAML 則腳本功能停用。執行器只用標準庫。

## 週排程

排程儲存在 `data/schedules.json`。控制台啟動時會開一個背景排程器，每 20 秒檢查一次目前星期與時間；若命中排程，依項目類型建立 `run_agent`（AI 代打）或 `run_script`（無 AI 重放）job 並背景執行。為避免同一分鐘重複執行，排程項目會記錄 `last_run_key`。

注意：目前排程依賴 `server.py` 正在執行；若電腦關機或控制台未開，該時段不會補跑。

## AI 引擎（Codex）

`tools/ai_runner.py`：跑 AI 任務時直接使用 Codex CLI。

```bash
python tools/ai_runner.py "你的提示"
# PowerShell 方便包裝：
.\tools\ai.ps1 "你的提示"
```

答案印到 stdout，執行摘要印到 stderr（不干擾管線）。

引擎路徑自動偵測（不需寫死）：Codex 取 `%LOCALAPPDATA%\OpenAI\Codex\bin\*\codex.exe` 或 Windows Apps 裡的 Codex CLI。也可用 `config/local.json` 的 `codex_path` 或環境變數 `AUTOGAMETEST_CODEX_PATH` 覆寫。
背景 AI 任務預設鎖定 `gpt-5.5` + `high`，控制台「設定」分頁會把 model / reasoning effort 存進 `data/settings.json`，之後所有學習與 Agent 執行都會帶入 `--model` 與 `model_reasoning_effort`。

### 跑 Agent

`tools/run_agent.py` 會把「角色 persona + 遊戲 skill 知識 + 操作指令表 + 任務」組成一份自足 prompt，交給 Codex 執行。
Android 模擬器 agent 會額外載入 `.codex/skills/mobile-game-controls/SKILL.md`，把「點擊、雙擊、長按、拖曳、滑動、捲動」等手機遊戲口語操作轉成 ADB 動作語意。
背景 AI 任務預設 timeout 為 3600 秒（60 分鐘），可在控制台「設定」分頁調整，也可用 `--timeout` 覆寫；手動執行也可用 `--model gpt-5.5 --reasoning-effort high` 明確指定。
控制台背景執行會對 5 步以上的條列任務啟用自動 checkpoint，每 2 個編號步驟跑一段 Codex，降低單一長任務後段上下文越積越慢的情況。手動 CLI 可用 `--auto-segment` 開啟相同行為，或用 `--no-segment` 明確停用；每段預設最多等待 600 秒，也可用 `--segment-timeout` 調整。

Agent 可在編輯頁開啟「快速逐圖模式」。此模式僅支援 Android 模擬器：runner 每輪先用 ADB 截圖，開一個新的 Codex 最小對話，只提供遊戲 Skill、任務、上一輪一句摘要與最新截圖，要求 Codex 回 JSON 決策；runner 再解析 JSON 並執行 `tap` / `swipe` / `wait` / `launch_app`。它適合固定流程、每一步都能靠目前畫面判斷的任務，可避免長對話越跑越慢；複雜探索、需要長期策略記憶的任務建議維持一般模式。快速逐圖啟動前會先檢查/啟動模擬器、等待 ADB ready、啟動 App；若截圖失敗，任務結果會列出 emulator、serial、ADB 路徑、devices 與 screencap 失敗階段，方便在其他電腦對照 `config/local.json`。
Agent 也可開啟「自主探索模式」。此模式可讓 prompt 留空，runner 會自動使用快速逐圖架構，讓 AI 每輪看最新截圖、自己選擇低風險入口、記錄看到的 UI/流程/卡點，並在任務結果中回饋 `observation` / `learned`。遇到登入、PVP、未知或不確定畫面時不會立刻停止，而是先記錄、等待、返回或探索其他安全入口；仍不會代輸帳密、點第三方授權、購買付費商品，或開始會影響真人玩家的排位/匹配。
任務詳情會顯示「效能診斷」與 `performance`，包含 prompt 大小、fast layer 秒數、啟動模擬器/遊戲/首張截圖階段耗時、Codex 秒數、分段耗時與完成段落數，方便定位慢在哪一段。runner 會自動產生瓶頸觀察與優化建議；若遊戲已在前景，會跳過重新 launch app。ADB 截圖優先走 `exec-out screencap -p`，失敗才 fallback 到 `/sdcard` 檔案截圖，並會在 job progress 顯示 fast layer / Codex handoff 階段。
Agent job 完成後，若設定頁的「Agent 完成後自動把效能建議交給 Codex 調整」開啟，系統會自動建立 `autotune_agent` job。這個 job 會把該次 `performance_analysis`、原任務結果、Skill/Agent 摘要與已保存的 artifact 截圖候選交給 Codex，讓 Codex 做保守的知識調整；也會評估哪些截圖適合輸出 `AUTOGAMETEST_VISUAL_MEMORY`，runner 會自動合併到該遊戲圖片記憶，安全可操作記憶再晉升 fast rules。它不會操作遊戲、不會登入/付費/抽卡，也不會自動 git commit / push。
Agent prompt 會自動注入「完成判定與收尾」規則：任務最後一句若是「結束任務」「完成後通知我」等語意，達成後應直接回報 done，不再停在完成畫面等待額外指令。使用者仍可在 prompt 末尾寫明完成條件，會讓判斷更穩，但不是必填。
Agent 可在設定中開啟「任務完成時網頁彈窗通知」。控制台網頁會每 10 秒輪詢任務狀態；有開啟通知的任務從 pending/running 變成 done/error 時，瀏覽器會跳 alert。此通知依賴控制台頁面開著，不是 Windows 系統通知。

```bash
python tools/run_agent.py --agent masterduel-daily
python tools/run_agent.py --game gget --task "完成每日任務"   # 用遊戲+任務
python tools/run_agent.py --job <job_id>                     # 處理佇列任務並回寫狀態
python tools/run_agent.py --job <job_id> --timeout 7200      # 長任務可自行拉長
python tools/run_agent.py --agent <id> --print-prompt        # 只看組出的 prompt
python tools/run_agent.py --agent <id> --no-fast             # 停用快速判斷層排查問題
python tools/run_agent.py --agent <id> --auto-segment        # 5 步以上條列任務自動每 2 步分段
python tools/run_agent.py --agent <id> --segment-batch-size 3 # 改成每 3 個編號步驟一段
python tools/run_agent.py --agent <id> --no-segment          # 明確停用自動/手動分段
python tools/run_agent.py --agent <id> --segment-timeout 300 # 調整單段 timeout
python tools/run_agent.py --agent <id> --fast-visual         # 快速逐圖模式：每張截圖新對話
python tools/run_agent.py --game <game_id> --autonomous      # 自主探索：可不填任務，AI 自行探索
python tools/run_agent.py --agent <id> --fast-visual --visual-max-turns 20 --visual-turn-timeout 120
```

控制台按 Agent 的「執行」＝立即在背景跑這支，結果與使用引擎顯示在「任務佇列」。

- **模擬器（ADB）agent 最適合**：操作全是 `adb ... input tap` 之類 shell 指令，模擬器 agent 用 `danger-full-access` sandbox 讓 Codex 能呼叫 adb。
- **桌面（computer-use）agent 的限制**：headless / Codex 環境通常沒有 computer-use 工具，prompt 已指示「若無 computer-use 能力就回報需在互動 session 執行」，不會盲操作。這類 agent 仍建議在有 computer-use 的互動 session 跑。

### 快速判斷層

模擬器 agent 預設會先跑一層本地快速判斷器：

1. 啟動遊戲並截圖
2. 計算畫面 signature（sha256 + average hash）
3. 比對 `data/fast_rules/<game_id>.json` 的安全規則，以及 `data/visual_memory/<game_id>/memory.json` 裡標記為安全的圖片動作提示
4. 命中時直接用 ADB 執行 tap / swipe / wait；未命中才交給 Codex

這能把已知彈窗、每日領獎、固定選單流程從「每次請 AI 重判」降成「本地規則秒處理」。規則必須命中截圖 hash 才會執行，登入、付費、轉蛋、PVP 等高風險畫面不應建立快速規則。圖片記憶若要進入快速層，必須是 `risk: "safe"` / `low` / `routine`，且 action 或備註不能含登入、付費、抽卡、PVP 等風險關鍵字。

Codex 完成操作後若確認某個畫面與動作安全穩定，可在最終輸出附上 `AUTOGAMETEST_FAST_RULES` JSON，runner 會自動合併到該遊戲的 fast rules。若只是辨識畫面與安全可點區域，也可輸出 `AUTOGAMETEST_VISUAL_MEMORY`；runner 會保留 `wait`、`complete`、`handoff`、`max_repeats`、`fast_match` 等欄位，並把安全動作自動晉升成 fast rules。也可手動取得截圖 signature：

```bash
python tools/fast_rules.py signature path\to\screenshot.png
python tools/fast_rules.py list gget
python tools/fast_rules.py list gget --include-visual
```

## 學習迴圈（降低誤差的核心）

1. **執行時**：每步操作後截圖驗證，畫面不符預期即停下重判
2. **事後反思**：新學到的 UI 位置、流程變化、錯誤修正可透過 `AUTOGAMETEST_SKILL_LESSONS` 自動追加到 SKILL.md「經驗教訓」段
3. **固化**：重複驗證穩定的流程從「AI 即興判斷」降級為「固定步驟 + AI 只驗證畫面」

Agent 執行不會把每一步流水帳都寫進 Skill；完整紀錄仍保存在 `data/jobs/` 與 `data/logs/`。只有 Codex 在最終回覆中輸出的精煉區塊會寫入：

```text
AUTOGAMETEST_SKILL_LESSONS:
["主畫面若出現公告彈窗，先點右上角關閉，再進每日任務。"]
```

## 重要邊界

- **登入是硬邊界**：帳密輸入、第三方登入授權必須由使用者本人在遊戲/模擬器視窗操作，AI 不代做（防盜帳號、防提示注入）。登入通常只需一次。
- 線上遊戲多數條款禁止自動化；只做低頻選單操作（登入、領獎、日常），避免大量自動對戰。
