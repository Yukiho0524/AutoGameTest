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
  "codex_path": ""
}
```

也可以用環境變數覆寫：`AUTOGAMETEST_LDPLAYER_DIR`、`AUTOGAMETEST_LDCONSOLE_PATH`、`AUTOGAMETEST_ADB_PATH`、`AUTOGAMETEST_BLUESTACKS_DIR`、`AUTOGAMETEST_BLUESTACKS_PLAYER_PATH`、`AUTOGAMETEST_BLUESTACKS_ADB_PATH`、`AUTOGAMETEST_BLUESTACKS_SERIAL`、`AUTOGAMETEST_BLUESTACKS_INSTANCE`、`AUTOGAMETEST_CODEX_PATH`。

開發時也可直接啟動：

```bash
python server.py
```

也可以單獨檢查環境：

```bash
python tools/doctor.py
```

然後開 http://127.0.0.1:8777 。四個分頁：

- **遊戲庫**：新增/編輯遊戲。填 exe 路徑按「偵測平台」會自動判斷 Steam/Epic/Xbox/PC 並讀出 Steam AppID；模擬器遊戲則選 Android、按「列出已安裝」挑 package。每款遊戲可貼攻略網址送出「學習」任務。
- **模擬器操控**：即時顯示模擬器畫面，**點畫面就等於送 tap 到模擬器**（透過 ADB，不佔用你的實體滑鼠鍵盤）。可開自動更新。
- **Agent**：建立綁定某遊戲的代打 agent（預設玩家人格 + 指令），可儲存、重複執行。
- **任務佇列**：學習與執行 agent 產生的任務清單。
- **排程表**：週一到週日、24 小時直條行事曆。把右側 Agent 拖到指定星期與整點，按「儲存排程」後，只要控制台保持執行，未來每週固定時間會自動建立並執行該 Agent 任務。

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
server.py              # 控制台後端（Python 標準庫，零依賴）
core/
  store.py             # games/agents JSON 儲存、skill 檔讀寫、任務佇列
  platforms.py         # 從 exe 路徑偵測平台 + 讀 Steam AppID
  launcher.py          # 依平台啟動遊戲（協定 / exe / ADB）
  adb.py               # 雷電模擬器 ldconsole + adb 封裝（截圖/tap/啟動）
web/
  index.html app.js style.css   # 前端
data/
  games.json           # 遊戲與 agent 設定（單一事實來源）
  jobs/                # 學習/執行任務狀態
.codex/
  skills/<遊戲名>/SKILL.md    # 遊戲知識庫
  agents/<遊戲名>-player.md   # 綁定該遊戲的代打 agent
```

## AI 認知任務如何執行（learn / run_agent）

機械操作由 Python 控制台處理；**遊戲認知（學習、代打）由 Codex 執行**。
控制台的「學習」「執行 Agent」按鈕會在 `data/jobs/` 寫入任務檔，並立即背景執行：
1. learn：由 `tools/run_learn.py` 讀取遊戲設定與 `sources`，必要時請 AI 自行搜尋公開網路資料，生成/更新 `.codex/skills/<遊戲>/SKILL.md`
2. run_agent：由 `tools/run_agent.py` 載入該遊戲 skill + agent → 依控制模式操作遊戲 → 完成後回報
3. 任務檔會標記 `running` / `done` / `error`，並填入 `result`、`engine_used`、`attempts`

新增遊戲時若勾選「儲存後自動建立/更新 Skill」，系統會自動建立學習任務。可填攻略/wiki/官方網站網址；若留空，AI 會嘗試自行查找公開資料。

## 週排程

排程儲存在 `data/schedules.json`。控制台啟動時會開一個背景排程器，每 20 秒檢查一次目前星期與時間；若命中排程，會強制建立一筆 `run_agent` job 並背景執行。為避免同一分鐘重複執行，排程項目會記錄 `last_run_key`。

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

### 跑 Agent

`tools/run_agent.py` 會把「角色 persona + 遊戲 skill 知識 + 操作指令表 + 任務」組成一份自足 prompt，交給 Codex 執行。

```bash
python tools/run_agent.py --agent masterduel-daily
python tools/run_agent.py --game gget --task "完成每日任務"   # 用遊戲+任務
python tools/run_agent.py --job <job_id>                     # 處理佇列任務並回寫狀態
python tools/run_agent.py --agent <id> --print-prompt        # 只看組出的 prompt
python tools/run_agent.py --agent <id> --no-fast             # 停用快速判斷層排查問題
```

控制台按 Agent 的「執行」＝立即在背景跑這支，結果與使用引擎顯示在「任務佇列」。

- **模擬器（ADB）agent 最適合**：操作全是 `adb ... input tap` 之類 shell 指令，模擬器 agent 用 `danger-full-access` sandbox 讓 Codex 能呼叫 adb。
- **桌面（computer-use）agent 的限制**：headless / Codex 環境通常沒有 computer-use 工具，prompt 已指示「若無 computer-use 能力就回報需在互動 session 執行」，不會盲操作。這類 agent 仍建議在有 computer-use 的互動 session 跑。

### 快速判斷層

模擬器 agent 預設會先跑一層本地快速判斷器：

1. 啟動遊戲並截圖
2. 計算畫面 signature（sha256 + average hash）
3. 比對 `data/fast_rules/<game_id>.json` 的安全規則
4. 命中時直接用 ADB 執行 tap / swipe / wait；未命中才交給 Codex

這能把已知彈窗、每日領獎、固定選單流程從「每次請 AI 重判」降成「本地規則秒處理」。規則必須命中截圖 hash 才會執行，登入、付費、轉蛋、PVP 等高風險畫面不應建立快速規則。

Codex 完成操作後若確認某個畫面與動作安全穩定，可在最終輸出附上 `AUTOGAMETEST_FAST_RULES` JSON，runner 會自動合併到該遊戲的 fast rules。也可手動取得截圖 signature：

```bash
python tools/fast_rules.py signature path\to\screenshot.png
python tools/fast_rules.py list gget
```

## 學習迴圈（降低誤差的核心）

1. **執行時**：每步操作後截圖驗證，畫面不符預期即停下重判
2. **事後反思**：新學到的 UI 位置、流程變化、錯誤修正追加到 SKILL.md「經驗教訓」段
3. **固化**：重複驗證穩定的流程從「AI 即興判斷」降級為「固定步驟 + AI 只驗證畫面」

## 重要邊界

- **登入是硬邊界**：帳密輸入、第三方登入授權必須由使用者本人在遊戲/模擬器視窗操作，AI 不代做（防盜帳號、防提示注入）。登入通常只需一次。
- 線上遊戲多數條款禁止自動化；只做低頻選單操作（登入、領獎、日常），避免大量自動對戰。
