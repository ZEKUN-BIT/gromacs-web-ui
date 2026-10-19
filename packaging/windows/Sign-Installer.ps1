param(
    [Parameter(Mandatory = $true)][string]$Installer,
    [string]$StatusFile = "",
    [string]$TimestampUrl = "http://timestamp.digicert.com"
)

# Run only on a trusted Windows build runner. Secrets are supplied through the
# environment, never written to release notes, the payload or build logs.
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$installerPath = (Resolve-Path -LiteralPath $Installer).Path
if ([IO.Path]::GetExtension($installerPath) -ne ".exe") { throw "Signing expects the generated EXE installer." }
if (-not $StatusFile) { $StatusFile = Join-Path ([IO.Path]::GetDirectoryName($installerPath)) "windows-signing.json" }
$hasCertificate = -not [string]::IsNullOrWhiteSpace($env:WINDOWS_SIGNING_PFX_BASE64)
$hasPassword = -not [string]::IsNullOrWhiteSpace($env:WINDOWS_SIGNING_PASSWORD)
if ($hasCertificate -ne $hasPassword) { throw "Configure both Windows signing secrets, or neither." }
$status = "unsigned"
$reason = "Signing certificate is not configured."
$certificateFile = Join-Path ([IO.Path]::GetTempPath()) ("gromacs-signing-" + [guid]::NewGuid().ToString("N") + ".pfx")
try {
    if ($hasCertificate) {
        $signToolCommand = Get-Command signtool.exe -ErrorAction SilentlyContinue
        $signTool = if ($signToolCommand) { $signToolCommand.Source } else { $null }
        if (-not $signTool) {
            $sdk = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin"
            $signTool = Get-ChildItem "$sdk\*\x64\signtool.exe" -ErrorAction SilentlyContinue |
                Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
        }
        if (-not $signTool) { throw "Windows SDK SignTool was not found." }
        [IO.File]::WriteAllBytes($certificateFile, [Convert]::FromBase64String($env:WINDOWS_SIGNING_PFX_BASE64))
        & $signTool sign /fd SHA256 /tr $TimestampUrl /td SHA256 /f $certificateFile /p $env:WINDOWS_SIGNING_PASSWORD $installerPath
        if ($LASTEXITCODE -ne 0) { throw "Installer signing or RFC3161 timestamp failed." }
        & $signTool verify /pa /all /tw $installerPath
        if ($LASTEXITCODE -ne 0) { throw "Installer signature verification failed." }
        $signature = Get-AuthenticodeSignature -LiteralPath $installerPath
        if ($signature.Status -ne "Valid" -or -not $signature.TimeStamperCertificate) {
            throw "The installer needs a valid, timestamped Authenticode signature."
        }
        $status = "signed"
        $reason = "SHA-256 Authenticode and RFC3161 timestamp verified on the build runner."
    }
    $checksum = (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash.ToLowerInvariant()
    [IO.File]::WriteAllText($installerPath + ".sha256", "$checksum  $([IO.Path]::GetFileName($installerPath))`n", [Text.Encoding]::ASCII)
    @{ status = $status; reason = $reason; artifact = [IO.Path]::GetFileName($installerPath); sha256 = $checksum } |
        ConvertTo-Json | Set-Content -LiteralPath $StatusFile -Encoding UTF8
    Write-Host "Installer signing status: $status. $reason"
} finally {
    if (Test-Path -LiteralPath $certificateFile) { Remove-Item -LiteralPath $certificateFile -Force }
    # The PFX is passed directly to SignTool, without importing a persistent key.
    Remove-Item Env:WINDOWS_SIGNING_PFX_BASE64 -ErrorAction SilentlyContinue
    Remove-Item Env:WINDOWS_SIGNING_PASSWORD -ErrorAction SilentlyContinue
}
