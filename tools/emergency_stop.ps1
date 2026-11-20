$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$statePath = Join-Path $projectRoot "runtime\processes.json"
$expectedScripts = @("main.py", "editor.py", "analyst.py")
$stopped = 0

if (Test-Path $statePath) {
    $state = Get-Content -Raw -Encoding UTF8 $statePath | ConvertFrom-Json
    foreach ($role in @("collector", "editor", "analyst")) {
        $entry = $state.$role
        if ($null -eq $entry -or $null -eq $entry.pid) { continue }
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($entry.pid)" -ErrorAction SilentlyContinue
        if ($null -eq $process) { continue }
        $matchesProject = $false
        if (-not $process.CommandLine) {
            Write-Warning "Не вдалося перевірити командний рядок PID $($entry.pid); пропущено."
            continue
        }
        foreach ($script in $expectedScripts) {
            if ($process.CommandLine.Contains($projectRoot) -and $process.CommandLine.Contains($script)) {
                $matchesProject = $true
                break
            }
        }
        if (-not $matchesProject) {
            Write-Warning "PID $($entry.pid) не належить цьому комплекту; пропущено."
            continue
        }
        & taskkill.exe /PID $entry.pid /T /F | Out-Null
        $stopped++
    }
}

# Older process-manager versions could lose an already orphaned Codex child.
# Its structured-output command still contains an absolute schema path inside
# this project, which lets us target it without stopping unrelated Codex work.
Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match '^codex.*(\.exe)?$' -and
    $_.CommandLine -and $_.CommandLine.Contains($projectRoot)
} | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    $stopped++
}

Write-Host "Аварійне завершення виконано. Зупинено дерев процесів: $stopped"
Write-Host "Тепер файли профілів SQLite/WAL мають розблокуватися."
