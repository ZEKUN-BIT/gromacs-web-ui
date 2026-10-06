# Isolated host-selection and dispatch regressions; no WSL setup or UAC prompt.
$ErrorActionPreference = 'Stop'
$repository = Split-Path $PSScriptRoot -Parent
. (Join-Path $repository 'packaging/windows/WslSetup.ps1')
$testDirectory = Join-Path ([IO.Path]::GetTempPath()) ('gromacs-host-test-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory $testDirectory | Out-Null
$WslLog = Join-Path $testDirectory 'host.log'

function Assert-True($Condition, [string]$Message) {
    if (-not $Condition) { throw "Assertion failed: $Message" }
}

function Write-PeFixture([string]$Path, [int]$Machine, [int]$Offset = 80) {
    $bytes = New-Object byte[] 128
    $bytes[0] = 77; $bytes[1] = 90
    [Array]::Copy([BitConverter]::GetBytes($Offset), 0, $bytes, 60, 4)
    [Array]::Copy([BitConverter]::GetBytes([int]0x4550), 0, $bytes, 80, 4)
    [Array]::Copy([BitConverter]::GetBytes([System.UInt16]$Machine), 0, $bytes, 84, 2)
    [IO.File]::WriteAllBytes($Path, $bytes)
}

$originalWindir = $env:WINDIR
$originalProgramData = $env:ProgramData
try {
    $amd64 = Join-Path $testDirectory 'pwsh-amd64.exe'
    $x86 = Join-Path $testDirectory 'pwsh-x86.exe'
    $arm64 = Join-Path $testDirectory 'pwsh-arm64.exe'
    $damaged = Join-Path $testDirectory 'pwsh-damaged.exe'
    Write-PeFixture $amd64 0x8664
    Write-PeFixture $x86 0x014c
    Write-PeFixture $arm64 0xaa64
    Write-PeFixture $damaged 0x8664 2147483647
    Assert-True (Test-Amd64Executable $amd64) 'read the real PE machine header and accept AMD64'
    Assert-True (-not (Test-Amd64Executable $x86)) 'reject x86 PowerShell binaries'
    Assert-True (-not (Test-Amd64Executable $arm64)) 'reject ARM64 on the Intel AMD installer'
    Assert-True (-not (Test-Amd64Executable $damaged)) 'reject an out-of-bounds PE header without unbounded reads'
    Assert-True (-not (Test-PowerShell7Host $amd64)) 'a PE file without PowerShell 7 version metadata is not a compatible host'
    [IO.File]::WriteAllText($damaged, 'not an executable')
    Assert-True (-not (Test-Amd64Executable $damaged)) 'reject truncated or non-PE host files'

    if ($env:OS -eq 'Windows_NT') {
        # Real installed hosts are executed read-only. CI runs this under both
        # Windows PowerShell 5.1 and PowerShell 7, including the disk API test.
        $selected = Get-PreferredPowerShellHost
        $fallback = Get-PreferredPowerShellHost -PowerShellCandidates @()
        Assert-True (Test-Amd64Executable $fallback) 'real fallback is native 64-bit Windows PowerShell'
        $major = & $fallback -NoLogo -NoProfile -NonInteractive -Command '[Console]::WriteLine($PSVersionTable.PSVersion.Major)'
        Assert-True ($LASTEXITCODE -eq 0 -and ($major | Out-String).Trim() -eq '5') 'explicit fallback executes the system Windows PowerShell 5 host'
        if ($PSVersionTable.PSVersion.Major -ge 7 -and [IntPtr]::Size -eq 8) {
            Assert-True (Test-PowerShell7Host $selected) 'prefer a real installed x64 PowerShell 7 host when available'
        }
    } else { Write-Host 'SKIP: Linux host; real Windows installed-host tests were not run.' }

    $env:WINDIR = Join-Path $testDirectory 'Windows'
    $env:ProgramData = Join-Path $testDirectory 'ProgramData'
    $fallback = Join-Path $env:WINDIR 'System32/WindowsPowerShell/v1.0/powershell.exe'
    New-Item -ItemType Directory -Force (Split-Path $fallback -Parent) | Out-Null
    [IO.File]::WriteAllText($fallback, 'fallback fixture')
    $stable = Join-Path $testDirectory 'Program Files/PowerShell/7/pwsh.exe'
    $portable = Join-Path $testDirectory 'Portable PowerShell/pwsh.exe'
    $script:compatible = @($stable, $portable)
    function Test-PowerShell7Host([string]$Path) { return $script:compatible -contains $Path }
    Assert-True ((Get-PreferredPowerShellHost -PowerShellCandidates @($stable, $portable)) -ceq $stable) 'prefer the first compatible installed PowerShell 7 candidate'
    Assert-True ((Get-PreferredPowerShellHost -PowerShellCandidates @($x86, $portable)) -ceq $portable) 'skip incompatible candidates and use a compatible portable PATH host'
    Assert-True ((Get-PreferredPowerShellHost -PowerShellCandidates @($x86, $damaged)) -ceq $fallback) 'fall back to system Windows PowerShell when no compatible PowerShell 7 exists'
    $sysnative = Join-Path $env:WINDIR 'Sysnative/WindowsPowerShell/v1.0/powershell.exe'
    New-Item -ItemType Directory -Force (Split-Path $sysnative -Parent) | Out-Null
    [IO.File]::WriteAllText($sysnative, 'native fixture')
    Assert-True ((Get-PreferredPowerShellHost -PowerShellCandidates @()) -ceq $sysnative) 'a 32-bit bootstrap uses the Sysnative fallback instead of a redirected x86 host'

    # The real elevation coordinator must use the same selector. Substitute
    # only Start-Process so this test never opens UAC or runs prerequisites.
    function Get-PowerShell7Candidates { return @($stable) }
    function Start-Process([string]$FilePath, [string[]]$ArgumentList, [string]$Verb, [switch]$Wait, [switch]$PassThru) {
        Assert-True ($FilePath -ceq $stable -and $Verb -eq 'RunAs' -and $Wait -and $PassThru) 'elevated prerequisites use the same selected PowerShell 7 executable'
        Assert-True ($ArgumentList -contains '-NoProfile' -and ($ArgumentList -join ' ') -match 'WslPrerequisites\.ps1') 'elevation executes only the prerequisites helper without user profiles'
        $process = [pscustomobject]@{ ExitCode = 0 }
        $process | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value { }
        $process | Add-Member -MemberType ScriptMethod -Name Refresh -Value { }
        return $process
    }
    try { Install-WslPrerequisites } finally { Remove-Item Function:\Start-Process }

    # Execute the production bootstrap dispatch in a harmless fixture folder.
    $tokens = $null; $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile(
        (Join-Path $repository 'packaging/windows/PowerShellHost.ps1'), [ref]$tokens, [ref]$errors)
    Assert-True ($errors.Count -eq 0) 'shared host bootstrap parses in the running host'
    $entry = $ast.EndBlock.Statements[-1].Extent.Text
    $fixture = Join-Path $testDirectory 'Launcher With Spaces'
    New-Item -ItemType Directory $fixture | Out-Null
    $launcher = Join-Path $fixture 'PowerShellHost.ps1'
    $fixtureBody = @'
param([string]$LaunchAction, [string]$FixtureHost)
$ErrorActionPreference = 'Stop'
function Get-PreferredPowerShellHost { return $FixtureHost }
'@
    [IO.File]::WriteAllText($launcher, ($fixtureBody + "`n" + $entry))
    [IO.File]::WriteAllText((Join-Path $fixture 'Console.ps1'), @'
param([string]$Action)
[Console]::WriteLine("dispatched:$Action")
if ($Action -eq 'Diagnose') { exit 23 }
exit 0
'@)
    $Wsl = (Get-Process -Id $PID).Path
    foreach ($action in @('Start', 'Install', 'Stop', 'Files', 'RemoveEnvironment', 'Diagnose')) {
        $result = Invoke-WslCommand @('-NoLogo', '-NoProfile', '-File', $launcher, '-LaunchAction', $action, '-FixtureHost', $Wsl)
        $expectedCode = if ($action -eq 'Diagnose') { 23 } else { 0 }
        Assert-True ($result.ExitCode -eq $expectedCode -and $result.StdoutLines -contains "dispatched:$action") 'the common launcher forwards every action and preserves its exit status'
    }
    Write-Host 'Windows PowerShell host selection regression tests passed.'
} finally {
    $env:WINDIR = $originalWindir
    $env:ProgramData = $originalProgramData
    Remove-Item -Recurse -Force $testDirectory
}
