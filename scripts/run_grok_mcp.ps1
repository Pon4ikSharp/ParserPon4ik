$ErrorActionPreference = 'Stop'
$root = 'C:\ParserPon4ik_PostgreSQL\ParserPon4ik'
Set-Location $root
$env:PYTHONIOENCODING = 'utf-8'
$python = Join-Path $root '.venv_grok_mcp\Scripts\python.exe'
$outLog = Join-Path $root 'runtime\grok_mcp.out.log'
$errLog = Join-Path $root 'runtime\grok_mcp.err.log'
$wrapperLog = Join-Path $root 'runtime\grok_mcp.wrapper.log'

try {
    Add-Content $wrapperLog ("START " + (Get-Date -Format o) + " user=" + [Environment]::UserName + " cwd=" + (Get-Location))
    Add-Content $wrapperLog ("python_exists=" + (Test-Path $python) + " python=" + $python)
    $ErrorActionPreference = 'Continue'
    & $python -m app.grok_mcp_server 1>> $outLog 2>> $errLog
    $code = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    Add-Content $wrapperLog ("EXIT " + $code + " " + (Get-Date -Format o))
    exit $code
}
catch {
    Add-Content $wrapperLog ("ERROR " + ($_ | Out-String))
    exit 1
}
