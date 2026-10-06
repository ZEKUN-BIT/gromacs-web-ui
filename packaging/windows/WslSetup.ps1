# Shared setup functions; dot-sourcing this file does not install or change anything.
. (Join-Path $PSScriptRoot 'PowerShellHost.ps1')

function Get-WslLineEncoding([byte[]]$Bytes, [int]$Start, [switch]$Final) {
    $remaining = $Bytes.Length - $Start
    if ($remaining -eq 0 -or (-not $Final -and $remaining -lt 2)) { return $null }
    if ($remaining -ge 2 -and $Bytes[$Start] -eq 255 -and $Bytes[$Start + 1] -eq 254) {
        return @{ Unicode = $true; Skip = 2 }
    }
    if ($remaining -ge 3 -and $Bytes[$Start] -eq 239 -and $Bytes[$Start + 1] -eq 187 -and $Bytes[$Start + 2] -eq 191) {
        return @{ Unicode = $false; Skip = 3 }
    }
    # Restrict the sample to this line: the following line may use another encoding.
    $end = [Math]::Min($Bytes.Length, $Start + 64)
    $delimiter = $false
    $unicodeDelimiter = $false
    $firstDelimiter = -1
    for ($index = $Start; $index -lt $end; $index++) {
        if ($Bytes[$index] -in @(10, 13)) {
            $delimiter = $true
            $firstDelimiter = $index
            $unicodeDelimiter = (($index - $Start) % 2 -eq 0 -and $index + 1 -lt $Bytes.Length -and $Bytes[$index + 1] -eq 0)
            $end = $index + 1 + [int]$unicodeDelimiter
            break
        }
    }
    # A Chinese UTF-16 character can have 0a/0d as its LOW byte (上/不).
    # Do not commit to a byte newline until its paired character is understood.
    if ($delimiter -and -not $unicodeDelimiter -and ($firstDelimiter - $Start) % 2 -eq 0 -and
        $firstDelimiter + 1 -lt $Bytes.Length -and $Bytes[$firstDelimiter + 1] -ge 52 -and $Bytes[$firstDelimiter + 1] -le 159) {
        $ambiguous = ($firstDelimiter - $Start -le 4)
        try { [void](New-Object Text.UTF8Encoding($false, $true)).GetString($Bytes, $Start, $firstDelimiter - $Start) }
        catch { $ambiguous = $true }
        # A short UTF-8 status/blank line can be followed by Checking/Verified.
        # A run of printable ASCII gives actual evidence for that next message;
        # one Latin byte alone could still be a Chinese UTF-16 high byte.
        $asciiEnd = $firstDelimiter + 1
        while ($asciiEnd -lt $Bytes.Length -and $Bytes[$asciiEnd] -ge 32 -and $Bytes[$asciiEnd] -le 126) { $asciiEnd++ }
        if ($asciiEnd - $firstDelimiter - 1 -ge 4) { $ambiguous = $false }
        if ($ambiguous) {
            for ($index = $Start; $index + 1 -lt $Bytes.Length; $index += 2) {
                if ($Bytes[$index] -in @(10, 13) -and $Bytes[$index + 1] -eq 0) { return @{ Unicode = $true; Skip = 0 } }
            }
            if (-not $Final) { return $null }
        }
    }
    if (-not $Final -and -not $delimiter -and $remaining -lt 64) { return $null }
    $oddZeros = 0
    for ($index = $Start + 1; $index -lt $end; $index += 2) {
        if ($Bytes[$index] -eq 0) { $oddZeros++ }
    }
    return @{ Unicode = ($unicodeDelimiter -or $oddZeros -gt (($end - $Start) / 10)); Skip = 0 }
}

