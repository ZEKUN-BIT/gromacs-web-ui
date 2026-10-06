# Dependency-free regressions; Windows CLI probes are read-only, provisioning is mocked.
param([switch]$RequireWindowsWsl)
$ErrorActionPreference = 'Stop'
$repository = Split-Path $PSScriptRoot -Parent
. (Join-Path $repository 'packaging/windows/WslSetup.ps1')
$testDirectory = Join-Path ([IO.Path]::GetTempPath()) ('gromacs-wsl-test-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory $testDirectory | Out-Null
$WslLog = Join-Path $testDirectory 'setup.log'

function Assert-True($Condition, [string]$Message) {
    if (-not $Condition) { throw "Assertion failed: $Message" }
}

try {
    # Execute the exact production counter declarations on every platform. AST
    # parsing alone accepts unknown aliases such as [ulong] in Windows PS 5.1.
    $spaceFunction = (Get-Command Get-WindowsFreeSpace).ScriptBlock.ToString()
    $declarations = [regex]::Matches($spaceFunction, '\[[^\]]+\]\$(available|total|free)\s*=\s*0')
    Assert-True ($declarations.Count -eq 3) 'locate all three native disk-space counter declarations'
    foreach ($declaration in $declarations) {
        Invoke-Expression $declaration.Value
        $counter = Get-Variable -Name $declaration.Groups[1].Value -ValueOnly
        Assert-True ($counter.GetType().FullName -ceq 'System.UInt64') 'resolve and execute the production unsigned counter declaration in the running PowerShell host'
    }

    $oldDownloadTimeout = [Environment]::GetEnvironmentVariable('GROMACS_CONSOLE_DOWNLOAD_TIMEOUT_SECONDS')
    try {
        [Environment]::SetEnvironmentVariable('GROMACS_CONSOLE_DOWNLOAD_TIMEOUT_SECONDS', $null)
        Assert-True ((Get-LinuxDownloadTimeoutExport) -ceq '') 'an unset Windows download timeout preserves the Linux default'
        foreach ($value in @('1', '86400', ' 120 ')) {
            [Environment]::SetEnvironmentVariable('GROMACS_CONSOLE_DOWNLOAD_TIMEOUT_SECONDS', $value)
            $expected = 'export GROMACS_CONSOLE_DOWNLOAD_TIMEOUT_SECONDS=' + ([int]$value).ToString() + "`n"
            Assert-True ((Get-LinuxDownloadTimeoutExport) -ceq $expected) 'export a canonical numeric download deadline into the Linux script'
        }
        foreach ($value in @('0', '-1', '86401', '2147483648', '1.5', 'abc', '1; touch injected', "10`nexport INJECTED=1", '$(whoami)')) {
            [Environment]::SetEnvironmentVariable('GROMACS_CONSOLE_DOWNLOAD_TIMEOUT_SECONDS', $value)
            $message = ''
            try { Get-LinuxDownloadTimeoutExport | Out-Null } catch { $message = $_.Exception.Message }
            Assert-True ($message -match 'integer between 1 and 86400') 'reject invalid or shell-containing Windows download timeout values'
        }
    } finally { [Environment]::SetEnvironmentVariable('GROMACS_CONSOLE_DOWNLOAD_TIMEOUT_SECONDS', $oldDownloadTimeout) }

    $settingsPath = Join-Path $testDirectory 'settings.json'
    Save-WslSettings 'Ubuntu Alpha' $settingsPath
    $written = (Get-Item $settingsPath).LastWriteTimeUtc.Ticks
    Start-Sleep -Milliseconds 30
    Save-WslSettings 'Ubuntu Alpha' $settingsPath
    Assert-True ((Get-Item $settingsPath).LastWriteTimeUtc.Ticks -eq $written) 'unchanged WSL settings are not rewritten on every launch'
    Save-WslSettings 'Ubuntu Beta' $settingsPath
    Assert-True ((Read-WslSettings $settingsPath).distribution -ceq 'Ubuntu Beta') 'atomically replace changed distribution configuration'
    Assert-True (((Get-Content -Raw ($settingsPath + '.previous')) | ConvertFrom-Json).distribution -ceq 'Ubuntu Alpha') 'retain last valid configuration as atomic replace backup'
    [IO.File]::WriteAllText($settingsPath, '{interrupted')
    Assert-True ((Read-WslSettings $settingsPath).distribution -ceq 'Ubuntu Alpha') 'recover damaged current settings from valid previous configuration'
    Assert-True (@(Get-ChildItem $testDirectory -Filter 'settings.json.damaged.*').Count -eq 1) 'preserve damaged configuration for diagnosis'
    Save-WslSettings 'Ubuntu Alpha' $settingsPath
    [IO.File]::WriteAllText(($settingsPath + '.unfinished.tmp'), '{unfinished')
    Assert-True ((Read-WslSettings $settingsPath).distribution -ceq 'Ubuntu Alpha') 'unfinished temporary writes do not damage live configuration'

    # WSL parses its raw command line: quoting a switch can turn it into a Linux command.
    Assert-True ((ConvertTo-NativeArgument '--install') -ceq '--install') 'leave WSL switches unquoted'
    Assert-True ((ConvertTo-NativeArgument '--no-distribution') -ceq '--no-distribution') 'leave base-install switch unquoted'
    Assert-True ((ConvertTo-NativeArgument 'Ubuntu-24.04') -ceq 'Ubuntu-24.04') 'leave ordinary values unquoted'
    Assert-True ((ConvertTo-NativeArgument '') -ceq '""') 'retain empty native arguments'
    Assert-True ((ConvertTo-NativeArgument 'Ubuntu Work') -ceq '"Ubuntu Work"') 'quote distro names with spaces'
    $setupLine = (@('--install', '--no-distribution', '--web-download') | ForEach-Object { ConvertTo-NativeArgument $_ }) -join ' '
    Assert-True ($setupLine -ceq '--install --no-distribution --web-download') 'base installation stays a Windows WSL command'

    if ($env:OS -eq 'Windows_NT') {
        $space = Get-WindowsFreeSpace $testDirectory
        Assert-True ($space.AvailableBytes -ge 0 -and $space.Volume) 'real Windows disk API checks the actual temporary filesystem volume'
        $Wsl = Join-Path $env:WINDIR 'System32\wsl.exe'
        if (Test-Path (Join-Path $env:WINDIR 'Sysnative\wsl.exe')) {
            $Wsl = Join-Path $env:WINDIR 'Sysnative\wsl.exe'
        }
        if (Test-Path $Wsl) {
            # Real wsl.exe must parse --help on Windows, even with no distro installed.
            # Do not enable features, install, start, convert or unregister a distro.
            $help = Invoke-WslCommand @('--help')
            Assert-True ($help.ExitCode -eq 0) "real Windows WSL help succeeds (exit $($help.ExitCode)): $($help.Lines -join ' ')"
            Assert-True (($help.Lines -join "`n") -match '--distribution' -and ($help.Lines -join "`n") -match '--exec') 'real WSL recognizes a host-side option'
            Write-Host 'Real Windows wsl.exe read-only CLI test passed.'
        } elseif ($RequireWindowsWsl) { throw 'Required Windows wsl.exe is missing.' }
        else { Write-Host 'SKIP: wsl.exe is unavailable; real WSL CLI test was not run.' }
    } elseif ($RequireWindowsWsl) { throw 'Real WSL CLI test requires Windows.' }
    else { Write-Host 'SKIP: Linux host; real Windows WSL CLI test was not run.' }

    # Exercise the actual native-command wrapper, including stderr and failed exit status.
    $Wsl = (Get-Process -Id $PID).Path
    $result = Invoke-WslCommand @('-NoProfile', '-Command', "[Console]::Error.WriteLine('native DNS detail'); exit 17")
    Assert-True ($result.ExitCode -eq 17) 'preserve native exit status'
    Assert-True (($result.Lines -join "`n") -match 'native DNS detail') 'capture native stderr'
    Assert-True ((Get-Content -Raw $WslLog) -match 'native DNS detail') 'persist failure details'
    Assert-True ($ErrorActionPreference -eq 'Stop') 'restore caller error preference'
    Assert-True ($result.StdoutLines.Count -eq 0 -and $result.StderrLines -contains 'native DNS detail') 'keep native stdout separate from WSL diagnostics'

    # A timed-out native invocation must preserve its diagnostics and terminate
    # only its own process, leaving another launcher process alive.
    $unrelated = New-Object Diagnostics.Process
    $unrelated.StartInfo = New-Object Diagnostics.ProcessStartInfo
    $unrelated.StartInfo.FileName = $Wsl
    $unrelated.StartInfo.Arguments = '-NoProfile -Command "Start-Sleep -Seconds 20"'
    $unrelated.StartInfo.UseShellExecute = $false
    $unrelated.StartInfo.CreateNoWindow = $true
    [void]$unrelated.Start()
    try {
        $timer = [Diagnostics.Stopwatch]::StartNew()
        $message = ''
        try { Invoke-WslCommand @('-NoProfile', '-Command', "[Console]::WriteLine('deadline child ready'); Start-Sleep -Seconds 20") -TimeoutSeconds 1 | Out-Null }
        catch { $message = $_.Exception.Message }
        Assert-True ($message -match '1-second deadline' -and $timer.Elapsed.TotalSeconds -lt 7) 'real native child deadline is bounded'
        Assert-True (-not $unrelated.HasExited) 'native timeout does not terminate unrelated processes'
        Assert-True ((Get-Content -Raw $WslLog) -match 'deadline child ready' -and (Get-Content -Raw $WslLog) -match 'deadline') 'retain output and timeout diagnosis'
    } finally {
        if (-not $unrelated.HasExited) { $unrelated.Kill(); [void]$unrelated.WaitForExit(5000) }
        $unrelated.Dispose()
    }
    $dns = Get-WslFailureMessage ([Convert]::ToInt32('80072EE7', 16))
    Assert-True ($dns -match '0x80072EE7' -and $dns -match 'DNS') 'show HRESULT and DNS advice'
    Assert-True ($dns -notmatch 'restart') 'do not tell users to reboot on download failure'

    $unicodeText = ([string][char]0x6D4B + [char]0x8BD5) + ' https://raw.githubusercontent.com/example WININET_E_TIMEOUT'
    $utf16 = [Text.Encoding]::Unicode.GetBytes($unicodeText)
    $utf8 = [Text.Encoding]::UTF8.GetBytes($unicodeText)
    Assert-True ((ConvertFrom-WslBytes $utf16) -ceq $unicodeText) 'decode BOM-less UTF-16 without corrupting Chinese'
    Assert-True ((ConvertFrom-WslBytes $utf8) -ceq $unicodeText) 'decode Linux UTF-8 output'
    Assert-True ((ConvertFrom-WslBytes ([byte[]](239, 187, 191) + $utf8)) -ceq $unicodeText) 'remove UTF-8 BOM before identifying distro names'
    $encoded = [Convert]::ToBase64String($utf16)
    $result = Invoke-WslCommand @('-NoProfile', '-Command', "[Console]::OpenStandardOutput().Write([Convert]::FromBase64String('$encoded'),0,$($utf16.Length)); exit 0")
    Assert-True ($result.Lines[0] -ceq $unicodeText) 'capture native UTF-16 bytes before PowerShell decoding'

    $child = Join-Path $testDirectory 'argument-test.ps1'
    Set-Content -Encoding ASCII $child 'param([string]$Value); Write-Output $Value'
    $argument = 'C:\Program Files\a"quoted\'
    $result = Invoke-WslCommand @('-NoProfile', '-File', $child, '-Value', $argument)
    Assert-True ($result.Lines[0] -ceq $argument) 'preserve spaces, embedded quotes and trailing backslashes in native arguments'
    Assert-True ((Get-Content -Raw $WslLog).Contains($Wsl)) 'log the actual executable path'
    Assert-True ((Get-Content -Raw $WslLog).Contains('-NoProfile -File ')) 'log the serialized command line with unquoted switches'

    # Native stdin must contain only the script, without PowerShell's added CRLF/BOM.
    $inputScript = "# $unicodeText`r`nprintf 'prepared'`r`n"
    $readStdin = '$buffer = New-Object IO.MemoryStream; [Console]::OpenStandardInput().CopyTo($buffer); [Console]::WriteLine([Convert]::ToBase64String($buffer.ToArray()))'
    $result = Invoke-WslCommand @('-NoProfile', '-Command', $readStdin) -InputText $inputScript
    $expectedBytes = [Text.Encoding]::UTF8.GetBytes($inputScript.Replace("`r`n", "`n"))
    Assert-True ($result.ExitCode -eq 0 -and $result.Lines[0] -ceq [Convert]::ToBase64String($expectedBytes)) 'send exact UTF-8/LF stdin bytes and EOF'
    $result = Invoke-WslCommand @('-NoProfile', '-Command', $readStdin) -InputText ''
    Assert-True ($result.ExitCode -eq 0 -and $result.Lines.Count -eq 0) 'close even empty redirected stdin'

    # Observe real child output while the child is still alive, including one
    # UTF-8 stdout and one Windows UTF-16 stderr stream. The child creates its
    # marker only after a delay, so buffered-at-exit output cannot pass this test.
    $streamChild = Join-Path $testDirectory 'stream-test.ps1'
    $finished = Join-Path $testDirectory 'stream-child-finished'
    $script:streamObserved = @()
    $script:earlyObserved = @()
    $streamScript = @'
param([string]$Finished)
$chinese = [string][char]0x6D4B + [char]0x8BD5
$out = [Console]::OpenStandardOutput()
$err = [Console]::OpenStandardError()
$first = [Text.Encoding]::UTF8.GetBytes("stream.stdout.$chinese`n")
$out.Write($first, 0, $first.Length)
$out.Flush()
$errorBytes = [Text.Encoding]::Unicode.GetBytes("stream.stderr.$chinese`n")
$err.Write($errorBytes, 0, $errorBytes.Length)
$err.Flush()
# Split a multibyte character after the stream encoding has been identified.
$second = [Text.Encoding]::UTF8.GetBytes("stream.split.$chinese`n")
$out.Write($second, 0, $second.Length - 2)
$out.Flush()
Start-Sleep -Milliseconds 200
$out.Write($second, $second.Length - 2, 2)
$out.Flush()
Start-Sleep -Milliseconds 1000
[IO.File]::WriteAllText($Finished, 'done')
$tail = [Text.Encoding]::UTF8.GetBytes('stream.final.without-newline')
$out.Write($tail, 0, $tail.Length)
$out.Flush()
exit 19
'@
    Set-Content -Encoding ASCII $streamChild $streamScript
    function Write-Host([object]$Object) {
        $line = [string]$Object
        $script:streamObserved += $line
        if ($line.StartsWith('stream.stdout.') -or $line.StartsWith('stream.stderr.')) {
            Assert-True (-not (Test-Path $finished)) 'display native output before child exits'
            Assert-True ((Get-Content -Raw $WslLog).Contains($line)) 'persist the same phase immediately before display'
            $script:earlyObserved += $line
        }
    }
    try {
        $result = Invoke-WslCommand @('-NoProfile', '-File', $streamChild, '-Finished', $finished) -ShowOutput -StreamOutput
    } finally { Remove-Item Function:\Write-Host }
    Assert-True ($result.ExitCode -eq 19) 'streaming retains native failure status'
    Assert-True ($script:earlyObserved.Count -eq 2) 'both stdout and stderr stream before exit'
    $expectedChinese = [string][char]0x6D4B + [char]0x8BD5
    Assert-True ($script:streamObserved -contains "stream.stdout.$expectedChinese") 'stream Linux UTF-8 without corrupting Chinese'
    Assert-True ($script:streamObserved -contains "stream.stderr.$expectedChinese") 'stream Windows UTF-16 error without corrupting Chinese'
    Assert-True ($script:streamObserved -contains "stream.split.$expectedChinese") 'retain split UTF-8 characters across asynchronous reads'
    Assert-True ($script:streamObserved -contains 'stream.final.without-newline') 'flush the final unterminated output line'
    Assert-True ($script:streamObserved.Count -eq 4) 'do not replay already streamed output at exit'
    Assert-True (@(Select-String -Path $WslLog -SimpleMatch "stream.stdout.$expectedChinese").Count -eq 1) 'do not duplicate streamed log lines'
    Assert-True ($result.Lines -contains "stream.stdout.$expectedChinese" -and $result.Lines -contains "stream.stderr.$expectedChinese") 'retain complete decoded command results after streaming'

    $invalidChild = Join-Path $testDirectory 'stream-invalid-byte.ps1'
    $invalidScript = @'
$out = [Console]::OpenStandardOutput()
$first = [Text.Encoding]::UTF8.GetBytes("stream.valid-first`n")
$out.Write($first, 0, $first.Length)
$out.Flush()
Start-Sleep -Milliseconds 200
$later = [Text.Encoding]::ASCII.GetBytes('stream.invalid-later.') + [byte[]](255, 10)
$out.Write($later, 0, $later.Length)
$out.Flush()
exit 23
'@
    Set-Content -Encoding ASCII $invalidChild $invalidScript
    $script:invalidObserved = @()
    function Write-Host([object]$Object) { $script:invalidObserved += [string]$Object }
    try {
        $result = Invoke-WslCommand @('-NoProfile', '-File', $invalidChild) -ShowOutput -StreamOutput
    } finally { Remove-Item Function:\Write-Host }
    Assert-True ($result.ExitCode -eq 23) 'malformed later output bytes do not abort installation or lose exit status'
    Assert-True ($script:invalidObserved.Count -eq 2 -and $script:invalidObserved[1].StartsWith('stream.invalid-later.')) 'display malformed later output safely'
    Assert-True ($result.Lines[0] -eq 'stream.valid-first') 'retain original complete byte decoding when streamed output is malformed'

    # Real WSL can write its UTF-16 Windows proxy warning and then UTF-8 Linux
    # stderr into the very same pipe. Include both direction changes and EOF.
    $mixedChild = Join-Path $testDirectory 'mixed-encoding-test.ps1'
    $mixedScript = @'
param([int]$Mode, [int]$UnicodeTail, [string]$Finished)
$chinese = [string][char]0x6D4B + [char]0x8BD5
$utf16 = [Text.Encoding]::Unicode
$utf8 = [Text.Encoding]::UTF8
$boundary = [string][char]0x4E0D + [char]0x4E0A + $chinese
$bytes = $utf16.GetBytes("wsl: localhost proxy $chinese`r`n") +
    $utf8.GetBytes("Another installation is running.`n") +
    $utf16.GetBytes("$boundary`r`n") + $utf8.GetBytes("Linux UTF8 $chinese`n") +
    $utf16.GetBytes("wsl: Windows again $chinese`n")
$tail = if ($UnicodeTail) { $utf16.GetBytes("wsl: UTF16 EOF $chinese") } else { $utf8.GetBytes("UTF8 EOF $chinese") }
$bytes += $tail
$err = [Console]::OpenStandardError()
$random = New-Object Random 73
$offset = 0
while ($offset -lt $bytes.Length) {
    $size = switch ($Mode) { 0 { $bytes.Length } 1 { 1 } default { $random.Next(1, 10) } }
    $size = [Math]::Min($size, $bytes.Length - $offset)
    $err.Write($bytes, $offset, $size)
    $err.Flush()
    $offset += $size
    if ($Mode -eq 1) { Start-Sleep -Milliseconds 2 }
    elseif ($Mode -ne 0) { Start-Sleep -Milliseconds 5 }
}
Start-Sleep -Milliseconds 400
[IO.File]::WriteAllText($Finished, 'done')
exit 31
'@
    Set-Content -Encoding ASCII $mixedChild $mixedScript
    foreach ($mode in @(0, 1, 2)) {
        foreach ($unicodeTail in @(0, 1)) {
            $mixedFinished = Join-Path $testDirectory "mixed-finished-$mode-$unicodeTail"
            $script:mixedObserved = @()
            function Write-Host([object]$Object) {
                $line = [string]$Object
                $script:mixedObserved += $line
                if ($line -eq 'Another installation is running.') {
                    Assert-True (-not (Test-Path $mixedFinished)) 'display mixed Linux stderr before child exits'
                    Assert-True ((Get-Content -Raw $WslLog).Contains($line)) 'persist mixed Linux stderr immediately'
                }
            }
            try {
                $result = Invoke-WslCommand @('-NoProfile', '-File', $mixedChild, '-Mode', "$mode", '-UnicodeTail', "$unicodeTail", '-Finished', $mixedFinished) -ShowOutput -StreamOutput
            } finally { Remove-Item Function:\Write-Host }
            $expectedTail = if ($unicodeTail) { "wsl: UTF16 EOF $expectedChinese" } else { "UTF8 EOF $expectedChinese" }
            $expectedBoundary = [string][char]0x4E0D + [char]0x4E0A + $expectedChinese
            $expectedMixed = @("wsl: localhost proxy $expectedChinese", 'Another installation is running.', $expectedBoundary, "Linux UTF8 $expectedChinese", "wsl: Windows again $expectedChinese", $expectedTail)
            Assert-True ($result.ExitCode -eq 31) 'mixed encoding preserves native failed exit code'
            Assert-True (($script:mixedObserved -join '|') -ceq ($expectedMixed -join '|')) "display mixed encodings correctly for native chunk mode $mode and EOF $unicodeTail"
            Assert-True (($result.Lines -join '|') -ceq ($expectedMixed -join '|')) 'streamed and final mixed-encoding lines agree without duplicates'
        }
    }

    # Controlled one-byte/fixed/random feeds guarantee boundaries inside UTF-16
    # characters, CRLF and UTF-8 multibyte characters, independently of OS reads.
    $mixedBytes = [Text.Encoding]::Unicode.GetBytes("wsl: localhost proxy $expectedChinese`r`n") +
        [Text.Encoding]::UTF8.GetBytes("Another installation is running.`n") +
        [Text.Encoding]::Unicode.GetBytes("$expectedBoundary`r`n") + [Text.Encoding]::UTF8.GetBytes("Linux UTF8 $expectedChinese`n") +
        [Text.Encoding]::Unicode.GetBytes("wsl: Windows again $expectedChinese`n") +
        [Text.Encoding]::UTF8.GetBytes("UTF8 EOF $expectedChinese")
    $expectedMixed = @("wsl: localhost proxy $expectedChinese", 'Another installation is running.', $expectedBoundary, "Linux UTF8 $expectedChinese", "wsl: Windows again $expectedChinese", "UTF8 EOF $expectedChinese")
    Assert-True (((ConvertFrom-WslBytes $mixedBytes) -split '\r?\n' | Where-Object { $_ }) -join '|' -ceq ($expectedMixed -join '|')) 'whole-buffer decoder also handles mixed Windows and Linux output'
    foreach ($chunkSize in @(1, 2, 3, 7, 64, 0)) {
        $state = @{ Pending = New-Object IO.MemoryStream; Encoding = $null; ScanOffset = 0; AfterCR = 0; Lines = New-Object 'Collections.Generic.List[string]' }
        $random = New-Object Random 90210
        try {
            $offset = 0
            while ($offset -lt $mixedBytes.Length) {
                $size = if ($chunkSize) { $chunkSize } else { $random.Next(1, 12) }
                $size = [Math]::Min($size, $mixedBytes.Length - $offset)
                $chunk = New-Object byte[] $size
                [Array]::Copy($mixedBytes, $offset, $chunk, 0, $size)
                Publish-WslOutput $state $chunk
                $offset += $size
            }
            Publish-WslOutput $state ([byte[]]@()) -Final
            $actual = @($state.Lines | Where-Object { $_ })
            Assert-True (($actual -join '|') -ceq ($expectedMixed -join '|')) "mixed-encoding parser is independent of byte chunk size $chunkSize"
        } finally { $state.Pending.Dispose() }
    }

    $crlfText = "first`r`n`r`nlast"
    foreach ($encoding in @([Text.Encoding]::UTF8, [Text.Encoding]::Unicode)) {
        Assert-True ((ConvertFrom-WslBytes $encoding.GetBytes($crlfText)) -ceq "first`n`nlast") 'CRLF creates one separator while real blank lines are retained'
    }

    # A short ASCII status or blank line followed by Checking must not wait for
    # process EOF merely because C/V falls within UTF-16 CJK's high-byte range.
    $shortChild = Join-Path $testDirectory 'short-utf8-status.ps1'
    $shortScript = @'
param([int]$Blank, [string]$Finished)
$prefix = if ($Blank) { "`n" } else { "OK`n" }
$bytes = [Text.Encoding]::UTF8.GetBytes($prefix + "Verified more dependencies`nChecking current environment`n")
$out = [Console]::OpenStandardOutput()
$out.Write($bytes, 0, $bytes.Length)
$out.Flush()
Start-Sleep -Milliseconds 700
[IO.File]::WriteAllText($Finished, 'done')
exit 0
'@
    Set-Content -Encoding ASCII $shortChild $shortScript
    foreach ($blank in @(0, 1)) {
        $shortFinished = Join-Path $testDirectory "short-utf8-finished-$blank"
        $script:shortObserved = @()
        function Write-Host([object]$Object) {
            $line = [string]$Object
            $script:shortObserved += $line
            if ($line -eq 'Checking current environment') {
                Assert-True (-not (Test-Path $shortFinished)) 'show Checking before the delayed child exits after a short UTF-8 line'
            }
        }
        try {
            $result = Invoke-WslCommand @('-NoProfile', '-File', $shortChild, '-Blank', "$blank", '-Finished', $shortFinished) -ShowOutput -StreamOutput
        } finally { Remove-Item Function:\Write-Host }
        $expectedShort = if ($blank) { @('Verified more dependencies', 'Checking current environment') } else { @('OK', 'Verified more dependencies', 'Checking current environment') }
        Assert-True ($result.ExitCode -eq 0 -and ($script:shortObserved -join '|') -ceq ($expectedShort -join '|')) 'short/blank UTF-8 status retains real-time complete messages'
        Assert-True (($result.Lines -join '|') -ceq ($expectedShort -join '|')) 'short/blank UTF-8 final messages match display'
    }
    foreach ($prefix in @("OK`n", "`n")) {
        $state = @{ Pending = New-Object IO.MemoryStream; Encoding = $null; ScanOffset = 0; AfterCR = 0; Lines = New-Object 'Collections.Generic.List[string]' }
        try {
            $bytes = [Text.Encoding]::UTF8.GetBytes($prefix + "Checking current environment`n")
            foreach ($value in $bytes) { Publish-WslOutput $state ([byte[]]@($value)) }
            Assert-True ($state.Lines -contains 'Checking current environment') 'one-byte short/blank UTF-8 feeds complete without needing Final'
        } finally { $state.Pending.Dispose() }
    }

    # A child can fill stderr before reading stdin. Both must keep progressing.
    $duplex = '$large = New-Object string ([char]120), 200000; [Console]::Error.WriteLine($large); $text = [Console]::In.ReadToEnd(); [Console]::WriteLine($text.Length)'
    $result = Invoke-WslCommand @('-NoProfile', '-Command', $duplex) -InputText ('y' * 300000)
    Assert-True ($result.ExitCode -eq 0 -and $result.Lines[0] -eq '300000') 'drain large stderr while writing large stdin without deadlock'

    if ($env:OS -ne 'Windows_NT') {
        $Wsl = '/bin/bash'
        $result = Invoke-WslCommand @('--noprofile', '--norc', '-s') -InputText "set -euo pipefail`r`nprintf 'Bash stdin works\n'`r`n"
        Assert-True ($result.ExitCode -eq 0 -and $result.Lines[0] -ceq 'Bash stdin works') 'actual Bash accepts Windows script input after LF normalization'
        $result = Invoke-WslCommand @('--noprofile', '--norc', '-s') -InputText "gromacs_console_missing_command`n"
        Assert-True ($result.ExitCode -eq 127) 'preserve actual Bash missing-command status'
        Assert-True ((Get-Content -Raw $WslLog) -match 'gromacs_console_missing_command: command not found') 'persist Linux setup stderr in diagnostic log'
    }

    $script:commands = @()
    $script:elevations = 0
    $script:statusCode = 0
    $script:versionCode = 0
    $script:importCode = 0
    $script:webAvailable = $true
    $script:availableDistros = @('Ubuntu')
    $script:distroVersion = 2
    $script:ubuntuVersion = '24.04'
    $script:architecture = 'x86_64'
    $script:linuxExitCode = 127
    $script:downloads = 0
    $script:downloadPath = $null
    $script:rebootNeeded = $false
    $script:releaseJson = '{"installation_sha256":"installed","environment_sha256":"environment"}'
    $script:pendingUpdate = 0
    $script:spaceChecks = @()
    function Get-WindowsFreeSpace([string]$Path) {
        $script:spaceChecks += $Path
        return [pscustomobject]@{ Volume = $Path; AvailableBytes = 16GB }
    }
    $logDirectory = Join-Path $testDirectory 'Logs'
    New-Item -ItemType Directory $logDirectory | Out-Null
    $fixtureImage = Join-Path $testDirectory 'user-image.wsl'
    Set-Content -Encoding ASCII $fixtureImage 'fixture image bytes'
    $fixtureHash = (Get-FileHash -Algorithm SHA256 $fixtureImage).Hash.ToLowerInvariant()
    $script:spec = [pscustomobject]@{ url = 'https://releases.ubuntu.com/24.04/test.wsl'; sha256 = $fixtureHash }
    $UbuntuImage = $fixtureImage
    function Get-UbuntuRootfsSpec { return $script:spec }
    function Save-UbuntuRootfs([string]$Url, [string]$Destination) {
        $script:downloads++
        $script:downloadPath = $Destination
        Copy-Item $fixtureImage $Destination
    }
    function Invoke-WslCommand([string[]]$Arguments, [switch]$ShowOutput, [string]$InputText, [switch]$StreamOutput, [int]$TimeoutSeconds) {
        $script:commands += ,$Arguments
        switch ($Arguments[0]) {
            '--version' { return [pscustomobject]@{ ExitCode = $script:versionCode; Lines = @('WSL version: 2.6.0') } }
            '--list' {
                $lines = if ($Arguments[1] -eq '--quiet') { $script:availableDistros } else { @($script:availableDistros | ForEach-Object { "* $_ Running $script:distroVersion" }) }
                return [pscustomobject]@{ ExitCode = 0; Lines = @($lines) }
            }
            '--distribution' {
                if ($Arguments -contains 'bash') {
                    if ($InputText -match 'GC_RELEASE_BEGIN') {
                        Assert-True ($Arguments -contains 'timeout' -and $TimeoutSeconds -gt 0) 'bound the combined Linux probe with its own process-group deadline'
                        $lines = @('GC_ID=ubuntu', ('GC_VERSION=' + $script:ubuntuVersion), ('GC_ARCH=' + $script:architecture),
                            'GC_FREE_KB=16777216', 'GC_GPU=0', 'GC_SERVICE=1', 'GC_APPROOT=1', ('GC_PENDING=' + $script:pendingUpdate), 'GC_RELEASE_BEGIN', $script:releaseJson, 'GC_RELEASE_END')
                        return [pscustomobject]@{ ExitCode = 0; Lines = $lines + @('wsl: proxy diagnostic'); StdoutLines = $lines; StderrLines = @('wsl: proxy diagnostic') }
                    }
                    Assert-True ($Arguments -contains 'root' -and $Arguments[-1] -eq '-s') 'run setup in the requested WSL user with Bash stdin'
                    Assert-True ($ShowOutput -and $StreamOutput -and $InputText -ceq 'setup fixture') 'capture and stream Linux setup output'
                    Assert-True ($Arguments -contains 'timeout' -and $Arguments -contains '--kill-after=10s' -and $TimeoutSeconds -gt 0) 'setup deadline targets only this Linux process group'
                    return [pscustomobject]@{ ExitCode = $script:linuxExitCode; Lines = @('fixture: command not found') }
                }
                $lines = if ($Arguments -contains 'uname') { @($script:architecture) } else { @('ID=ubuntu', ('VERSION_ID="' + $script:ubuntuVersion + '"')) }
                return [pscustomobject]@{ ExitCode = 0; Lines = $lines }
            }
            '--help' {
                $help = if ($script:webAvailable) { '--web-download' } else { '--install' }
                return [pscustomobject]@{ ExitCode = 0; Lines = @($help) }
            }
            '--status' { return [pscustomobject]@{ ExitCode = $script:statusCode; Lines = @() } }
            '--import' { return [pscustomobject]@{ ExitCode = $script:importCode; Lines = @() } }
            default { throw ('Unexpected WSL command: ' + ($Arguments -join ' ')) }
        }
    }
    function Install-WslPrerequisites([switch]$WebDownload) {
        $script:elevations++
        if ($script:rebootNeeded) { throw 'Windows requires a restart.' }
    }

    $message = ''
    try { Invoke-LinuxScript 'Ubuntu' 'root' 'setup fixture' } catch { $message = $_.Exception.Message }
    Assert-True ($message -match 'Ubuntu' -and $message -match 'root' -and $message -match '127' -and $message.Contains($WslLog)) 'Linux setup failure identifies distro, user, native exit code and log'
    $script:linuxExitCode = 75
    $message = ''
    try { Invoke-LinuxScript 'Ubuntu' 'root' 'setup fixture' } catch { $message = $_.Exception.Message }
    Assert-True ($message -match 'Another Linux installation' -and $message -match 'Ubuntu' -and $message -match 'Configure/Update' -and $message.Contains($WslLog)) 'installation lock contention explains waiting and retrying with the diagnostic log'
    $script:linuxExitCode = 127

    $script:linuxExitCode = 124
    $message = ''
    try { Invoke-LinuxScript 'Ubuntu' 'root' 'setup fixture' } catch { $message = $_.Exception.Message }
    Assert-True ($message -match 'deadline' -and $message -match 'GROMACS_WSL_SETUP_TIMEOUT') 'Linux timeout identifies its configurable stage deadline'
    $script:linuxExitCode = 76
    $result = Invoke-LinuxScript 'Ubuntu' 'root' 'setup fixture' -AllowExitCodes @(76)
    Assert-True ($result.ExitCode -eq 76) 'return application-only repair fallback without hiding a normal failure'
    $script:linuxExitCode = 127

    $selected = Install-WslDistribution
    Assert-True ($selected -eq 'Ubuntu') 'reuse a compatible distro even when its name is not Ubuntu-24.04'
    Assert-True ($script:downloads -eq 0 -and $script:elevations -eq 0) 'compatible WSL skips all downloads and elevation'
    Assert-True (@($script:commands | Where-Object { $_[0] -eq '--import' }).Count -eq 0) 'compatible WSL is not imported again'
    Assert-True ((Find-CompatibleWslDistribution -Preferred 'Ubuntu') -eq 'Ubuntu') 'honor a saved compatible distro'
    $script:commands = @()
    Find-CompatibleWslDistribution -Preferred 'Ubuntu' | Out-Null
    $snapshot = Get-WslDistributionSnapshot 'Ubuntu'
    Assert-True ($script:commands.Count -eq 2) 'compatible daily launch batches Linux OS architecture marker and space into one call after one WSL2 listing'
    Assert-True ($snapshot.InstalledRelease.installation_sha256 -eq 'installed' -and $snapshot.LinuxFreeBytes -eq 16GB -and $snapshot.HasService) 'combined snapshot parses structured stdout despite proxy stderr'
    $script:pendingUpdate = 1
    Assert-True ((Get-WslDistributionSnapshot 'Ubuntu' -Refresh).PendingUpdate) 'detect interrupted application transaction even when the release marker still matches'
    $script:pendingUpdate = 0
    $script:releaseJson = '{damaged'
    $snapshot = Get-WslDistributionSnapshot 'Ubuntu' -Refresh
    Assert-True ($snapshot.Supported -and $null -eq $snapshot.InstalledRelease -and $snapshot.HasService) 'damaged release marker requests repair while retaining service presence protection'
    $script:releaseJson = '{"installation_sha256":"installed","environment_sha256":"environment"}'
    $script:availableDistros = @('Ubuntu Work')
    $script:versionCode = 1
    Assert-True ((Install-WslDistribution) -eq 'Ubuntu Work') 'reuse WSL2 with a spaced distro name even if --version is unavailable'
    Assert-True ($script:downloads -eq 0 -and $script:elevations -eq 0) 'older WSL version-reporting CLI does not trigger downloads'
    $script:availableDistros = @('Ubuntu')
    $script:versionCode = 0
    $script:distroVersion = 1
    Assert-True (-not (Test-SupportedDistro 'Ubuntu')) 'WSL1 is not compatible'
    $script:distroVersion = 2
    $script:ubuntuVersion = '22.04'
    Assert-True (-not (Test-SupportedDistro 'Ubuntu')) 'Ubuntu release is checked independently of WSL version'
    $script:ubuntuVersion = '24.04'
    $script:architecture = 'aarch64'
    Assert-True (-not (Test-SupportedDistro 'Ubuntu')) 'reject wrong Linux architecture'
    $script:architecture = 'x86_64'

    $script:backingPath = 'E:\\RelocatedWSL\\Ubuntu'
    function Get-WslBackingPath([string]$Name) { return $script:backingPath }
    $snapshot = Get-WslDistributionSnapshot 'Ubuntu' -Refresh
    $script:spaceChecks = @()
    Assert-EnvironmentSpace 'Ubuntu' $snapshot
    Assert-True ($script:spaceChecks -contains $script:backingPath) 'check the registered backing path rather than the launcher installation drive'
    function Get-WindowsFreeSpace([string]$Path) { return [pscustomobject]@{ Volume = $Path; AvailableBytes = 2GB } }
    $snapshot.GpuCandidate = $true
    $snapshot.InstalledRelease = $null
    $message = ''
    try { Assert-EnvironmentSpace 'Ubuntu' $snapshot } catch { $message = $_.Exception.Message }
    Assert-True ($message -match 'Not enough disk space' -and $message -match 'GPU candidate' -and $message.Contains($script:backingPath)) 'identify the actual insufficient volume for a first GPU installation'
    $snapshot.LinuxFreeBytes = 128MB
    $message = ''
    try { Assert-EnvironmentSpace 'Ubuntu' $snapshot -AppOnly } catch { $message = $_.Exception.Message }
    Assert-True ($message -match 'Linux filesystem' -and $message -match 'Application update') 'check Linux filesystem space independently of Windows VHD backing space'
    function Get-WindowsFreeSpace([string]$Path) {
        $script:spaceChecks += $Path
        return [pscustomobject]@{ Volume = $Path; AvailableBytes = 16GB }
    }

    $script:availableDistros = @()
    $script:commands = @()
    $selected = Install-WslDistribution
    Assert-True ($selected -eq 'GromacsConsole-Ubuntu-24.04') 'return the actual imported distro name'
    Assert-True ($script:downloads -eq 0) 'offline image needs no network download'
    $import = @($script:commands | Where-Object { $_[0] -eq '--import' })[0]
    Assert-True ($import -contains '--version' -and $import[-1] -eq '2') 'import directly into WSL2'
    Assert-True (@($script:commands | Where-Object { $_[0] -eq '--install' }).Count -eq 0) 'never fetch the GitHub distro catalogue'
    Assert-True (Test-Path $fixtureImage) 'retain user-owned offline image'

    $UbuntuImage = ''
    Install-UbuntuRootfs | Out-Null
    Assert-True ($script:downloads -eq 1 -and -not (Test-Path $script:downloadPath)) 'delete our downloaded image after successful import'
    $script:importCode = [Convert]::ToInt32('80370102', 16)
    $message = ''
    try { Install-UbuntuRootfs | Out-Null } catch { $message = $_.Exception.Message }
    Assert-True ($message -match 'virtualization') 'show virtualization failure without invoking the distro catalogue'
    Assert-True (-not (Test-Path $script:downloadPath)) 'clean temporary downloads on failed import'
    $script:importCode = 0

    $script:spec.sha256 = '0' * 64
    $script:commands = @()
    $message = ''
    try { Install-UbuntuRootfs -ImagePath $fixtureImage | Out-Null } catch { $message = $_.Exception.Message }
    Assert-True ($message -match 'SHA-256 mismatch') 'reject a corrupt or unapproved local image'
    Assert-True (@($script:commands | Where-Object { $_[0] -eq '--import' }).Count -eq 0) 'checksum failure must not import anything'
    $script:spec.sha256 = $fixtureHash

    $script:availableDistros = @('GromacsConsole-Ubuntu-24.04')
    $message = ''
    try { Install-UbuntuRootfs -ImagePath $fixtureImage | Out-Null } catch { $message = $_.Exception.Message }
    Assert-True ($message -match 'will not be overwritten') 'never overwrite a registered distro'
    $script:availableDistros = @()
    $storage = Join-Path $logDirectory '..\WSL\Ubuntu-24.04'
    Set-Content (Join-Path $storage 'existing.vhdx') 'preserve'
    $message = ''
    try { Install-UbuntuRootfs -ImagePath $fixtureImage | Out-Null } catch { $message = $_.Exception.Message }
    Assert-True ($message -match 'not empty') 'never overwrite existing unregistered VHD files'
    Remove-Item (Join-Path $storage 'existing.vhdx')

    $script:statusCode = 1
    $script:rebootNeeded = $true
    $script:commands = @()
    $message = ''
    try { Install-WslDistribution | Out-Null } catch { $message = $_.Exception.Message }
    Assert-True ($message -match 'restart' -and $script:elevations -eq 1) 'stop when WSL base features require a reboot'
    Assert-True (@($script:commands | Where-Object { $_[0] -eq '--import' }).Count -eq 0) 'do not import before required reboot'

    # Check the elevated entrypoint cannot accidentally register a distro for an admin.
    $helper = Get-Content -Raw (Join-Path $repository 'packaging/windows/WslPrerequisites.ps1')
    Assert-True ($helper -match "'--no-distribution'" -and $helper -notmatch "'--distribution'") 'elevated setup never installs a distribution'
    Write-Host 'WSL setup regression tests passed.'
} finally {
    Remove-Item -Recurse -Force $testDirectory
}
