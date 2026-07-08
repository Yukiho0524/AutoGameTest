<#
.SYNOPSIS
  跑一個 AI 任務，Claude Code 額度用完時自動改用 Codex CLI。

.DESCRIPTION
  這是 tools/ai_runner.py 的方便包裝。它在「外部」呼叫 Claude，偵測到額度/用量
  上限錯誤時自動改呼叫 Codex，回傳先成功的那個結果。

  重要：這只對「腳本化的一次性提示」有效。它無法讓你正在進行的互動式 Claude Code
  對話在額度用完時無縫轉到 Codex —— 互動 session 的上下文與 MCP 工具不會轉移。

.EXAMPLE
  .\tools\ai.ps1 "幫我把這段程式重構成 async"

.EXAMPLE
  .\tools\ai.ps1 -Engine codex -Timeout 300 "解釋這個錯誤"

.EXAMPLE
  "從 stdin 來的長提示" | .\tools\ai.ps1
#>
param(
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$Prompt,

    [ValidateSet("auto", "claude", "codex")]
    [string]$Engine = "auto",

    [int]$Timeout = 600,

    [switch]$NoFallback,

    [string]$Cwd
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$runner = Join-Path $scriptDir "ai_runner.py"

$argsList = @($runner)
if ($Engine -ne "auto") { $argsList += @("--engine", $Engine) }
$argsList += @("--timeout", "$Timeout")
if ($NoFallback) { $argsList += "--no-fallback" }
if ($Cwd) { $argsList += @("--cwd", $Cwd) }

if ($Prompt -and $Prompt.Count -gt 0) {
    $argsList += ($Prompt -join " ")
} else {
    $argsList += "-"   # read prompt from stdin
}

# 答案走 stdout；引擎/切換摘要走 stderr（不干擾管線）
python @argsList
exit $LASTEXITCODE