function Read-WslOutputLines([hashtable]$State, [byte[]]$Bytes, [switch]$Final) {
    $State.Pending.Write($Bytes, 0, $Bytes.Length)
    $pending = $State.Pending.ToArray()
    $start = 0
    while ($start -lt $pending.Length) {
        if ($State.AfterCR) {
            $crWidth = $State.AfterCR
            if ($pending[$start] -eq 10 -and $start + $crWidth -gt $pending.Length -and -not $Final) { break }
            $State.AfterCR = 0
            if ($pending[$start] -eq 10 -and $start + $crWidth -le $pending.Length -and ($crWidth -eq 1 -or $pending[$start + 1] -eq 0)) {
                $start += $crWidth
                $State.ScanOffset = $start
                continue
            }
        }
        if (-not $State.Encoding) {
            $State.Encoding = Get-WslLineEncoding $pending $start -Final:$Final
            if (-not $State.Encoding) { break }
            $State.ScanOffset = $start + $State.Encoding.Skip
        }
        $width = 1 + [int]$State.Encoding.Unicode
        $scan = $State.ScanOffset
        $terminated = $false
        while ($scan + $width -le $pending.Length) {
            if ($pending[$scan] -in @(10, 13) -and ($width -eq 1 -or $pending[$scan + 1] -eq 0)) {
                $terminated = $true
                break
            }
            $scan += $width
        }
        $State.ScanOffset = $scan
        if (-not $terminated -and -not $Final) { break }
        $end = if ($terminated) { $scan } else { $pending.Length }
        $contentStart = $start + $State.Encoding.Skip
        $length = $end - $contentStart
        if ($State.Encoding.Unicode) {
            $text = [Text.Encoding]::Unicode.GetString($pending, $contentStart, $length)
        } else {
            try { $text = (New-Object Text.UTF8Encoding($false, $true)).GetString($pending, $contentStart, $length) }
            catch {
                # A tool's malformed or OEM-encoded line must never hide its exit code.
                $text = [Text.Encoding]::GetEncoding([Globalization.CultureInfo]::CurrentCulture.TextInfo.OEMCodePage).GetString($pending, $contentStart, $length)
            }
        }
        $State.Lines.Add($text)
        [pscustomobject]@{ Text = $text; Terminated = $terminated }
        if ($terminated -and $pending[$end] -eq 13) { $State.AfterCR = $width }
        $start = $end + $(if ($terminated) { $width } else { 0 })
        $State.Encoding = $null
        $State.ScanOffset = $start
    }
    if ($start -gt 0) {
        $State.Pending.SetLength(0)
        $State.Pending.Position = 0
        if ($start -lt $pending.Length) { $State.Pending.Write($pending, $start, $pending.Length - $start) }
        $State.ScanOffset -= $start
    }
}

function ConvertFrom-WslBytes([byte[]]$Bytes) {
    $state = @{ Pending = New-Object IO.MemoryStream; Encoding = $null; ScanOffset = 0; AfterCR = 0; Lines = New-Object 'Collections.Generic.List[string]' }
    $text = New-Object Text.StringBuilder
    try {
        foreach ($line in @(Read-WslOutputLines $state $Bytes -Final)) {
            [void]$text.Append($line.Text)
            if ($line.Terminated) { [void]$text.Append("`n") }
        }
        return $text.ToString()
    } finally { $state.Pending.Dispose() }
}

