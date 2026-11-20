$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$CollectorHome = Join-Path $Root "codex\collector\profile"

$codex = $null
$global = Get-Command codex -ErrorAction SilentlyContinue
if ($global) {
    $codex = $global.Source
}
if (-not $codex) {
    $cached = Get-ChildItem (Join-Path $Root "runtime\_codex_npx_cache\_npx\*\node_modules\@openai\codex-win32-x64\vendor\x86_64-pc-windows-msvc\bin\codex.exe") -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cached) { $codex = $cached.FullName }
}
if (-not $codex) {
    throw "Codex CLI executable not found for Collector usage snapshot."
}

$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
$listener.Start()
$port = ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
$listener.Stop()

$oldHome = $env:CODEX_HOME
$env:CODEX_HOME = $CollectorHome
$server = $null
$ws = $null

function Send-Json([System.Net.WebSockets.ClientWebSocket]$socket, [object]$obj) {
    $json = $obj | ConvertTo-Json -Depth 20 -Compress
    $bytes = [Text.Encoding]::UTF8.GetBytes($json)
    $segment = [ArraySegment[byte]]::new($bytes)
    $null = $socket.SendAsync(
        $segment,
        [System.Net.WebSockets.WebSocketMessageType]::Text,
        $true,
        [Threading.CancellationToken]::None
    ).GetAwaiter().GetResult()
}

function Receive-Json([System.Net.WebSockets.ClientWebSocket]$socket) {
    $buffer = New-Object byte[] 65536
    $ms = [IO.MemoryStream]::new()
    try {
        do {
            $seg = [ArraySegment[byte]]::new($buffer)
            $res = $socket.ReceiveAsync(
                $seg,
                [Threading.CancellationToken]::None
            ).GetAwaiter().GetResult()
            if ($res.MessageType -eq [System.Net.WebSockets.WebSocketMessageType]::Close) {
                return $null
            }
            $ms.Write($buffer, 0, $res.Count)
        } while (-not $res.EndOfMessage)
        $text = [Text.Encoding]::UTF8.GetString($ms.ToArray())
        return ($text | ConvertFrom-Json)
    }
    finally {
        $ms.Dispose()
    }
}

try {
    $server = Start-Process -FilePath $codex -ArgumentList @(
        "app-server",
        "--listen",
        "ws://127.0.0.1:$port"
    ) -PassThru -WindowStyle Hidden

    $ws = [System.Net.WebSockets.ClientWebSocket]::new()
    $connected = $false
    for ($i = 0; $i -lt 40 -and -not $connected; $i++) {
        try {
            $cts = [System.Threading.CancellationTokenSource]::new([TimeSpan]::FromMilliseconds(500))
            $null = $ws.ConnectAsync(
                [Uri]"ws://127.0.0.1:$port",
                $cts.Token
            ).GetAwaiter().GetResult()
            $connected = $true
        }
        catch {
            Start-Sleep -Milliseconds 150
            if ($ws) { $ws.Dispose() }
            $ws = [System.Net.WebSockets.ClientWebSocket]::new()
        }
    }
    if (-not $connected) {
        throw "Could not connect to local Collector app-server."
    }

    Send-Json $ws @{
        jsonrpc = "2.0"
        id = 1
        method = "initialize"
        params = @{
            clientInfo = @{name = "ponchik-processing-v2"; version = "1.0"}
            capabilities = $null
        }
    }

    $init = $null
    while ($null -eq $init) {
        $m = Receive-Json $ws
        if ($null -eq $m) { throw "Socket closed before initialize response." }
        if ($m.id -eq 1) { $init = $m }
    }
    if (-not $init.result) { throw "Collector app-server initialize failed." }

    Send-Json $ws @{jsonrpc = "2.0"; method = "initialized"}
    Send-Json $ws @{
        jsonrpc = "2.0"
        id = 2
        method = "account/rateLimits/read"
        params = @{
            excludeResetCreditDetails = $true
            supportsLunaReserve = $false
        }
    }
    Send-Json $ws @{
        jsonrpc = "2.0"
        id = 3
        method = "account/usage/read"
        params = @{}
    }

    $rate = $null
    $usage = $null
    while (($null -eq $rate) -or ($null -eq $usage)) {
        $m = Receive-Json $ws
        if ($null -eq $m) { break }
        if ($m.id -eq 2) { $rate = $m }
        elseif ($m.id -eq 3) { $usage = $m }
    }

    Write-Output "COLLECTOR USAGE SNAPSHOT"

    if ($rate.result) {
        Write-Output ("ordinaryUsageAllowed=" + $rate.result.ordinaryUsageAllowed)
        $buckets = $rate.result.rateLimitsByLimitId
        if ($buckets) {
            foreach ($prop in $buckets.PSObject.Properties) {
                $v = $prop.Value
                $p = $v.primary
                $s = $v.secondary
                Write-Output (
                    "limit=" + $prop.Name +
                    " primary_used=" + $(if ($p) {$p.usedPercent} else {"na"}) +
                    " primary_window_min=" + $(if ($p) {$p.windowDurationMins} else {"na"}) +
                    " primary_resets_at=" + $(if ($p) {$p.resetsAt} else {"na"}) +
                    " secondary_used=" + $(if ($s) {$s.usedPercent} else {"na"}) +
                    " secondary_window_min=" + $(if ($s) {$s.windowDurationMins} else {"na"}) +
                    " secondary_resets_at=" + $(if ($s) {$s.resetsAt} else {"na"})
                )
            }
        }
        elseif ($rate.result.rateLimits) {
            $v = $rate.result.rateLimits
            Write-Output (
                "limit=" + $v.limitId +
                " primary_used=" + $v.primary.usedPercent +
                " secondary_used=" + $(if ($v.secondary) {$v.secondary.usedPercent} else {"na"})
            )
        }
    }
    elseif ($rate.error) {
        Write-Output ("rate_error=" + ($rate.error | ConvertTo-Json -Compress))
    }

    if ($usage.result) {
        $summary = $usage.result.summary
        Write-Output ("lifetimeTokens=" + $summary.lifetimeTokens)
        Write-Output ("peakDailyTokens=" + $summary.peakDailyTokens)
        if ($usage.result.dailyUsageBuckets) {
            $last = $usage.result.dailyUsageBuckets | Select-Object -Last 1
            Write-Output ("latestDaily=" + $last.startDate + " tokens=" + $last.tokens)
        }
    }
    elseif ($usage.error) {
        Write-Output ("usage_error=" + ($usage.error | ConvertTo-Json -Compress))
    }
}
finally {
    if ($ws) {
        try {
            $null = $ws.CloseAsync(
                [System.Net.WebSockets.WebSocketCloseStatus]::NormalClosure,
                "done",
                [Threading.CancellationToken]::None
            ).GetAwaiter().GetResult()
        } catch {}
        $ws.Dispose()
    }
    if ($server -and -not $server.HasExited) {
        Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
    }
    $env:CODEX_HOME = $oldHome
}
