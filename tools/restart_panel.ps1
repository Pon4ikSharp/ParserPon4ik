$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Не знайдено $python"
}

Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match '^python(w)?\.exe$' -and $_.CommandLine -and
    $_.CommandLine.Contains($projectRoot) -and
    $_.CommandLine.Contains("control_panel.py")
} | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}

Start-Sleep -Milliseconds 700
Start-Process -FilePath $python -ArgumentList "control_panel.py" `
    -WorkingDirectory $projectRoot -WindowStyle Minimized
Write-Host "Вебпанель повністю перезапущено: http://127.0.0.1:8000"
