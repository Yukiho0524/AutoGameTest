# SD Gundam G Generation Eternal

## 遊戲概述

Android 版 `SD Gundam G Generation Eternal`，package 為 `com.bandainamcoent.gget_WW`。目前以 LDPlayer 9 的 ADB 操作，目標序號通常是 `emulator-5554`。

## 啟動流程

1. 確認 LDPlayer 實例啟動，必要時使用 `C:\LDPlayer\LDPlayer9\ldconsole.exe launch --index 0`。
2. 等待 ADB 裝置上線與 `sys.boot_completed=1`。
3. 啟動遊戲：
   `C:\LDPlayer\LDPlayer9\adb.exe -s emulator-5554 shell monkey -p com.bandainamcoent.gget_WW -c android.intent.category.LAUNCHER 1`
4. 標題畫面會顯示 `TAP TO START`，可點擊畫面下方中央附近進入。

## UI 地圖

- 標題畫面：左上有「選單」「資料同步」，下方中央是 `TAP TO START`。
- 年齡限制彈窗：標題為「年齡限制」，內文提示需在指定網站進行年齡確認。按「開啟」會進入外部確認流程；按「關閉」後會顯示「無法進行遊戲」。

## 例行任務

- 尚未能進入主畫面，主畫面、編制、一鍵編隊、活動關卡與自動戰鬥入口待補。

## 風險守則

- 登入、帳密、第三方授權、年齡/身分確認、付費畫面一律停止並請使用者本人處理。
- 每一步 ADB 操作後都必須截圖驗證畫面。
- 只做低頻選單操作與單人關卡；不自動進行線上排位或消費。

## 經驗教訓

- 2026-07-08：首次啟動後停在年齡限制彈窗。點「關閉」會得到「無法進行遊戲」，無法進入主畫面。下次執行任務前，需要使用者本人完成指定網站的年齡確認；完成後再從標題畫面 `TAP TO START` 繼續探索。
