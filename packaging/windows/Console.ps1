[CmdletBinding()]
param(
    [ValidateSet('Start', 'Install', 'Stop', 'Files', 'RemoveEnvironment', 'Diagnose')]
    [string]$Action = 'Start',
    [string]$Distro,
    [string]$UbuntuImage
)

$ErrorActionPreference = 'Stop'
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$StateFile = Join-Path $PSScriptRoot 'wsl-settings.json'
$LinuxRoot = '/home/gromacs-console/.local/share/gromacs-console'
$Wsl = Join-Path $env:WINDIR 'System32\wsl.exe'
if (Test-Path (Join-Path $env:WINDIR 'Sysnative\wsl.exe')) {
    $Wsl = Join-Path $env:WINDIR 'Sysnative\wsl.exe'
}
$logDirectory = Join-Path $env:LOCALAPPDATA 'GromacsConsole\Logs'
New-Item -ItemType Directory -Force $logDirectory | Out-Null
$WslLog = Join-Path $logDirectory 'wsl-setup.log'
if ((Test-Path $WslLog) -and (Get-Item $WslLog).Length -gt 1048576) {
    Move-Item -Force $WslLog ($WslLog + '.previous')
}
Add-Content -Encoding UTF8 $WslLog ((Get-Date -Format s) + " Launcher action: $Action; Windows build: " + [Environment]::OSVersion.Version.Build)
Add-Content -Encoding UTF8 $WslLog ('PowerShell host: ' + $PSVersionTable.PSEdition + ' ' + $PSVersionTable.PSVersion + '; process architecture: ' + ([IntPtr]::Size * 8) + '-bit')
. (Join-Path $PSScriptRoot 'WslSetup.ps1')

function Invoke-Service([string]$Command) {
    $python = if ($Command -eq 'start') { "$LinuxRoot/.venv/bin/python" } else { 'python3' }
    $result = Invoke-WslCommand @('--distribution', $Distro, '--user', 'gromacs-console', '--exec',
        $python, "$LinuxRoot/desktop_service.py", $Command) -TimeoutSeconds 120
    if ($result.ExitCode -ne 0) {
        $result.Lines | ForEach-Object { Write-Host $_ }
        throw "Could not $Command the service (exit code $($result.ExitCode)). See $WslLog."
    }
    return (($result.StdoutLines -join "`n") | ConvertFrom-Json)
}

function Invoke-PayloadServiceStop([string]$EncodedPath) {
    # Use known payload code and system Python so a damaged application or venv
    # can still be repaired without bypassing protection for running jobs.
    $guard = @'
set -euo pipefail
payload_windows="$(printf '%s' '__PAYLOAD_B64__' | base64 -d)"
payload="$(wslpath -u "$payload_windows")"
python3 "$payload/desktop_service.py" stop --app-root '/home/gromacs-console/.local/share/gromacs-console' >/dev/null
'@
    Invoke-LinuxScript $Distro 'gromacs-console' ($guard.Replace('__PAYLOAD_B64__', $EncodedPath)) -TimeoutSeconds 120
}

function Wait-LocalService([string]$Url, [int]$TimeoutSeconds = 60) {
    $uri = [Uri]$Url
    if ($uri.Scheme -ne 'http' -or -not $uri.IsLoopback) { throw 'The service readiness URL must use local loopback HTTP.' }
    $timer = [Diagnostics.Stopwatch]::StartNew()
    while ($timer.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
        $request = [Net.HttpWebRequest]::Create($uri)
        $request.Proxy = $null
        $request.AllowAutoRedirect = $false
        $request.KeepAlive = $false
        $request.Timeout = [Math]::Max(1, [Math]::Min(2000, [int](($TimeoutSeconds - $timer.Elapsed.TotalSeconds) * 1000)))
        $request.ReadWriteTimeout = $request.Timeout
        $response = $null
        try {
            $response = $request.GetResponse()
            if ([int]$response.StatusCode -eq 200) { return $true }
        } catch [Net.WebException] { if ($_.Exception.Response) { $_.Exception.Response.Close() } }
        finally { if ($response) { $response.Close() }; $request.Abort() }
        $remaining = [int](($TimeoutSeconds - $timer.Elapsed.TotalSeconds) * 1000)
        if ($remaining -gt 0) { Start-Sleep -Milliseconds ([Math]::Min(250, $remaining)) }
    }
    return $false
}

