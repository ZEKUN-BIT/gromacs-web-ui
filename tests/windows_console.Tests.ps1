# Run only the install coordinator, with a harmless substitute for environment setup.
$ErrorActionPreference = 'Stop'
$repository = Split-Path $PSScriptRoot -Parent
. (Join-Path $repository 'packaging/windows/WslSetup.ps1')
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $repository 'packaging/windows/Console.ps1'), [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'Console.ps1 has syntax errors.' }
$function = $ast.Find({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Install-Environment'
}, $true)
if (-not $function) { throw 'Install coordinator was not found.' }
$definition = $function.Extent.Text
Invoke-Expression $definition
foreach ($name in @('Invoke-Service', 'Invoke-PayloadServiceStop', 'Wait-LocalService')) {
    $node = $ast.Find({ param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name
    }, $true)
    if (-not $node) { throw "$name was not found." }
    Invoke-Expression $node.Extent.Text
}
$testDirectory = Join-Path ([IO.Path]::GetTempPath()) ('gromacs-setup-lock-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory $testDirectory | Out-Null
$WslLog = Join-Path $testDirectory 'setup.log'
$children = @()

function Assert-True($Condition, [string]$Message) {
    if (-not $Condition) { throw "Assertion failed: $Message" }
}

function Start-TestChild([string]$Script, [string[]]$Arguments) {
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = (Get-Process -Id $PID).Path
    $info.Arguments = (@('-NoProfile', '-File', $Script) + $Arguments | ForEach-Object { ConvertTo-NativeArgument $_ }) -join ' '
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $info
    [void]$process.Start()
    return $process
}

try {
    # Real local HTTP with a broken global proxy must still be reachable. The
    # server is our own child and exits immediately after one request.
    $serverScript = Join-Path $testDirectory 'local-http.ps1'
    $readyPath = Join-Path $testDirectory 'local-http.ready'
    Set-Content -Encoding ASCII $serverScript @'
param([string]$Ready)
$listener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, 0)
$listener.Start()
try {
    [IO.File]::WriteAllText($Ready, $listener.LocalEndpoint.Port.ToString())
    $accept = $listener.AcceptTcpClientAsync()
    if (-not $accept.Wait(10000)) { throw 'Test HTTP server timed out.' }
    $client = $accept.GetAwaiter().GetResult()
    try {
        $stream = $client.GetStream()
        $buffer = New-Object byte[] 4096
        [void]$stream.Read($buffer, 0, $buffer.Length)
        $bytes = [Text.Encoding]::ASCII.GetBytes("HTTP/1.1 200 OK`r`nContent-Length: 2`r`nConnection: close`r`n`r`nOK")
        $stream.Write($bytes, 0, $bytes.Length)
    } finally { $client.Dispose() }
} finally { $listener.Stop() }
'@
    $server = Start-TestChild $serverScript @('-Ready', $readyPath)
    $children += $server
    $deadline = (Get-Date).AddSeconds(10)
    while (-not (Test-Path $readyPath)) {
        Assert-True (-not $server.HasExited -and (Get-Date) -lt $deadline) 'local test server starts'
        Start-Sleep -Milliseconds 20
    }
    $oldProxy = [Net.WebRequest]::DefaultWebProxy
    try {
        [Net.WebRequest]::DefaultWebProxy = New-Object Net.WebProxy('http://127.0.0.1:1', $false)
        $url = 'http://127.0.0.1:' + (Get-Content -Raw $readyPath)
        Assert-True (Wait-LocalService $url -TimeoutSeconds 3) 'loopback readiness bypasses a configured broken proxy'
    } finally { [Net.WebRequest]::DefaultWebProxy = $oldProxy }
    Assert-True ($server.WaitForExit(5000) -and $server.ExitCode -eq 0) 'local HTTP child exits cleanly'
    $timer = [Diagnostics.Stopwatch]::StartNew()
    Assert-True (-not (Wait-LocalService $url -TimeoutSeconds 1)) 'closed local port remains unready'
    Assert-True ($timer.Elapsed.TotalSeconds -lt 2.5) 'HTTP polling obeys one total deadline rather than per-attempt timeout sum'
    $message = ''
    try { Wait-LocalService 'https://example.com/' | Out-Null } catch { $message = $_.Exception.Message }
    Assert-True ($message -match 'loopback HTTP') 'readiness only probes the local HTTP service'

    $worker = Join-Path $testDirectory 'holder.ps1'
    $workerBody = @'
param([string]$Name, [string]$Ready, [string]$Release, [string]$Finished, [string]$Mode)
$ErrorActionPreference = 'Stop'
'@
    $workerBody += "`n" + $definition + "`n" + @'
function Install-EnvironmentCore {
    [IO.File]::WriteAllText($Ready, 'locked')
    $deadline = (Get-Date).AddSeconds(10)
    while (-not (Test-Path $Release)) {
        if ((Get-Date) -ge $deadline) { throw 'Test holder timed out.' }
        Start-Sleep -Milliseconds 20
    }
    [IO.File]::WriteAllText($Finished, 'finished')
    if ($Mode -eq 'abandon') { [Environment]::Exit(0) }
}
Install-Environment -MutexName $Name
'@
    Set-Content -Encoding UTF8 $worker $workerBody
    foreach ($mode in @('normal', 'abandon')) {
        $name = 'GromacsConsole.Test.' + [Guid]::NewGuid().ToString('N')
        $ready = Join-Path $testDirectory ($mode + '.ready')
        $release = Join-Path $testDirectory ($mode + '.release')
        $finished = Join-Path $testDirectory ($mode + '.finished')
        $child = Start-TestChild $worker @('-Name', $name, '-Ready', $ready, '-Release', $release, '-Finished', $finished, '-Mode', $mode)
        $children += $child
        $deadline = (Get-Date).AddSeconds(10)
        while (-not (Test-Path $ready)) {
            Assert-True (-not $child.HasExited -and (Get-Date) -lt $deadline) 'real child acquires setup lock'
            Start-Sleep -Milliseconds 20
        }
        $script:entered = $false
        $script:waitObserved = $false
        function Write-Host([object]$Object) {
            if ([string]$Object -match 'Waiting up to 60 seconds') {
                Assert-True (-not $script:entered -and -not (Test-Path $finished)) 'duplicate setup does not enter while holder is active'
                $script:waitObserved = $true
                [IO.File]::WriteAllText($release, 'release')
            }
        }
        function Install-EnvironmentCore {
            Assert-True (Test-Path $finished) 'system setup runs only after prior setup leaves its body'
            $script:entered = $true
        }
        try { Install-Environment -MutexName $name } finally { Remove-Item Function:\Write-Host }
        Assert-True ($script:waitObserved -and $script:entered) 'normal and abandoned holder allow a safe retry'
        Assert-True ($child.WaitForExit(5000) -and $child.ExitCode -eq 0) 'holder exits successfully'
    }

    # A failure inside setup must release the coordinator, even while its caller lives.
    $name = 'GromacsConsole.Test.' + [Guid]::NewGuid().ToString('N')
    function Install-EnvironmentCore { throw 'fixture setup failure' }
    $message = ''
    try { Install-Environment -MutexName $name } catch { $message = $_.Exception.Message }
    Assert-True ($message -eq 'fixture setup failure') 'preserve original setup failure'
    $probe = Join-Path $testDirectory 'probe.ps1'
    Set-Content -Encoding ASCII $probe @'
param([string]$Name)
$mutex = New-Object Threading.Mutex($false, $Name)
try {
    if (-not $mutex.WaitOne(0)) { exit 17 }
    $mutex.ReleaseMutex()
} finally { $mutex.Dispose() }
exit 0
'@
    $child = Start-TestChild $probe @('-Name', $name)
    $children += $child
    Assert-True ($child.WaitForExit(5000) -and $child.ExitCode -eq 0) 'another process can acquire mutex after failure'

    # The real service wrapper consumes JSON only from stdout, while native WSL
    # proxy diagnostics remain in shared stderr/log handling.
    $Distro = 'Ubuntu Test'
    $LinuxRoot = '/home/gromacs-console/.local/share/gromacs-console'
    function Invoke-WslCommand([string[]]$Arguments, [int]$TimeoutSeconds) {
        $script:serviceArguments = $Arguments
        Assert-True ($TimeoutSeconds -ge 120) 'allow the bounded Linux startup probe and cleanup to finish before the host service deadline'
        return [pscustomobject]@{ ExitCode = 0; Lines = @('{"url":"http://127.0.0.1:8000"}', 'wsl: proxy warning');
            StdoutLines = @('{"url":"http://127.0.0.1:8000"}'); StderrLines = @('wsl: proxy warning') }
    }
    Assert-True ((Invoke-Service 'start').url -eq 'http://127.0.0.1:8000') 'parse service JSON without WSL stderr contamination'
    Assert-True ($script:serviceArguments -contains 'Ubuntu Test' -and $script:serviceArguments -contains 'gromacs-console') 'service command uses the common wrapper and unprivileged user'
    function Invoke-LinuxScript([string]$Name, [string]$User, [string]$Script, [int]$TimeoutSeconds) {
        Assert-True ($User -eq 'gromacs-console' -and $TimeoutSeconds -eq 120) 'pre-apt service guard runs as the app owner with a bounded timeout'
        Assert-True ($Script.Contains('python3 "$payload/desktop_service.py" stop --app-root') -and $Script.Contains('wslpath -u')) 'pre-apt guard uses payload code and system Python rather than a damaged installed helper or venv'
        Assert-True ($Script.Contains([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes('E:\\Console')))) 'payload guard carries the exact Windows path encoded without shell interpolation'
    }
    Invoke-PayloadServiceStop ([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes('E:\\Console')))

    # Load the exact core function from a fixture file so PSScriptRoot naturally
    # points at harmless payload fixtures; no real WSL or service is modified.
    $core = $ast.Find({ param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Install-EnvironmentCore'
    }, $true)
    $fixture = Join-Path $testDirectory 'core-fixture'
    New-Item -ItemType Directory $fixture | Out-Null
    [IO.File]::WriteAllText((Join-Path $fixture 'application.tar.gz'), 'fixture payload')
    [IO.File]::WriteAllText((Join-Path $fixture 'linux-system.sh'), 'system-stage')
    [IO.File]::WriteAllText((Join-Path $fixture 'linux-install.sh'), 'application-stage')
    $manifest = @{ payload_sha256 = (Get-FileHash -Algorithm SHA256 (Join-Path $fixture 'application.tar.gz')).Hash.ToLowerInvariant(); environment_sha256 = 'environment' }
    $manifest | ConvertTo-Json | Set-Content (Join-Path $fixture 'release.json')
    Set-Content (Join-Path $fixture 'functions.ps1') $core.Extent.Text
    . (Join-Path $fixture 'functions.ps1')
    function Get-WslDistributionSnapshot([string]$Name, [switch]$Refresh) {
        $script:snapshotCalls++
        return [pscustomobject]@{ Supported = $true; HasService = $true; HasApplicationRoot = $true;
            InstalledRelease = [pscustomobject]@{ environment_sha256 = 'environment' } }
    }
    function Assert-EnvironmentSpace([string]$Name, $Snapshot, [switch]$AppOnly) { $script:coreCalls += $(if ($AppOnly) { 'space-app' } else { 'space-full' }) }
    function Save-WslSettings([string]$Name) { $script:coreCalls += 'saved' }
    function Invoke-Service([string]$Command) { $script:coreCalls += ('service-' + $Command) }
    function Invoke-PayloadServiceStop([string]$EncodedPath) { $script:coreCalls += 'service-stop' }
    function Invoke-LinuxScript([string]$Name, [string]$User, [string]$Script, [int[]]$AllowExitCodes) {
        $stage = if ($User -eq 'root') { 'system' } elseif ($Script.Contains('export APP_ONLY=1')) { 'application-only' } else { 'full-application' }
        $script:coreCalls += $stage
        if ($User -ne 'root') { Assert-True ($Script.Contains("export SETUP_BACKEND='CPU'")) 'carry the detected backend disk budget into Linux setup' }
        if ($User -ne 'root' -and $script:expectedDownloadExport) {
            Assert-True ($Script.Contains($script:expectedDownloadExport)) 'explicitly forward the parsed Windows download timeout to the Linux GPU stage'
        }
        if ($stage -eq 'application-only' -and $script:repairFallback) { return [pscustomobject]@{ ExitCode = 76 } }
        if ($stage -eq 'application-only') { Assert-True ($AllowExitCodes -contains 76) 'only explicit invalid-readiness status allows full-repair fallback' }
    }
    $Action = 'Start'
    $script:repairFallback = $false
    $script:coreCalls = @()
    Install-EnvironmentCore
    Assert-True (($script:coreCalls -join '|') -eq 'space-app|application-only|saved') 'healthy application-only update leaves system packages and early service stop to the Linux commit path'
    $script:repairFallback = $true
    $script:coreCalls = @()
    Install-EnvironmentCore
    Assert-True (($script:coreCalls -join '|') -eq 'space-app|application-only|space-full|service-stop|system|full-application|saved') 'invalid app-only health falls back to full repair with service guard before apt'
    $Action = 'Install'
    $script:coreCalls = @()
    Install-EnvironmentCore
    Assert-True (($script:coreCalls -join '|') -eq 'space-full|service-stop|system|full-application|saved') 'explicit Configure Update retains full repair even with matching environment inputs'

    $oldDownloadTimeout = [Environment]::GetEnvironmentVariable('GROMACS_CONSOLE_DOWNLOAD_TIMEOUT_SECONDS')
    try {
        [Environment]::SetEnvironmentVariable('GROMACS_CONSOLE_DOWNLOAD_TIMEOUT_SECONDS', ' 1234 ')
        $script:expectedDownloadExport = "export GROMACS_CONSOLE_DOWNLOAD_TIMEOUT_SECONDS=1234`n"
        $script:coreCalls = @()
        Install-EnvironmentCore
        Assert-True ($script:coreCalls -contains 'full-application') 'a valid Windows download budget reaches full Linux installation'
        $script:expectedDownloadExport = $null
        [Environment]::SetEnvironmentVariable('GROMACS_CONSOLE_DOWNLOAD_TIMEOUT_SECONDS', '1; touch injected')
        $script:coreCalls = @()
        $probesBefore = $script:snapshotCalls
        $message = ''
        try { Install-EnvironmentCore } catch { $message = $_.Exception.Message }
        Assert-True ($message -match 'integer between 1 and 86400' -and $script:coreCalls.Count -eq 0 -and $script:snapshotCalls -eq $probesBefore) 'reject shell-containing download timeout before WSL probes, service stop or Linux installation'
    } finally {
        $script:expectedDownloadExport = $null
        [Environment]::SetEnvironmentVariable('GROMACS_CONSOLE_DOWNLOAD_TIMEOUT_SECONDS', $oldDownloadTimeout)
    }

    $startSwitch = $ast.Find({ param($node)
        $node -is [System.Management.Automation.Language.SwitchStatementAst] -and $node.Extent.Text -match '^switch\s*\(\$Action\)'
    }, $true)
    Set-Content (Join-Path $fixture 'start-switch.ps1') $startSwitch.Extent.Text
    @{ installation_sha256 = 'same' } | ConvertTo-Json | Set-Content (Join-Path $fixture 'release.json')
    $Action = 'Start'
    $snapshot = [pscustomobject]@{ PendingUpdate = $true; InstalledRelease = [pscustomobject]@{ installation_sha256 = 'same' } }
    $script:pendingRepairs = 0
    function Install-Environment { $script:pendingRepairs++ }
    function Invoke-Service([string]$Command) { return [pscustomobject]@{ url = 'http://127.0.0.1:8000' } }
    function Wait-LocalService([string]$Url) { return $true }
    function Start-Process([string]$FilePath) { }
    . (Join-Path $fixture 'start-switch.ps1')
    Assert-True ($script:pendingRepairs -eq 1) 'ordinary Start recovers an interrupted transaction even when the installed release already matches'
    $snapshot.PendingUpdate = $false
    . (Join-Path $fixture 'start-switch.ps1')
    Assert-True ($script:pendingRepairs -eq 1) 'ordinary matching healthy Start skips installation'
    Write-Host 'Windows install coordinator regression tests passed.'
} finally {
    foreach ($child in $children) {
        if (-not $child.HasExited) { $child.Kill(); $child.WaitForExit() }
        $child.Dispose()
    }
    Remove-Item -Recurse -Force $testDirectory
}
