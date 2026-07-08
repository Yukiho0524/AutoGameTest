<#
.SYNOPSIS
  用 Codex CLI 跑一個 AutoGameTest AI 任務。

.DESCRIPTION
  這是 tools/ai_runner.py 的方便包裝。答案走 stdout；執行摘要走 stderr。

.EXAMPLE
  .\tools\ai.ps1 "幫我整理這個遊戲的每日任務流程"

.EXAMPLE
  .\tools\ai.ps1 -Timeout 3600 "解釋這個錯誤"

.EXAMPLE
  "從 stdin 來的長提示" | .\tools\ai.ps1
#>
param(
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$Prompt,

    [int]$Timeout = 3600,

    [string]$Cwd
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$runner = Join-Path $scriptDir "ai_runner.py"

$argsList = @($runner, "--timeout", "$Timeout")
if ($Cwd) { $argsList += @("--cwd", $Cwd) }

if ($Prompt -and $Prompt.Count -gt 0) {
    $argsList += ($Prompt -join " ")
} else {
    $argsList += "-"
}

python @argsList
exit $LASTEXITCODE