function Install-Environment([string]$MutexName) {
    if (-not $MutexName) {
        $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        $MutexName = 'Local\GromacsConsole.Setup.' + $sid
    }
    $mutex = New-Object Threading.Mutex($false, $MutexName)
    $ownsLock = $false
    try {
        try { $ownsLock = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $ownsLock = $true }
        if (-not $ownsLock) {
            Write-Host 'Another GROMACS setup window is active. Waiting up to 60 seconds for it to finish...'
            Add-Content -Encoding UTF8 $WslLog 'Waiting for the existing Windows setup process.'
            for ($attempt = 0; $attempt -lt 60 -and -not $ownsLock; $attempt++) {
                try { $ownsLock = $mutex.WaitOne(1000) } catch [Threading.AbandonedMutexException] { $ownsLock = $true }
                if (-not $ownsLock -and ($attempt + 1) % 15 -eq 0) {
                    Write-Host 'The other setup window is still active; waiting for it to finish...'
                }
            }
            if (-not $ownsLock) {
                throw 'Another GROMACS setup is still active. Follow its progress in the original window, then launch again after it finishes.'
            }
        }
        Install-EnvironmentCore
    } finally {
        if ($ownsLock) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
}

function Install-EnvironmentCore {
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $downloadTimeoutExport = Get-LinuxDownloadTimeoutExport
    $manifest = Get-Content -Raw (Join-Path $PSScriptRoot 'release.json') | ConvertFrom-Json
    $payload = Join-Path $PSScriptRoot 'application.tar.gz'
    if ((Get-FileHash -Algorithm SHA256 $payload).Hash.ToLowerInvariant() -ne $manifest.payload_sha256) {
        throw 'Application archive checksum mismatch. Download a fresh installer.'
    }
    $snapshot = Get-WslDistributionSnapshot $Distro -Refresh
    if (-not $snapshot -or -not $snapshot.Supported) { throw 'The selected Linux environment is no longer compatible.' }
    $current = $snapshot.InstalledRelease
    $appOnly = ($Action -eq 'Start' -and $current -and $manifest.environment_sha256 -and
        $current.environment_sha256 -ceq $manifest.environment_sha256)
    Assert-EnvironmentSpace $Distro $snapshot -AppOnly:$appOnly
    $encodedPath = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($PSScriptRoot))
    $backend = if ($snapshot.GpuCandidate) { 'CUDA' } else { 'CPU' }
    $script = "export PAYLOAD_WINDOWS_B64='$encodedPath'`nexport SETUP_BACKEND='$backend'`n" + $downloadTimeoutExport + (Get-Content -Raw (Join-Path $PSScriptRoot 'linux-install.sh'))
    Write-Host "Live diagnostic log: $WslLog"
    if ($appOnly) {
        Write-Host 'Updating application files; existing compute dependencies are checked locally and reused.'
        $result = Invoke-LinuxScript $Distro 'gromacs-console' ("export APP_ONLY=1`n" + $script) -AllowExitCodes @(76)
        if ($result -and $result.ExitCode -eq 76) {
            Write-Host 'The existing compute environment needs repair; continuing with full environment setup.'
            $appOnly = $false
            Assert-EnvironmentSpace $Distro $snapshot
        }
    }
    if (-not $appOnly) {
        # Shared apt/Python/GROMACS changes must reject running jobs first.
        # Program-only updates keep serving until staged files are ready; Linux
        # handles the stop and repeats the task check at commit under its lock.
        if ($snapshot.HasApplicationRoot) { Invoke-PayloadServiceStop $encodedPath }
        Write-Host 'Preparing scientific tools and detecting GPU support (no desktop or full CUDA compiler toolkit).'
        Invoke-LinuxScript $Distro 'root' (Get-Content -Raw (Join-Path $PSScriptRoot 'linux-system.sh'))
        Invoke-LinuxScript $Distro 'gromacs-console' $script
    }
    Save-WslSettings $Distro
    $message = 'Environment ready in {0:N0} seconds.' -f $timer.Elapsed.TotalSeconds
    Write-Host $message
    Add-Content -Encoding UTF8 $WslLog $message
}

