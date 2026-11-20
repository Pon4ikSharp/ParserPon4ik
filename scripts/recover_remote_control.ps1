param(
    [switch]$ForceRestart,
    [int]$WaitSeconds = 15
)

$ErrorActionPreference = 'Stop'
$Root = 'C:\ParserPon4ik_PostgreSQL\DesktopCommanderCustom'
$Node = 'C:\Program Files\nodejs\node.exe'
$Dist = Join-Path $Root 'dist\index.js'
$Log = 'C:\ParserPon4ik_PostgreSQL\ParserPon4ik\runtime\remote_control_recovery.log'

function Write-Log([string]$Message) {
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $line = "[$stamp] $Message"
    Write-Host $line
    Add-Content -Path $Log -Value $line -Encoding UTF8
}

function Get-DcAgents {
    $all = @(Get-CimInstance Win32_Process -Filter "Name='node.exe'")
    $children = @{}
    foreach ($p in $all) {
        if (-not $children.ContainsKey([int]$p.ParentProcessId)) { $children[[int]$p.ParentProcessId] = @() }
        $children[[int]$p.ParentProcessId] += $p
    }
    $agents = @()
    foreach ($p in $all) {
        $cmd = [string]$p.CommandLine
        if ($cmd -notmatch 'dist\\index\.js\s+remote') { continue }
        $kids = @($children[[int]$p.ProcessId])
        $owned = $false
        foreach ($kid in $kids) {
            $kidCmd = [string]$kid.CommandLine
            if ($kidCmd -like "*$Dist*") { $owned = $true; break }
        }
        if ($owned) { $agents += $p }
    }
    return @($agents)
}

function Stop-DcTree([int]$Pid) {
    $kids = @(Get-CimInstance Win32_Process | Where-Object { [int]$_.ParentProcessId -eq $Pid })
    foreach ($kid in $kids) {
        try { Stop-Process -Id ([int]$kid.ProcessId) -Force -ErrorAction Stop } catch {}
    }
    try { Stop-Process -Id $Pid -Force -ErrorAction Stop } catch {}
}

if (-not (Test-Path $Root)) { throw "DesktopCommander root not found: $Root" }
if (-not (Test-Path $Node)) { throw "Node executable not found: $Node" }
if (-not (Test-Path $Dist)) { throw "DesktopCommander entrypoint not found: $Dist" }
New-Item -ItemType Directory -Force -Path (Split-Path $Log) | Out-Null
$agents = @(Get-DcAgents)
Write-Log "Detected Desktop Commander remote agents: $($agents.Count)"

if ($ForceRestart) {
    foreach ($agent in $agents) {
        Write-Log "Stopping Desktop Commander tree PID $($agent.ProcessId)"
        Stop-DcTree -Pid ([int]$agent.ProcessId)
    }
    Start-Sleep -Seconds 2
    $agents = @()
}

if ($agents.Count -eq 0) {
    Write-Log 'Starting one Desktop Commander remote agent.'
    $proc = Start-Process -FilePath $Node -ArgumentList '.\dist\index.js','remote' -WorkingDirectory $Root -WindowStyle Hidden -PassThru
    Write-Log "Started parent PID $($proc.Id). Waiting up to $WaitSeconds seconds for child/server."

    $deadline = (Get-Date).AddSeconds($WaitSeconds)
    do {
        Start-Sleep -Seconds 1
        $agents = @(Get-DcAgents)
    } until ($agents.Count -ge 1 -or (Get-Date) -ge $deadline)
}

if ($agents.Count -lt 1) {
    Write-Log 'ERROR: Desktop Commander remote agent did not become healthy.'
    exit 2
}
foreach ($agent in $agents) {
    $tcp = @(Get-NetTCPConnection -OwningProcess ([int]$agent.ProcessId) -State Established -ErrorAction SilentlyContinue | Where-Object { $_.RemotePort -eq 443 })
    Write-Log "Healthy agent PID $($agent.ProcessId); HTTPS connections: $($tcp.Count)"
}

if (-not $ForceRestart -and $agents.Count -gt 1) {
    Write-Log 'WARNING: multiple healthy agents detected. No process was stopped in safe mode.'
}

Write-Log 'REMOTE_CONTROL_OK'
exit 0
