param(
    [Parameter(Mandatory = $true)][string]$Installer,
    [string]$ReportFile = '',
    [ValidateRange(5, 60)][int]$TimeoutSeconds = 30,
    [switch]$RequireWindows
)

# Read the actual NSIS controls, not source text or PE version resources. This
# file is ASCII so Windows PowerShell 5's source-code encoding cannot mask the
# installer regression. Unicode expectations are explicit C# escape sequences.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($env:OS -ne 'Windows_NT') {
    if ($RequireWindows) { throw 'This check requires an actual Windows desktop session.' }
    Write-Host 'SKIP: Linux/non-Windows host; installer UI was not executed.'
    return
}

if (-not ('GromacsInstallerSmoke' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

public static class GromacsInstallerSmoke {
    public const string Welcome = "\u6b22\u8fce\u5b89\u88c5";
    public const string Workspace = "\u672c\u5730\u5206\u5b50\u52a8\u529b\u5b66\u5de5\u4f5c\u53f0";
    public const string Simplified = "\u7b80\u4f53";
    private delegate bool Enumerate(IntPtr window, IntPtr unused);
    [DllImport("user32.dll")] private static extern bool EnumWindows(Enumerate callback, IntPtr unused);
    [DllImport("user32.dll")] private static extern bool EnumChildWindows(IntPtr parent, Enumerate callback, IntPtr unused);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr window, out uint process);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr window);
    [DllImport("user32.dll", CharSet = CharSet.Unicode, ExactSpelling = true)] private static extern int GetClassNameW(IntPtr window, StringBuilder text, int count);
    [DllImport("user32.dll", EntryPoint = "SendMessageTimeoutW", CharSet = CharSet.Unicode, ExactSpelling = true, SetLastError = true)]
    private static extern IntPtr SendText(IntPtr window, uint message, UIntPtr wparam, StringBuilder text, uint flags, uint timeout, out UIntPtr result);
    [DllImport("user32.dll", EntryPoint = "SendMessageTimeoutW", SetLastError = true)]
    private static extern IntPtr SendNumber(IntPtr window, uint message, UIntPtr wparam, IntPtr lparam, uint flags, uint timeout, out UIntPtr result);
    [DllImport("user32.dll", SetLastError = true)] private static extern bool PostMessageW(IntPtr window, uint message, UIntPtr wparam, IntPtr lparam);

    private static bool Owned(IntPtr window, int process) {
        uint actual; GetWindowThreadProcessId(window, out actual);
        return actual == (uint)process;
    }
    private static void RequireOwned(IntPtr window, int process) {
        if (!Owned(window, process)) throw new InvalidOperationException("Refusing to inspect or control another process.");
    }
    public static IntPtr[] Windows(int process, IntPtr parent) {
        var found = new List<IntPtr>();
        Enumerate callback = (window, unused) => {
            if (Owned(window, process) && IsWindowVisible(window)) found.Add(window);
            return found.Count < 256;
        };
        if (parent == IntPtr.Zero) EnumWindows(callback, IntPtr.Zero);
        else { RequireOwned(parent, process); EnumChildWindows(parent, callback, IntPtr.Zero); }
        return found.ToArray();
    }
    public static string Class(IntPtr window, int process) {
        RequireOwned(window, process);
        var buffer = new StringBuilder(256); GetClassNameW(window, buffer, buffer.Capacity);
        return buffer.ToString();
    }
    public static string Text(IntPtr window, int process, uint message, int index) {
        RequireOwned(window, process);
        var buffer = new StringBuilder(8192); UIntPtr result;
        // WM_GETTEXT uses capacity; CB_GETLBTEXT uses a previously bounded index.
        uint argument = message == 0x000D ? (uint)buffer.Capacity : (uint)index;
        if (SendText(window, message, new UIntPtr(argument), buffer, 0x0002, 250, out result) == IntPtr.Zero)
            throw new InvalidOperationException("Timed out reading installer controls.");
        return buffer.ToString();
    }
    public static long Number(IntPtr window, int process, uint message, int argument) {
        RequireOwned(window, process); UIntPtr result;
        if (SendNumber(window, message, new UIntPtr((uint)argument), IntPtr.Zero, 0x0002, 250, out result) == IntPtr.Zero)
            throw new InvalidOperationException("Timed out communicating with installer controls.");
        return unchecked((long)result.ToUInt64());
    }
    public static void Close(IntPtr window, int process) {
        RequireOwned(window, process);
        PostMessageW(window, 0x0010, UIntPtr.Zero, IntPtr.Zero); // WM_CLOSE only.
    }
}
'@
}

