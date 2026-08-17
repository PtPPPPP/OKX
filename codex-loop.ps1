param(
    [string]$ProjectPath = "D:\Program\OKX",
    [int]$MaxRounds = 100,
    [int]$SleepSeconds = 15,
    [int]$MaxConsecutiveFailures = 3
)

$ErrorActionPreference = "Continue"

$ProjectPath = (Resolve-Path $ProjectPath).Path
$LogDirectory = Join-Path $ProjectPath ".codex-loop"
$StopFile = Join-Path $ProjectPath "STOP_CODEX_LOOP"
$PromptFile = Join-Path $ProjectPath "CODEX_LOOP_PROMPT.md"

New-Item -ItemType Directory -Force -Path $LogDirectory | Out-Null

if (-not (Test-Path $PromptFile)) {
    throw "缺少任务文件：$PromptFile"
}

$consecutiveFailures = 0

for ($round = 1; $round -le $MaxRounds; $round++) {
    if (Test-Path $StopFile) {
        Write-Host "检测到 STOP_CODEX_LOOP，停止运行。"
        break
    }

    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $logFile = Join-Path $LogDirectory "round-$round-$timestamp.log"
    $prompt = Get-Content $PromptFile -Raw -Encoding UTF8

    $iterationPrompt = @"
你正在执行自动化循环的第 $round 轮。

请完整阅读并严格遵守：
- CODEX_LOOP_PROMPT.md

本轮要求：
1. 检查当前仓库、Git 状态和已跟踪的当前基线文档。
2. 选择一个尚未完成、范围明确、能够独立验证的最小任务。
3. 只完成这一个任务，不扩大范围。
4. 完成后运行对应测试、Ruff、Mypy 或其他必要验证。
5. 如果验证失败，优先修复本轮造成的问题。
6. 更新与本轮任务直接相关的已跟踪文档，准确记录：
   - 本轮完成内容
   - 修改文件
   - 验证结果
   - 遗留问题
   - 下一轮建议
7. 不要仅分析或给建议；应在安全约束允许时实际修改并验证代码。
8. 完成一个受限任务后主动结束本轮，不要无限探索。

以下是循环总任务说明：

$prompt
"@

    Write-Host ""
    Write-Host "========== Codex 第 $round/$MaxRounds 轮 =========="
    Write-Host "日志：$logFile"

    $iterationPrompt |
        codex exec `
            --cd $ProjectPath `
            --sandbox workspace-write `
            --ask-for-approval never `
            - 2>&1 |
        Tee-Object -FilePath $logFile

    $exitCode = $LASTEXITCODE

    if ($exitCode -eq 0) {
        $consecutiveFailures = 0
        Write-Host "第 $round 轮完成。"
    }
    else {
        $consecutiveFailures++
        Write-Warning "第 $round 轮失败，退出码：$exitCode"
        Write-Warning "连续失败次数：$consecutiveFailures/$MaxConsecutiveFailures"

        if ($consecutiveFailures -ge $MaxConsecutiveFailures) {
            Write-Error "连续失败次数达到上限，自动停止，防止故障死循环。"
            break
        }
    }

    if ($round -lt $MaxRounds) {
        Start-Sleep -Seconds $SleepSeconds
    }
}

Write-Host "Codex 循环已结束。"
