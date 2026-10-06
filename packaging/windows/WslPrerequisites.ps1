# Elevated helper: installs only WSL base components, never a user distribution.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-f0-9]{32}$')]
    [string]$LogId,
    [switch]$WebDownload
)
$ErrorActionPreference = 'Stop'
$logDirectory = Join-Path $env:ProgramData 'GromacsConsole\SetupLogs'
New-Item -ItemType Directory -Force $logDirectory | Out-Null
$logFile = Join-Path $logDirectory ($LogId + '.log')
Start-Transcript -Path $logFile -Force | Out-Null
$code = 1
try {
    $pending = @('Microsoft-Windows-Subsystem-Linux', 'VirtualMachinePlatform') | ForEach-Object {
        Get-WindowsOptionalFeature -Online -FeatureName $_
    } | Where-Object { $_.State -eq 'EnablePending' -or $_.State -eq 'EnabledPending' }
    if ($pending) {
        Write-Host 'Windows optional features are waiting for a restart.'
        $code = 3010
    } else {
        $wslExe = Join-Path $env:WINDIR 'System32\wsl.exe'
        $arguments = @('--install', '--no-distribution')
        if ($WebDownload) { $arguments += '--web-download' }
        Write-Host ('Running: wsl ' + ($arguments -join ' '))
        # Decode Windows WSL output from raw bytes before writing the transcript.
        . (Join-Path $PSScriptRoot 'WslSetup.ps1')
        $Wsl = $wslExe
        $WslLog = Join-Path $logDirectory ($LogId + '.native.log')
        $native = Invoke-WslCommand $arguments -ShowOutput -TimeoutSeconds (Get-WslTimeout 'SETUP')
        $code = $native.ExitCode
        if ($code -eq 0) {
            $pending = @('Microsoft-Windows-Subsystem-Linux', 'VirtualMachinePlatform') | ForEach-Object {
                Get-WindowsOptionalFeature -Online -FeatureName $_
            } | Where-Object { $_.State -eq 'EnablePending' -or $_.State -eq 'EnabledPending' }
            if ($pending) { $code = 3010 }
        }
    }
} catch {
    Write-Host $_.Exception.Message
    $code = 1
} finally {
    Write-Host "WSL base setup exit code: $code"
    Stop-Transcript | Out-Null
}
exit $code