try {
    if (-not $env:WINDIR) { throw 'Run this launcher on Windows.' }
    if ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64' -and $env:PROCESSOR_ARCHITEW6432 -ne 'AMD64') {
        throw 'This package requires 64-bit Windows on an Intel/AMD CPU.'
    }
    $build = [Environment]::OSVersion.Version.Build
    if ($build -lt 19041) { throw 'Windows 10 version 2004 or later, or Windows 11, is required.' }
    if ($Action -eq 'Diagnose') {
        foreach ($arguments in @(@('--version'), @('--status'), @('--list', '--verbose'))) {
            Invoke-WslCommand $arguments -ShowOutput | Out-Null
        }
        Write-Host "Diagnostic log: $WslLog"
        exit 0
    }
    if (-not $Distro) {
        $settings = Read-WslSettings
        if ($settings) { $Distro = $settings.distribution }
    }
    $Distro = Find-CompatibleWslDistribution -Preferred $Distro
    if (-not $Distro) {
        if ($Action -notin @('Start', 'Install')) { throw 'No configured Ubuntu 24.04 WSL2 environment was found.' }
        if ((Get-WslDistributions) -contains 'Ubuntu-24.04') {
            throw 'Ubuntu-24.04 already exists but is not compatible. If it uses WSL1, run: wsl --set-version Ubuntu-24.04 2. Then launch again.'
        }
        Write-Host 'Installing WSL2 and Ubuntu 24.04. Windows may request administrator permission and a restart.'
        if (-not (Test-Path $Wsl)) { throw 'Enable Windows Subsystem for Linux, restart Windows, and run this shortcut again.' }
        $Distro = Install-WslDistribution -SkipExistingCheck
        if (-not (Test-SupportedDistro $Distro)) { throw 'Restart Windows to finish WSL2 installation, then run this shortcut again.' }
    }
    $snapshot = Get-WslDistributionSnapshot $Distro
    $hasEnvironment = $null -ne $snapshot.InstalledRelease
    if ($Action -eq 'Install' -or ($Action -eq 'Start' -and -not $hasEnvironment)) {
        Install-Environment
        $hasEnvironment = $true
        $snapshot = Get-WslDistributionSnapshot $Distro -Refresh
    }
    if (-not $hasEnvironment -and $Action -ne 'Files') { throw 'Run the Configure / Update shortcut first.' }
    if ($hasEnvironment) { Save-WslSettings $Distro }
    switch ($Action) {
        'Start' {
            # Start also applies a changed application payload after a Windows update.
            $current = $snapshot.InstalledRelease
            $wanted = Get-Content -Raw (Join-Path $PSScriptRoot 'release.json') | ConvertFrom-Json
            if ($snapshot.PendingUpdate -or $current.installation_sha256 -ne $wanted.installation_sha256) { Install-Environment }
            $service = Invoke-Service 'start'
            $ready = Wait-LocalService $service.url
            if (-not $ready) { throw "Service is not reachable at $($service.url). Check $LinuxRoot/service.log in WSL." }
            Start-Process $service.url
            Write-Host 'The browser is open. Simulations keep running when the browser or this window is closed.'
            Write-Host 'Use the Stop Service shortcut after all jobs finish.'
        }
        'Stop' { Invoke-Service 'stop' | Out-Null; Write-Host 'Service stopped.' }
        'Files' {
            Start-Process explorer.exe -ArgumentList ('"\\wsl.localhost\' + $Distro + '\home\gromacs-console\.local\share\gromacs-console\runtime"')
        }
        'RemoveEnvironment' {
            Write-Host 'This removes the application and Python/GROMACS environment. Simulation results and settings are kept.'
            if ((Read-Host 'Type REMOVE to continue') -cne 'REMOVE') { exit 0 }
            Invoke-Service 'remove' | Out-Null
            Write-Host 'Environment removed. Results remain in the WSL runtime directory.'
        }
    }
    exit 0
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    Add-Content -Encoding UTF8 $WslLog $_.Exception.Message
    Write-Host "Diagnostic log: $WslLog"
    exit 1
}
