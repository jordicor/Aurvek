param([ValidateSet('start', 'stop', 'status')][string]$Action = 'start')

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path $PSScriptRoot -Parent
$workspaceRoot = Split-Path $repoRoot -Parent
$python = Join-Path $workspaceRoot 'venvs\aurvek-venv\Scripts\python.exe'
$redisDirectory = Join-Path $workspaceRoot 'services\memurai\Memurai'
$logDirectory = Join-Path $repoRoot 'logs'
$statePath = Join-Path $logDirectory 'local-processes.json'
$services = @()
if (Test-Path -LiteralPath $statePath) {
    $services = @(Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json)
}

function Get-OwnedProcess($Service) {
    $process = Get-Process -Id $Service.processId -ErrorAction SilentlyContinue
    if ($process -and $process.StartTime.ToUniversalTime().Ticks.ToString() -eq $Service.started) {
        return $process
    }
}

function Stop-OwnedProcess($Service) {
    if (-not (Get-OwnedProcess $Service)) { return }
    $snapshot = @(Get-CimInstance Win32_Process)
    $processIds = [System.Collections.Generic.List[int]]::new()
    $processIds.Add([int]$Service.processId)
    for ($index = 0; $index -lt $processIds.Count; $index++) {
        foreach ($child in $snapshot | Where-Object ParentProcessId -eq $processIds[$index]) {
            $processIds.Add([int]$child.ProcessId)
        }
    }
    for ($index = $processIds.Count - 1; $index -ge 0; $index--) {
        Stop-Process -Id $processIds[$index] -ErrorAction SilentlyContinue
    }
}

function Stop-LocalServices {
    foreach ($name in @('worker', 'app', 'redis')) {
        foreach ($service in $script:services | Where-Object name -eq $name) {
            if ($name -eq 'redis' -and (Get-OwnedProcess $service)) {
                & (Join-Path $redisDirectory 'memurai-cli.exe') -h 127.0.0.1 -p 6379 shutdown
                if ($LASTEXITCODE -ne 0) { throw 'Redis shutdown failed.' }
            } else {
                Stop-OwnedProcess $service
            }
        }
    }
    if (Test-Path -LiteralPath $statePath) { Remove-Item -LiteralPath $statePath }
}

if ($Action -eq 'status') {
    foreach ($service in $services) {
        [pscustomobject]@{ Name = $service.name; Running = [bool](Get-OwnedProcess $service) }
    }
    return
}
if ($Action -eq 'stop') {
    Stop-LocalServices
    Write-Output 'Local Aurvek services stopped.'
    return
}
if (@($services | Where-Object { Get-OwnedProcess $_ }).Count -gt 0) {
    throw 'Local services already exist. Use status or stop first.'
}
foreach ($path in @($python, (Join-Path $redisDirectory 'memurai.exe'),
        (Join-Path $repoRoot '.env'), (Join-Path $repoRoot 'db\Aurvek.db'),
        (Join-Path $repoRoot 'db\redis\memurai.conf'))) {
    if (-not (Test-Path -LiteralPath $path)) { throw "Required path is missing: $path" }
}
$listeners = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
if (@($listeners | Where-Object Port -in @(6379, 7789, 9191)).Count -gt 0) {
    throw 'A required local port (6379, 7789 or 9191) is already in use.'
}
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
$services = @()

function Start-LocalProcess($Name, $Executable, $Arguments) {
    $process = Start-Process -FilePath $Executable -ArgumentList $Arguments -WorkingDirectory $repoRoot `
        -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logDirectory "local-$Name.stdout.log") `
        -RedirectStandardError (Join-Path $logDirectory "local-$Name.stderr.log")
    $script:services += [pscustomobject]@{
        name = $Name; processId = $process.Id; started = $process.StartTime.ToUniversalTime().Ticks.ToString()
    }
    ConvertTo-Json -InputObject @($script:services) | Set-Content -LiteralPath $statePath -Encoding UTF8
}

$environmentNames = @('dramatiq_prom_host', 'dramatiq_prom_db', 'dramatiq_prom_lock')
$savedEnvironment = @{}
foreach ($name in $environmentNames) { $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name) }
try {
    Start-LocalProcess 'redis' (Join-Path $redisDirectory 'memurai.exe') ('"{0}"' -f (Join-Path $repoRoot 'db\redis\memurai.conf'))
    $deadline = (Get-Date).AddSeconds(15)
    do {
        Start-Sleep -Milliseconds 500
        $reply = & (Join-Path $redisDirectory 'memurai-cli.exe') -h 127.0.0.1 -p 6379 ping 2>$null
        if ($reply -eq 'PONG') { break }
        if ((Get-Date) -ge $deadline) { throw 'Redis did not become ready; inspect logs/local-redis.log.' }
    } while ($true)

    $env:dramatiq_prom_host = '127.0.0.1'
    $env:dramatiq_prom_db = Join-Path $logDirectory 'dramatiq-prometheus'
    $env:dramatiq_prom_lock = Join-Path $logDirectory 'dramatiq-prometheus.lock'
    Start-LocalProcess 'worker' $python '-u -m dramatiq tasks --processes 1 --threads 2 --use-spawn'
    Start-LocalProcess 'app' $python '-u -m uvicorn app:app --host 127.0.0.1 --port 7789 --workers 1 --no-proxy-headers'
    $deadline = (Get-Date).AddSeconds(60)
    do {
        Start-Sleep -Seconds 1
        foreach ($service in $services) {
            if (-not (Get-OwnedProcess $service)) { throw "The $($service.name) process exited. Inspect logs/local-$($service.name).stderr.log." }
        }
        $ready = $false
        try {
            $health = Invoke-RestMethod -Uri 'http://127.0.0.1:7789/health' -TimeoutSec 2
            $ready = $health.status -eq 'ok'
        } catch {
            $ready = $false
        }
        $workerReady = Select-String -LiteralPath (Join-Path $logDirectory 'local-worker.stderr.log') -SimpleMatch 'Worker process is ready for action.' -Quiet
        if ($ready -and $workerReady) { break }
        if ((Get-Date) -ge $deadline) { throw 'Startup timed out. Inspect logs/local-app.stderr.log and logs/local-worker.stderr.log.' }
    } while ($true)
    Write-Output 'Aurvek ready at http://127.0.0.1:7789 (Redis and Dramatiq running on loopback).'
} catch {
    Stop-LocalServices
    throw
} finally {
    foreach ($name in $environmentNames) { [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name]) }
}