function ConvertTo-NativeArgument([string]$Argument) {
    # WSL parses raw switches before unquoting, so "--install" becomes a Linux command.
    # Use CommandLineToArgvW escaping only for values that actually require quoting.
    if ($Argument.Length -gt 0 -and $Argument -notmatch '[\s"]') { return $Argument }
    $escaped = [regex]::Replace($Argument, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Publish-WslOutput([hashtable]$State, [byte[]]$Bytes, [switch]$Final, [switch]$Display) {
    foreach ($line in @(Read-WslOutputLines $State $Bytes -Final:$Final)) {
        if ($Display -and $line.Text.Length -gt 0) {
            # Persist before display so the same phase is already visible in logs.
            Add-Content -Encoding UTF8 $WslLog $line.Text
            Write-Host $line.Text
        }
    }
}

function Get-WslTimeout([string]$Kind = 'PROBE') {
    $value = [Environment]::GetEnvironmentVariable('GROMACS_WSL_' + $Kind + '_TIMEOUT')
    $seconds = 0
    if ($value -and (-not [int]::TryParse($value, [ref]$seconds) -or $seconds -lt 1 -or $seconds -gt 86400)) {
        throw "GROMACS_WSL_${Kind}_TIMEOUT must be between 1 and 86400 seconds."
    }
    if ($value) { return $seconds }
    if ($Kind -eq 'SETUP') { return 7200 }
    return 60
}

function Get-LinuxDownloadTimeoutExport {
    $value = [Environment]::GetEnvironmentVariable('GROMACS_CONSOLE_DOWNLOAD_TIMEOUT_SECONDS')
    if (-not $value) { return '' }
    $seconds = 0
    if (-not [int]::TryParse($value, [ref]$seconds) -or $seconds -lt 1 -or $seconds -gt 86400) {
        throw 'GROMACS_CONSOLE_DOWNLOAD_TIMEOUT_SECONDS must be an integer between 1 and 86400 seconds.'
    }
    # WSL does not automatically inherit arbitrary Windows environment variables.
    # Pass only the parsed numeric value; never place raw environment text in Bash.
    return ('export GROMACS_CONSOLE_DOWNLOAD_TIMEOUT_SECONDS=' + $seconds.ToString([Globalization.CultureInfo]::InvariantCulture) + "`n")
}

function Invoke-WslCommand([string[]]$Arguments, [switch]$ShowOutput, [string]$InputText, [switch]$StreamOutput,
    [int]$TimeoutSeconds = (Get-WslTimeout)) {
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $Wsl
    $info.Arguments = ($Arguments | ForEach-Object { ConvertTo-NativeArgument $_ }) -join ' '
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.RedirectStandardInput = $PSBoundParameters.ContainsKey('InputText')
    Add-Content -Encoding UTF8 $WslLog ((Get-Date -Format s) + ' Executable: ' + $info.FileName)
    Add-Content -Encoding UTF8 $WslLog ('Arguments: ' + $info.Arguments)
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $info
    $streams = @()
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $timedOut = $false
    $started = $false
    try {
        [void]$process.Start()
        $started = $true
        foreach ($reader in @($process.StandardOutput, $process.StandardError)) {
            $buffer = New-Object byte[] 8192
            $streams += @{
                Stream = $reader.BaseStream; Buffer = $buffer; Done = $false
                Pending = New-Object IO.MemoryStream
                Encoding = $null; ScanOffset = 0; AfterCR = 0; Lines = New-Object 'Collections.Generic.List[string]'
                Task = $reader.BaseStream.ReadAsync($buffer, 0, $buffer.Length)
            }
        }
        $inputTask = $null
        $inputClosed = -not $info.RedirectStandardInput
        if ($info.RedirectStandardInput) {
            # Normalize exact UTF-8/LF bytes; asynchronous writing also lets us drain
            # both output pipes while a child is consuming a large stdin script.
            $inputBytes = [Text.Encoding]::UTF8.GetBytes($InputText.Replace("`r`n", "`n").Replace("`r", "`n").TrimStart([char]0xFEFF))
            $inputTask = $process.StandardInput.BaseStream.WriteAsync($inputBytes, 0, $inputBytes.Length)
        }
        $nextHeartbeat = (Get-Date).AddSeconds(15)
        while (-not $process.HasExited -or -not $streams[0].Done -or -not $streams[1].Done -or -not $inputClosed) {
            if ($timer.Elapsed.TotalSeconds -ge $TimeoutSeconds) {
                $timedOut = $true
                # Stop only the wsl.exe process created by this invocation. Setup
                # scripts also have their own Linux timeout process group below.
                if (-not $process.HasExited) { $process.Kill(); [void]$process.WaitForExit(5000) }
                break
            }
            foreach ($state in $streams) {
                if ($state.Done -or -not $state.Task.IsCompleted) { continue }
                $count = $state.Task.GetAwaiter().GetResult()
                if ($count -eq 0) {
                    $state.Done = $true
                    Publish-WslOutput $state ([byte[]]@()) -Final -Display:$StreamOutput
                    continue
                }
                # Only this thread accesses pending bytes; async readers own Buffer
                # until their task completes, then a new read starts after copying.
                $bytes = New-Object byte[] $count
                [Array]::Copy($state.Buffer, $bytes, $count)
                Publish-WslOutput $state $bytes -Display:$StreamOutput
                $state.Task = $state.Stream.ReadAsync($state.Buffer, 0, $state.Buffer.Length)
            }
            if (-not $inputClosed -and $inputTask.IsCompleted) {
                try { [void]$inputTask.GetAwaiter().GetResult() } catch [IO.IOException] {
                    Add-Content -Encoding UTF8 $WslLog 'Process closed stdin before consuming the entire script.'
                } finally { $process.StandardInput.Close(); $inputClosed = $true }
            }
            if ($ShowOutput -and (Get-Date) -ge $nextHeartbeat) {
                Write-Host 'WSL operation is still in progress...'
                $nextHeartbeat = (Get-Date).AddSeconds(15)
            }
            # An exited process can still have buffered pipe bytes. Drain them too.
            if ($process.HasExited) { Start-Sleep -Milliseconds 20 }
            else { [void]$process.WaitForExit(50) }
        }
        if ($timedOut) {
            foreach ($state in $streams) { Publish-WslOutput $state ([byte[]]@()) -Final -Display:$StreamOutput }
        }
        $code = if ($timedOut) { 124 } else { $process.ExitCode }
        $lines = @(@($streams[0].Lines) + @($streams[1].Lines) | Where-Object { $_.Length -gt 0 })
        if (-not $StreamOutput) {
            $lines | Add-Content -Encoding UTF8 $WslLog
            if ($ShowOutput) { $lines | ForEach-Object { Write-Host $_ } }
        }
        Add-Content -Encoding UTF8 $WslLog "Exit code: $code"
        if ($timedOut) {
            $message = "WSL command exceeded its $TimeoutSeconds-second deadline: $($info.Arguments). Only this launcher process was stopped. Diagnostic log: $WslLog"
            Add-Content -Encoding UTF8 $WslLog $message
            throw [TimeoutException]::new($message)
        }
        return [pscustomobject]@{ ExitCode = $code; Lines = $lines; StdoutLines = @($streams[0].Lines); StderrLines = @($streams[1].Lines) }
    } finally {
        if ($started -and -not $process.HasExited) { $process.Kill(); [void]$process.WaitForExit(5000) }
        foreach ($state in $streams) { $state.Stream.Dispose() }
        $process.Dispose()
        foreach ($state in $streams) { $state.Pending.Dispose() }
    }
}

function Invoke-LinuxScript([string]$Name, [string]$User, [string]$Script, [int[]]$AllowExitCodes = @(),
    [int]$TimeoutSeconds = (Get-WslTimeout 'SETUP')) {
    $seconds = $TimeoutSeconds
    # GNU timeout owns a new process group for this script, so its descendants
    # are bounded without shutting down WSL or terminating unrelated jobs.
    $result = Invoke-WslCommand @('--distribution', $Name, '--user', $User, '--exec',
        'timeout', '--signal=TERM', '--kill-after=10s', ($seconds.ToString() + 's'), 'bash', '-s') `
        -InputText $Script -ShowOutput -StreamOutput -TimeoutSeconds ($seconds + 30)
    if ($result.ExitCode -in $AllowExitCodes) { return $result }
    if ($result.ExitCode -in @(124, 137)) {
        throw "Linux setup exceeded its $seconds-second deadline in '$Name' as '$User'. Only this setup process group was stopped. Increase GROMACS_WSL_SETUP_TIMEOUT for a slow network. Diagnostic log: $WslLog"
    }
    if ($result.ExitCode -eq 75) {
        throw "Another Linux installation is still running in '$Name'. Wait for the original setup to finish, then run Configure/Update again. Diagnostic log: $WslLog"
    }
    if ($result.ExitCode -ne 0) {
        throw "Linux setup script failed in '$Name' as '$User' (exit code $($result.ExitCode)). See the output above and $WslLog."
    }
}

function Get-WslFailureMessage([int]$Code) {
    $unsigned = [BitConverter]::ToUInt32([BitConverter]::GetBytes($Code), 0)
    $hex = '0x{0:X8}' -f $unsigned
    $advice = switch ($hex) {
        '0x80072EE7' { 'The download server could not be resolved. Check DNS and Internet access.' }
        '0x80072EFD' { 'The download server could not be reached. Check Internet/proxy access.' }
        '0x80072EFE' { 'The download connection was interrupted. Check Internet/proxy access and try again.' }
        '0x80072EE2' { 'The download timed out. Check access to the download server; restarting Windows will not fix a blocked server.' }
        '0x80370102' { 'Enable hardware virtualization in BIOS/UEFI and Virtual Machine Platform in Windows.' }
        '0x80370114' { 'The WSL virtual machine service is unavailable. Check Virtual Machine Platform and virtualization.' }
        '0x8007019E' { 'Windows Subsystem for Linux is not enabled. Enable it, then restart Windows.' }
        '0x800701BC' { 'The WSL kernel needs updating. Run wsl --update.' }
        '0x80070005' { 'Windows denied access. Check administrator permission and device policy.' }
        '0x800704C7' { 'Administrator permission was cancelled. Run the shortcut again and accept the Windows prompt.' }
        default { 'See the WSL error text above and the setup log for the cause.' }
    }
    return "WSL failed with exit code $Code ($hex). $advice"
}

function Install-WslPrerequisites([switch]$WebDownload) {
    $logId = [Guid]::NewGuid().ToString('N')
    $helper = Join-Path $PSScriptRoot 'WslPrerequisites.ps1'
    $powershell = Get-PreferredPowerShellHost
    $arguments = @('-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $helper + '"'), '-LogId', $logId)
    if ($WebDownload) { $arguments += '-WebDownload' }
    Write-Host 'Installing WSL base components. Accept the Windows administrator prompt.'
    try {
        $process = Start-Process -FilePath $powershell -ArgumentList $arguments -Verb RunAs -Wait -PassThru
        $process.WaitForExit()
        $process.Refresh()
        $code = $process.ExitCode
    } catch {
        throw ('Could not start WSL setup with administrator permission. ' + $_.Exception.Message)
    }
    # ProgramData is independent of the administrator account used for the UAC prompt.
    $helperLog = Join-Path $env:ProgramData "GromacsConsole\SetupLogs\$logId.log"
    if (Test-Path $helperLog) {
        $text = Get-Content -Raw -Encoding UTF8 $helperLog
        Write-Host $text
        Add-Content -Encoding UTF8 $WslLog $text
    }
    Add-Content -Encoding UTF8 $WslLog "Elevated WSL setup exit code: $code"
    if ($null -eq $code) { throw 'Windows did not return a WSL setup exit code. Check the setup log.' }
    if ($code -in @(3010, 1641)) {
        throw 'WSL base components were installed; Windows requires a restart. Restart, then launch GROMACS Console again.'
    }
    if ($code -ne 0) { throw (Get-WslFailureMessage $code) }
}

function Get-UbuntuRootfsSpec {
    return (Get-Content -Raw (Join-Path $PSScriptRoot 'ubuntu-rootfs.json') | ConvertFrom-Json)
}

function Get-WslDistributions {
    if (-not (Test-Path $Wsl)) { return @() }
    $result = Invoke-WslCommand @('--list', '--quiet')
    if ($result.ExitCode -ne 0) { return @() }
    return @($result.Lines | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}

function Get-WslDistributionSnapshot([string]$Name, [switch]$Refresh) {
    if (-not $script:WslSnapshots) { $script:WslSnapshots = @{} }
    if (-not $Refresh -and $script:WslSnapshots.ContainsKey($Name)) { return $script:WslSnapshots[$Name] }
    $probe = @'
set -eu
. /etc/os-release
printf 'GC_ID=%s\nGC_VERSION=%s\nGC_ARCH=%s\n' "$ID" "$VERSION_ID" "$(uname -m)"
df -Pk /home | awk 'NR==2 {printf "GC_FREE_KB=%s\n", $4}'
if test -e /dev/dxg && test -e /usr/lib/wsl/lib/libcuda.so.1; then printf 'GC_GPU=1\n'; else printf 'GC_GPU=0\n'; fi
if test -f /home/gromacs-console/.local/share/gromacs-console/desktop_service.py; then
    printf 'GC_SERVICE=1\n'
else printf 'GC_SERVICE=0\n'; fi
if test -f /home/gromacs-console/.local/share/gromacs-console/.application-update.json; then
    printf 'GC_PENDING=1\n'
else printf 'GC_PENDING=0\n'; fi
if test -d /home/gromacs-console/.local/share/gromacs-console; then printf 'GC_APPROOT=1\n'; else printf 'GC_APPROOT=0\n'; fi
printf 'GC_RELEASE_BEGIN\n'
if test -f /home/gromacs-console/.local/share/gromacs-console/installed-release.json; then
    cat /home/gromacs-console/.local/share/gromacs-console/installed-release.json
fi
printf '\nGC_RELEASE_END\n'
'@
    $seconds = Get-WslTimeout
    $result = Invoke-WslCommand @('--distribution', $Name, '--user', 'root', '--exec', 'timeout', '--kill-after=2s',
        ($seconds.ToString() + 's'), 'bash', '-s') -InputText $probe -TimeoutSeconds ($seconds + 5)
    if ($result.ExitCode -in @(124, 137)) { throw "Linux environment check in '$Name' exceeded its $seconds-second deadline. Check $WslLog." }
    if ($result.ExitCode -ne 0) { return $null }
    $lines = if ($result.PSObject.Properties['StdoutLines']) { $result.StdoutLines } else { $result.Lines }
    $text = $lines -join "`n"
    $values = @{}
    foreach ($line in $lines) { if ($line -match '^GC_(ID|VERSION|ARCH|FREE_KB|GPU|SERVICE|PENDING|APPROOT)=(.*)$') { $values[$Matches[1]] = $Matches[2] } }
    $release = $null
    if ($text -match '(?s)GC_RELEASE_BEGIN\r?\n(.*?)\r?\nGC_RELEASE_END') {
        $json = $Matches[1].Trim()
        if ($json) {
            try { $release = $json | ConvertFrom-Json }
            catch { Add-Content -Encoding UTF8 $WslLog "Unreadable installed-release.json in '$Name'; full environment repair will be used." }
        }
    }
    $snapshot = [pscustomobject]@{
        Supported = ($values.ID -eq 'ubuntu' -and $values.VERSION -eq '24.04' -and $values.ARCH -eq 'x86_64')
        InstalledRelease = $release
        LinuxFreeBytes = if ($values.FREE_KB -match '^\d+$') { [long]$values.FREE_KB * 1024 } else { $null }
        GpuCandidate = ($values.GPU -eq '1')
        HasService = ($values.SERVICE -eq '1')
        PendingUpdate = ($values.PENDING -eq '1')
        HasApplicationRoot = ($values.APPROOT -eq '1')
    }
    $script:WslSnapshots[$Name] = $snapshot
    return $snapshot
}

function Test-SupportedDistro([string]$Name, [string[]]$Listing) {
    if ($Name -notmatch '^[A-Za-z0-9][A-Za-z0-9._ -]*$') { return $false }
    if (-not $PSBoundParameters.ContainsKey('Listing')) {
        $result = Invoke-WslCommand @('--list', '--verbose')
        if ($result.ExitCode -ne 0) { return $false }
        $Listing = $result.Lines
    }
    if (($Listing -join "`n") -notmatch ('(?m)^\s*\*?\s*' + [regex]::Escape($Name) + '\s+.+\s+2\s*$')) { return $false }
    $snapshot = Get-WslDistributionSnapshot $Name -Refresh
    return ($snapshot -and $snapshot.Supported)
}

function Find-CompatibleWslDistribution([string]$Preferred) {
    Write-Host 'Checking existing WSL version and Linux distributions before downloading anything...'
    if (-not (Test-Path $Wsl)) { return $null }
    $result = Invoke-WslCommand @('--list', '--verbose')
    if ($result.ExitCode -ne 0) { return $null }
    $candidates = if ($Preferred) { @($Preferred) } else {
        @($result.Lines | ForEach-Object { if ($_ -match '^\s*\*?\s*(.+?)\s+\S+\s+([12])\s*$') { $Matches[1] } })
    }
    foreach ($candidate in $candidates) {
        if (Test-SupportedDistro $candidate -Listing $result.Lines) {
            Write-Host "Reusing WSL2 Ubuntu 24.04 '$candidate'; skipping WSL and Ubuntu image downloads."
            return $candidate
        }
    }
    if ($Preferred) { throw "'$Preferred' must be Ubuntu 24.04 x86_64 running on WSL2. No download was started." }
    return $null
}

function Read-WslSettings([string]$Path = $StateFile) {
    foreach ($candidate in @($Path, ($Path + '.previous'))) {
        if (-not (Test-Path -LiteralPath $candidate)) { continue }
        try {
            $state = Get-Content -Raw -LiteralPath $candidate | ConvertFrom-Json
            if ($state.distribution -notmatch '^[A-Za-z0-9][A-Za-z0-9._ -]*$') { throw 'Invalid distribution name.' }
            if ($candidate -ne $Path) { Write-Host 'Recovered WSL settings from the last valid configuration.' }
            return $state
        } catch {
            $damaged = $candidate + '.damaged.' + [Guid]::NewGuid().ToString('N')
            Move-Item -LiteralPath $candidate -Destination $damaged
            Add-Content -Encoding UTF8 $WslLog "Damaged WSL settings preserved at $damaged"
            Write-Host "WSL settings were unreadable; the previous configuration or compatible installed distribution will be used. Saved copy: $damaged"
        }
    }
    return $null
}

function Save-WslSettings([string]$Name, [string]$Path = $StateFile) {
    $current = Read-WslSettings $Path
    if ($current -and $current.distribution -ceq $Name -and (Test-Path -LiteralPath $Path)) { return }
    $temporary = $Path + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    try {
        [IO.File]::WriteAllText($temporary, (@{ distribution = $Name } | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
        if (Test-Path -LiteralPath $Path) { [IO.File]::Replace($temporary, $Path, ($Path + '.previous')) }
        else { [IO.File]::Move($temporary, $Path) }
    } finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary } }
}

function Get-WindowsFreeSpace([string]$Path) {
    $resolved = [Environment]::ExpandEnvironmentVariables($Path)
    if ($resolved.StartsWith('\\?\')) { $resolved = $resolved.Substring(4) }
    if ($resolved.StartsWith('\??\')) { $resolved = $resolved.Substring(4) }
    if ($env:OS -eq 'Windows_NT') {
        if (-not ('GromacsConsole.NativeDisk' -as [type])) {
            Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
namespace GromacsConsole {
    public static class NativeDisk {
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool GetDiskFreeSpaceEx(string path, out ulong available, out ulong total, out ulong free);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool GetVolumePathName(string path, StringBuilder volume, uint size);
    }
}
'@
        }
        $existing = [IO.Path]::GetFullPath($resolved)
        while (-not (Test-Path -LiteralPath $existing)) {
            $parent = [IO.Path]::GetDirectoryName($existing)
            if (-not $parent -or $parent -eq $existing) { throw "Cannot locate a storage volume for $Path." }
            $existing = $parent
        }
        [System.UInt64]$available = 0; [System.UInt64]$total = 0; [System.UInt64]$free = 0
        if (-not [GromacsConsole.NativeDisk]::GetDiskFreeSpaceEx($existing, [ref]$available, [ref]$total, [ref]$free)) {
            throw "Cannot check free space at $existing (Windows error $([Runtime.InteropServices.Marshal]::GetLastWin32Error()))."
        }
        $volume = New-Object Text.StringBuilder 1024
        if (-not [GromacsConsole.NativeDisk]::GetVolumePathName($existing, $volume, 1024)) { [void]$volume.Append($existing) }
        return [pscustomobject]@{ Volume = $volume.ToString(); AvailableBytes = [long]$available }
    }
    $root = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($resolved))
    $drive = New-Object IO.DriveInfo($root)
    return [pscustomobject]@{ Volume = $root; AvailableBytes = $drive.AvailableFreeSpace }
}

function Get-WslBackingPath([string]$Name) {
    $registry = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss'
    if (-not (Test-Path $registry)) { return $null }
    foreach ($key in Get-ChildItem $registry) {
        $entry = Get-ItemProperty -LiteralPath $key.PSPath
        if ($entry.DistributionName -eq $Name) { return $entry.BasePath }
    }
    return $null
}

function Assert-WindowsSpace([string]$Path, [long]$RequiredBytes, [string]$Purpose) {
    $space = Get-WindowsFreeSpace $Path
    $message = '{0}: {1:N1} GiB available on {2}; need at least {3:N1} GiB.' -f $Purpose, ($space.AvailableBytes / 1GB), $space.Volume, ($RequiredBytes / 1GB)
    Write-Host $message
    Add-Content -Encoding UTF8 $WslLog $message
    if ($space.AvailableBytes -lt $RequiredBytes) { throw "Not enough disk space. $message Free space on that volume and retry." }
}

function Assert-EnvironmentSpace([string]$Name, $Snapshot, [switch]$AppOnly) {
    # Incremental free-space allowances include archives, unpacking and rollback.
    # CPU/GPU choice is finalized by the real CUDA probe in gpu_setup.py.
    $required = if ($AppOnly) { 256MB } elseif ($Snapshot.InstalledRelease) {
        if ($Snapshot.GpuCandidate) { 3GB } else { 2GB }
    } else { if ($Snapshot.GpuCandidate) { 4GB } else { 3GB } }
    $mode = if ($AppOnly) { 'Application update' } elseif ($Snapshot.InstalledRelease) { 'Environment repair/update' } else { 'First environment installation' }
    if (-not $AppOnly) { $mode += $(if ($Snapshot.GpuCandidate) { ' (GPU candidate)' } else { ' (CPU)' }) }
    Assert-WindowsSpace ([IO.Path]::GetTempPath()) 128MB 'Windows temporary files'
    $backing = Get-WslBackingPath $Name
    if ($backing) { Assert-WindowsSpace $backing $required ($mode + ' / WSL backing disk') }
    else { Write-Host "Could not locate the registered WSL backing disk for '$Name'; Linux free space is checked separately." }
    if ($null -eq $Snapshot.LinuxFreeBytes) { throw "Could not read Linux free space in '$Name'. Check the WSL setup log." }
    if ($Snapshot.LinuxFreeBytes -lt $required) {
        throw ('Not enough free space in the Linux filesystem: {0:N1} GiB available; {1:N1} GiB required for {2}.' -f ($Snapshot.LinuxFreeBytes / 1GB), ($required / 1GB), $mode)
    }
}

function Save-UbuntuRootfs([string]$Url, [string]$Destination) {
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if (-not $curl) { throw 'Windows curl.exe is missing. Download the official Ubuntu image on another computer and use -UbuntuImage.' }
    Write-Host 'Downloading the official Ubuntu WSL image (about 370 MB). Please wait...'
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $curl.Source --fail --location --progress-bar --retry 2 --connect-timeout 20 --max-time 1200 `
            --proto '=https' --proto-redir '=https' --tlsv1.2 $Url --output $Destination
        $code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previousPreference }
    if ($code -ne 0) {
        throw "Ubuntu image download failed (curl exit code $code). URL: $Url. Download it on another network and use -UbuntuImage to import the local file."
    }
}

function Install-UbuntuRootfs([string]$ImagePath = $UbuntuImage) {
    $name = 'GromacsConsole-Ubuntu-24.04'
    if ((Get-WslDistributions) -contains $name) { throw "'$name' already exists. Select the existing compatible distribution; it will not be overwritten." }
    $storage = [IO.Path]::GetFullPath((Join-Path $logDirectory '..\WSL\Ubuntu-24.04'))
    if ((Test-Path $storage) -and @(Get-ChildItem -Force $storage).Count -gt 0) {
        throw "WSL storage directory is not empty: $storage. It will not be overwritten. Check the setup log."
    }
    $spec = Get-UbuntuRootfsSpec
    $temporaryDirectory = $null
    try {
        # Ubuntu expands on its own storage volume, which can differ from TEMP.
        Assert-WindowsSpace $storage 2GB 'Fresh Ubuntu WSL import'
        if (-not $ImagePath) {
            Assert-WindowsSpace ([IO.Path]::GetTempPath()) 512MB 'Ubuntu image download / Windows TEMP'
            $temporaryDirectory = Join-Path ([IO.Path]::GetTempPath()) ('gromacs-ubuntu-' + [Guid]::NewGuid().ToString('N'))
            New-Item -ItemType Directory $temporaryDirectory | Out-Null
            $ImagePath = Join-Path $temporaryDirectory 'ubuntu-rootfs.tar.gz'
            Save-UbuntuRootfs $spec.url $ImagePath
        } else { $ImagePath = (Resolve-Path -LiteralPath $ImagePath).Path }
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $ImagePath).Hash.ToLowerInvariant() -ne $spec.sha256) {
            throw 'Ubuntu image SHA-256 mismatch. Nothing has been imported; download the specified official image again.'
        }
        Add-Content -Encoding UTF8 $WslLog ('Verified official Ubuntu image: ' + $spec.url + '; SHA-256: ' + $spec.sha256)
        New-Item -ItemType Directory -Force $storage | Out-Null
        Write-Host 'Importing verified Ubuntu directly into WSL2; no GitHub distribution list is needed.'
        $import = Invoke-WslCommand @('--import', $name, $storage, $ImagePath, '--version', '2') -ShowOutput -TimeoutSeconds (Get-WslTimeout 'SETUP')
        if ($import.ExitCode -ne 0) { throw (Get-WslFailureMessage $import.ExitCode) }
        return $name
    } finally {
        # Delete only our downloaded temporary copy; never delete a user's offline image or VHD.
        if ($temporaryDirectory) { Remove-Item -LiteralPath $temporaryDirectory -Recurse -Force }
    }
}

function Install-WslDistribution([switch]$SkipExistingCheck) {
    if (-not $SkipExistingCheck) {
        $existing = Find-CompatibleWslDistribution
        if ($existing) { return $existing }
    }
    $status = Invoke-WslCommand @('--status')
    if ($status.ExitCode -ne 0) {
        $help = Invoke-WslCommand @('--help')
        Install-WslPrerequisites -WebDownload:(($help.Lines -join "`n") -match '--web-download')
    }
    # wsl --install -d needs DistributionInfo.json on GitHub even with --web-download.
    # Direct import bypasses that catalogue and registers under the original Windows user.
    return (Install-UbuntuRootfs)
}
