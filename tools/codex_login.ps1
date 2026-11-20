param([ValidateSet('collector','analyst','editor')][string]$Role = 'collector')
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path $PSScriptRoot -Parent
$ProfilePath = Join-Path $ProjectRoot "codex\$Role\profile"
New-Item -ItemType Directory -Force -Path $ProfilePath | Out-Null
$PreviousCodexHome = $env:CODEX_HOME
try {
    $env:CODEX_HOME = $ProfilePath
    codex login
    if ($LASTEXITCODE -ne 0) { throw "Codex login failed: $Role" }
} finally {
    if ($null -eq $PreviousCodexHome) { Remove-Item Env:CODEX_HOME -ErrorAction SilentlyContinue }
    else { $env:CODEX_HOME = $PreviousCodexHome }
}
