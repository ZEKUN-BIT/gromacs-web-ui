# A shared selector for the command launcher and elevated prerequisites helper.
# Dot-sourcing defines functions only; executing this script opens Console.ps1.
[CmdletBinding()]
param(
    [ValidateSet('Start', 'Install', 'Stop', 'Files', 'RemoveEnvironment', 'Diagnose')]
    [string]$LaunchAction = 'Start'
)

function Test-Amd64Executable([string]$Path) {
    $stream = $null
    $reader = $null
    try {
        $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        $reader = New-Object IO.BinaryReader($stream)
        if ($stream.Length -lt 64 -or $reader.ReadUInt16() -ne 0x5A4D) { return $false }
        [void]$stream.Seek(60, [IO.SeekOrigin]::Begin)
        $offset = $reader.ReadInt32()
        if ($offset -lt 64 -or $offset -gt $stream.Length - 6) { return $false }
        [void]$stream.Seek($offset, [IO.SeekOrigin]::Begin)
        return ($reader.ReadUInt32() -eq 0x00004550 -and $reader.ReadUInt16() -eq 0x8664)
    } catch { return $false }
    finally {
        if ($reader) { $reader.Dispose() }
        if ($stream) { $stream.Dispose() }
    }
}

function Test-PowerShell7Host([string]$Path) {
    if (-not (Test-Amd64Executable $Path)) { return $false }
    try { return ([Diagnostics.FileVersionInfo]::GetVersionInfo($Path).ProductMajorPart -ge 7) }
    catch { return $false }
}

function Get-PowerShell7Candidates {
    $candidates = New-Object 'Collections.Generic.List[string]'
    foreach ($directory in @($env:ProgramW6432, $env:ProgramFiles)) {
        if ($directory) { $candidates.Add((Join-Path $directory 'PowerShell\7\pwsh.exe')) }
    }
    $current = (Get-Process -Id $PID).Path
    if ([IO.Path]::GetFileName($current) -ieq 'pwsh.exe') { $candidates.Add($current) }
    $registry = 'HKLM:\SOFTWARE\Microsoft\PowerShellCore\InstalledVersions'
    if ($env:OS -eq 'Windows_NT' -and (Test-Path $registry)) {
        foreach ($key in Get-ChildItem $registry -ErrorAction SilentlyContinue) {
            $entry = Get-ItemProperty -LiteralPath $key.PSPath -ErrorAction SilentlyContinue
            if ($entry.InstallLocation) { $candidates.Add((Join-Path $entry.InstallLocation 'pwsh.exe')) }
        }
    }
    # Store installations expose an execution alias instead of a readable PE
    # executable on PATH. Resolve the registered package's real installation.
    if ($env:OS -eq 'Windows_NT') {
        try {
            $appx = Get-Command Get-AppxPackage -ErrorAction SilentlyContinue
            if ($appx) {
                foreach ($package in & $appx -Name Microsoft.PowerShell -ErrorAction SilentlyContinue) {
                    if ($package.InstallLocation) { $candidates.Add((Join-Path $package.InstallLocation 'pwsh.exe')) }
                }
            }
        } catch { } # The Appx module may be unavailable on some managed systems.
    }
    foreach ($command in Get-Command pwsh.exe -CommandType Application -All -ErrorAction SilentlyContinue) {
        $candidates.Add($command.Source)
    }
    return @($candidates | Select-Object -Unique)
}

function Get-WindowsPowerShellHost {
    if (-not $env:WINDIR) { throw 'The desktop launcher requires Windows.' }
    foreach ($directory in @('Sysnative', 'System32')) {
        $path = Join-Path $env:WINDIR ($directory + '\WindowsPowerShell\v1.0\powershell.exe')
        if (Test-Path -LiteralPath $path) { return $path }
    }
    throw 'Windows PowerShell could not be found. Restore the Windows PowerShell system component and try again.'
}

function Get-PreferredPowerShellHost([AllowEmptyCollection()][string[]]$PowerShellCandidates = (Get-PowerShell7Candidates)) {
    foreach ($candidate in $PowerShellCandidates) {
        if ($candidate -and (Test-PowerShell7Host $candidate)) { return $candidate }
    }
    return (Get-WindowsPowerShellHost)
}

if ($MyInvocation.InvocationName -ne '.') {
    $ErrorActionPreference = 'Stop'
    try {
        $hostPath = Get-PreferredPowerShellHost
        & $hostPath -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'Console.ps1') -Action $LaunchAction
        exit $LASTEXITCODE
    } catch {
        Write-Host ('Could not launch GROMACS Console: ' + $_.Exception.Message) -ForegroundColor Red
        exit 1
    }
}