$source = (Resolve-Path -LiteralPath $Installer).Path
if ([IO.Path]::GetExtension($source) -ne '.exe') { throw 'An actual generated installer EXE is required.' }
$work = Join-Path ([IO.Path]::GetTempPath()) ('gromacs-installer-smoke-' + [Guid]::NewGuid().ToString('N'))
$process = $null
$report = [ordered]@{ status = 'failed'; installer = [IO.Path]::GetFileName($source); language = ''; visibleTexts = @(); error = '' }
try {
    New-Item -ItemType Directory -Path $work | Out-Null
    $copy = Join-Path $work 'setup.exe'
    Copy-Item -LiteralPath $source -Destination $copy
    $process = Start-Process -FilePath $copy -WorkingDirectory $work -PassThru
    $launchedId = $process.Id
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $selected = $false
    $welcomeWindow = [IntPtr]::Zero
    while ([DateTime]::UtcNow -lt $deadline) {
        $process.Refresh()
        if ($process.HasExited) { throw "Installer exited before the welcome page (exit $($process.ExitCode))." }
        $texts = @()
        foreach ($window in [GromacsInstallerSmoke]::Windows($launchedId, [IntPtr]::Zero)) {
            $children = [GromacsInstallerSmoke]::Windows($launchedId, $window)
            if (-not $selected) {
                foreach ($control in $children) {
                    if ([GromacsInstallerSmoke]::Class($control, $launchedId) -ne 'ComboBox') { continue }
                    $count = [GromacsInstallerSmoke]::Number($control, $launchedId, 0x0146, 0) # CB_GETCOUNT
                    if ($count -lt 2 -or $count -gt 100) { throw 'Unexpected language selector.' }
                    for ($index = 0; $index -lt $count; $index++) {
                        $length = [GromacsInstallerSmoke]::Number($control, $launchedId, 0x0149, $index) # CB_GETLBTEXTLEN
                        if ($length -lt 0 -or $length -ge 8192) { throw 'Invalid language label length.' }
                        $label = [GromacsInstallerSmoke]::Text($control, $launchedId, 0x0148, $index) # CB_GETLBTEXT
                        if ($label.Contains([GromacsInstallerSmoke]::Simplified) -or $label -match '(?i)Simplified') {
                            [void][GromacsInstallerSmoke]::Number($control, $launchedId, 0x014E, $index) # CB_SETCURSEL
                            # IDOK is sent only to the language dialog, which was
                            # identified by its language combo. Never Next/Install.
                            [void][GromacsInstallerSmoke]::Number($window, $launchedId, 0x0111, 1) # WM_COMMAND / IDOK
                            $report.language = $label
                            $selected = $true
                            break
                        }
                    }
                    if (-not $selected) { throw 'Simplified Chinese is missing from the language selector.' }
                    break
                }
                if ($selected) { break } # Child handles just closed; enumerate again.
            } else {
                $pageTexts = @($children | ForEach-Object { [GromacsInstallerSmoke]::Text($_, $launchedId, 0x000D, 0) })
                $texts += $pageTexts
                $joined = $pageTexts -join "`n"
                if ($joined.Contains([GromacsInstallerSmoke]::Welcome) -and $joined.Contains([GromacsInstallerSmoke]::Workspace)) {
                    $welcomeWindow = $window
                    break
                }
            }
        }
        if ($texts.Count) { $report.visibleTexts = $texts }
        if ($welcomeWindow -ne [IntPtr]::Zero) { break }
        Start-Sleep -Milliseconds 100
    }
    if ($welcomeWindow -eq [IntPtr]::Zero) {
        throw "Simplified Chinese welcome text was not rendered correctly within $TimeoutSeconds seconds. Controls: $($report.visibleTexts -join ' | ')"
    }
    [GromacsInstallerSmoke]::Close($welcomeWindow, $launchedId)
    [void]$process.WaitForExit(1000)
    $report.status = 'passed'
    Write-Host 'Actual installer UI passed: Simplified Chinese welcome title and workspace text are intact. No installation was started.'
} catch {
    $report.error = $_.Exception.Message
    throw
} finally {
    if ($null -ne $process) {
        try {
            $process.Refresh()
            if (-not $process.HasExited) { $process.Kill(); [void]$process.WaitForExit(2000) }
        } catch {
            # Closing a welcome page may finish between Refresh and Kill.
            if (-not $process.HasExited) { throw }
        } finally { $process.Dispose() }
    }
    if ($ReportFile) {
        [IO.File]::WriteAllText([IO.Path]::GetFullPath($ReportFile), ($report | ConvertTo-Json -Depth 4), (New-Object Text.UTF8Encoding($false)))
    }
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}
