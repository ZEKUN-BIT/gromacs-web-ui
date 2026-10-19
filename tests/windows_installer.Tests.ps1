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
using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

public static class GromacsInstallerSmoke {
    public const string Welcome = "\u6b22\u8fce\u5b89\u88c5";
    public const string Workspace = "\u672c\u5730\u5206\u5b50\u52a8\u529b\u5b66\u5de5\u4f5c\u53f0";
    public const string Simplified = "\u7b80\u4f53";
    private static Stopwatch elapsed;
    private static int budgetMilliseconds;
    private delegate bool Enumerate(IntPtr window, IntPtr unused);
    [DllImport("user32.dll")] private static extern bool EnumWindows(Enumerate callback, IntPtr unused);
    [DllImport("user32.dll")] private static extern bool EnumChildWindows(IntPtr parent, Enumerate callback, IntPtr unused);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr window, out uint process);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr window);
    [DllImport("user32.dll")] private static extern IntPtr GetAncestor(IntPtr window, uint flags);
    [DllImport("user32.dll", CharSet = CharSet.Unicode, ExactSpelling = true)] private static extern int GetClassNameW(IntPtr window, StringBuilder text, int count);
    [DllImport("user32.dll", EntryPoint = "SendMessageTimeoutW", CharSet = CharSet.Unicode, ExactSpelling = true, SetLastError = true)]
    private static extern IntPtr SendText(IntPtr window, uint message, UIntPtr wparam, StringBuilder text, uint flags, uint timeout, out UIntPtr result);
    [DllImport("user32.dll", EntryPoint = "SendMessageTimeoutW", ExactSpelling = true, SetLastError = true)]
    private static extern IntPtr SendNumber(IntPtr window, uint message, UIntPtr wparam, IntPtr lparam, uint flags, uint timeout, out UIntPtr result);
    [DllImport("user32.dll", ExactSpelling = true, SetLastError = true)] private static extern bool PostMessageW(IntPtr window, uint message, UIntPtr wparam, IntPtr lparam);
    [DllImport("kernel32.dll", ExactSpelling = true, SetLastError = true)] private static extern void SetLastError(uint error);

    public static void BeginWait(int seconds) {
        budgetMilliseconds = checked(seconds * 1000);
        elapsed = Stopwatch.StartNew();
    }
    public static bool HasTime { get { return elapsed != null && elapsed.ElapsedMilliseconds < budgetMilliseconds; } }
    private static uint ReadTimeout() {
        long remaining = elapsed == null ? 0 : budgetMilliseconds - elapsed.ElapsedMilliseconds;
        if (remaining <= 0) throw new TimeoutException("Installer UI deadline expired.");
        return (uint)Math.Min(2000L, remaining);
    }
    private static Exception MessageFailure(string operation, IntPtr window, int process, uint message, uint argument, uint timeout, int error) {
        string reason = error == 0 ? "No Win32 error was reported; this does not establish a timeout." : new Win32Exception(error).Message;
        return new InvalidOperationException(String.Format(
            "{0} failed: HWND=0x{1:X}, PID={2}, message=0x{3:X4}, argument={4}, timeoutMs={5}, Win32Error={6}. {7}",
            operation, window.ToInt64(), process, message, argument, timeout, error, reason));
    }

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
        uint timeout = ReadTimeout();
        SetLastError(0);
        if (SendText(window, message, new UIntPtr(argument), buffer, 0x0002, timeout, out result) == IntPtr.Zero) {
            int error = Marshal.GetLastWin32Error();
            throw MessageFailure("SendMessageTimeoutW/text", window, process, message, argument, timeout, error);
        }
        return buffer.ToString();
    }
    public static long Number(IntPtr window, int process, uint message, int argument) {
        RequireOwned(window, process); UIntPtr result;
        uint timeout = ReadTimeout();
        SetLastError(0);
        if (SendNumber(window, message, new UIntPtr((uint)argument), IntPtr.Zero, 0x0002, timeout, out result) == IntPtr.Zero) {
            int error = Marshal.GetLastWin32Error();
            throw MessageFailure("SendMessageTimeoutW/number", window, process, message, (uint)argument, timeout, error);
        }
        return unchecked((long)result.ToUInt64());
    }
    private static void Post(IntPtr window, int process, uint message, uint argument) {
        RequireOwned(window, process);
        SetLastError(0);
        if (!PostMessageW(window, message, new UIntPtr(argument), IntPtr.Zero)) {
            int error = Marshal.GetLastWin32Error();
            throw MessageFailure("PostMessageW", window, process, message, argument, 0, error);
        }
    }
    public static void SelectLanguage(IntPtr dialog, IntPtr combo, int process, int index) {
        RequireOwned(dialog, process);
        RequireOwned(combo, process);
        if (index < 0 || Class(combo, process) != "ComboBox" || GetAncestor(combo, 2) != dialog)
            throw new InvalidOperationException("Refusing to accept a window that is not the identified language dialog.");
        // Mutations may repaint controls or destroy the dialog. Queue them so
        // they run on its UI thread without a synchronous cross-process wait.
        Post(combo, process, 0x014E, (uint)index); // CB_SETCURSEL
        while (Number(combo, process, 0x0147, 0) != index) { // CB_GETCURSEL
            Thread.Sleep((int)Math.Min(50U, ReadTimeout()));
        }
        // IDOK is limited to this verified language dialog; never Next/Install.
        Post(dialog, process, 0x0111, 1); // WM_COMMAND / IDOK
    }
    public static void Close(IntPtr window, int process) {
        Post(window, process, 0x0010, 0); // WM_CLOSE only.
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
    [GromacsInstallerSmoke]::BeginWait($TimeoutSeconds)
    $selected = $false
    $welcomeWindow = [IntPtr]::Zero
    while ([GromacsInstallerSmoke]::HasTime) {
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
                            [GromacsInstallerSmoke]::SelectLanguage($window, $control, $launchedId, $index)
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
