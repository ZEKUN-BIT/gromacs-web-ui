$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$script = Join-Path (Split-Path -Parent $PSScriptRoot) "packaging/windows/Sign-Installer.ps1"
$work = Join-Path ([IO.Path]::GetTempPath()) ("gromacs-signing-test-" + [guid]::NewGuid().ToString("N"))
$originalCertificate = $env:WINDOWS_SIGNING_PFX_BASE64
$originalPassword = $env:WINDOWS_SIGNING_PASSWORD
try {
    New-Item -ItemType Directory -Path $work | Out-Null
    $installer = Join-Path $work "sample.exe"
    [IO.File]::WriteAllBytes($installer, [byte[]](77, 90, 0, 1))
    Set-Content ($installer + ".sha256") "stale checksum"
    $env:WINDOWS_SIGNING_PFX_BASE64 = ""
    $env:WINDOWS_SIGNING_PASSWORD = ""
    & $script -Installer $installer
    $status = Get-Content (Join-Path $work "windows-signing.json") -Raw | ConvertFrom-Json
    $expected = (Get-FileHash $installer -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($status.status -ne "unsigned" -or $status.sha256 -ne $expected) { throw "Unsigned status/checksum was not truthful." }
    if ((Get-Content ($installer + ".sha256") -Raw).Trim() -ne "$expected  sample.exe") { throw "Final checksum was not regenerated." }
    Remove-Item (Join-Path $work "windows-signing.json")
    $env:WINDOWS_SIGNING_PFX_BASE64 = "incomplete-configuration"
    $failed = $false
    try { & $script -Installer $installer } catch { $failed = $true }
    if (-not $failed -or (Test-Path (Join-Path $work "windows-signing.json"))) { throw "Partial signing configuration must fail." }
    Write-Host "Windows signing configuration tests passed. Certificate-backed signing remains a release-time check."
} finally {
    $env:WINDOWS_SIGNING_PFX_BASE64 = $originalCertificate
    $env:WINDOWS_SIGNING_PASSWORD = $originalPassword
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}
