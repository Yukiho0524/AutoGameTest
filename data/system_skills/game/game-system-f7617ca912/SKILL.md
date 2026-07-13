---
name: game-system-f7617ca912
description: "Understand and test the 便利商店M 投資開發 system from planning document C17.投資開發-1.0.2.0.xlsx. Use when running AutoGameTest QA TestCase jobs, navigating this feature, or interpreting its intended behavior, states, risks, and acceptance criteria."
---

# 便利商店M / 投資開發

## System Intent

- 投資開發透過消耗金幣與時間換取固定獎勵、隨機神祕獎勵與可能的特殊獎勵。
- 系統入口在便利機，任務通過後顯示『投資開發』Icon；有新投資或可領獎時顯示紅點。
- 主介面依地區頁籤顯示投資項目；初始解鎖好奇市，喵喵島與汪汪城依指定歷程解鎖。
- 投資流程為選擇投資項目、編制符合品階與等級需求的店員、消耗金幣出發、等待倒數、完成後領獎。
- 投資狀態包含未執行、執行中、完成；清單排序為完成 > 未執行 > 執行中，再依稀有度高到低、ID小到大。
- 刷新未執行投資與加速執行中投資會消耗鑽石；鑽石不足時顯示指定不足彈窗，不跳轉商城。
- 店員最多只能同時參與一個投資，領取完獎勵後才可參與其他投資；其他系統使用不影響投資店員。

## Source

- Game: 便利商店M (`game`)
- Planning doc: C17.投資開發-1.0.2.0.xlsx
- Generated: 2026-07-13
- TestCase count: 127

## Functional Areas

- 便利機入口: 5 條 TestCase
- 投資開發介面: 5 條 TestCase
- 地區頁籤: 6 條 TestCase
- 資源列: 4 條 TestCase
- 投資清單: 8 條 TestCase
- 投資詳細資料: 8 條 TestCase
- 系統說明頁面: 4 條 TestCase
- 無投資顯示: 1 條 TestCase
- 編制介面: 12 條 TestCase
- 單店員選擇: 7 條 TestCase
- 多店員選擇: 8 條 TestCase
- AI互動功能: 4 條 TestCase
- 投資出發: 4 條 TestCase
- 執行中狀態: 4 條 TestCase
- 完成狀態: 3 條 TestCase
- 領獎介面: 5 條 TestCase
- 一鍵執行: 7 條 TestCase
- 投資給予規則: 6 條 TestCase
- 刷新: 10 條 TestCase
- 加速: 8 條 TestCase
- 音效: 3 條 TestCase
- Log: 5 條 TestCase

## Agent Guidance

- Read this skill before executing QA TestCase jobs for this system.
- Use it to understand what the system is for, where to navigate, which states matter, and which risks should stop automation.
- Treat the generated TestCase workbook as the source of truth for PASS/FAIL; this skill provides context, not permission to invent missing rules.
- Stop and report if the test reaches login, account binding, payment, purchase confirmation, gacha confirmation, third-party authorization, PVP, or any unclear high-risk screen.

## Representative TestCases

- 便利機入口｜顯示確認｜P0/A｜完成對應任務後，便利機正確顯示『投資開發』Icon與系統名稱文字
- 便利機入口｜操作確認｜P0/S｜點擊『投資開發』Icon，正確開啟投資開發介面
- 便利機入口｜顯示確認｜P0/A｜系統內存在未執行的新投資時，『投資開發』入口正確顯示紅點提示
- 便利機入口｜顯示確認｜P0/A｜系統內存在未領取獎勵的投資時，『投資開發』入口正確顯示紅點提示
- 便利機入口｜操作確認｜P1/A｜點擊進入有新投資的對應頁籤後，入口紅點正確消失
- 投資開發介面｜顯示確認｜P0/A｜投資開發介面正確顯示『返回』鈕、系統名稱『投資項目』、『?』說明鈕、地區頁籤、鑽石、金幣與投資清單
- 投資開發介面｜顯示確認｜P1/A｜投資開發介面顯示正確無缺破圖
- 投資開發介面｜操作確認｜P0/S｜點擊『返回』鈕，正確返回進入系統前的所在介面
- 投資開發介面｜操作確認｜P1/A｜投資開發介面按下手機返回鍵，正確比照『返回』鈕返回上一層
- 投資開發介面｜操作確認｜P0/A｜投資開發介面有已上陣但未派出的店員時點擊『返回』鈕，正確清空已上陣店員
- 地區頁籤｜顯示確認｜P0/A｜首次進入投資開發介面時，正確預設開啟第一個地區頁籤好奇市
- 地區頁籤｜顯示確認｜P0/A｜好奇市頁籤已點擊時，正確顯示已選取底圖並顯示地區名稱

## Open Questions

- 投資開發入口Icon檔名標示為<待補>，無法驗證實際圖檔名稱。
- 好感度文字與暴擊值功能標示需等待好感度功能完成或暫不測試，無法產出完整好感度與暴擊率功能驗證。
- AI功能完成後的實際C端串接、前導文字與回覆規則未定，AI對話只能依目前固定話池驗證。
- 地區解鎖的SystemUnlock實際ID與歷程條件未在主流程明確列完整，需確認各區域解鎖條件。
- 多個美術資源標示圖檔名稱待補或以最終設計為準，無法驗證最終圖檔檔名與尺寸。
- 投資說明頁面提到預計包含升級提升說明，但未提供完整升級提升文字內容。
- 一鍵演出標示為暫定且僅描述兩秒後淡出，需確認最終演出規格與是否保留。
- 測試工具Get Next Investment Item需重新進出系統才生效，但未描述工具入口與操作權限。
