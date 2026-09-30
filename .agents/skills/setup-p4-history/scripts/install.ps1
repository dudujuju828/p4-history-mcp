#requires -Version 5.1
[CmdletBinding(DefaultParameterSetName = 'Source')]
param(
    [Parameter(ParameterSetName = 'Bundle', Mandatory = $true)][string]$BundlePath,
    [Parameter(ParameterSetName = 'Source')][string]$SourcePath,
    [Parameter(Mandatory = $true)][string]$Workspace,
    [Parameter(Mandatory = $true)][string]$Client,
    [Parameter(Mandatory = $true)][string]$Port,
    [Parameter(Mandatory = $true)][string]$User,
    [ValidateSet('copilot', 'codex', 'claude-code')][string]$HostClient = 'copilot',
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'Programs\p4-history'),
    [string]$StateDirectory,
    [switch]$SkipHooks,
    [switch]$CheckOnly
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-Digest([string]$Text) {
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($algorithm.ComputeHash([Text.Encoding]::UTF8.GetBytes($Text)))).Replace('-', '').ToLowerInvariant()
    } finally { $algorithm.Dispose() }
}

function Get-FileDigest([string]$File) {
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($File)
    try {
        return ([BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace('-', '').ToLowerInvariant()
    } finally { $stream.Dispose(); $algorithm.Dispose() }
}

function ConvertTo-CanonicalObject($Value) {
    if ($Value -is [pscustomobject]) {
        $result = [ordered]@{}
        foreach ($property in ($Value.PSObject.Properties | Sort-Object Name)) {
            $result[$property.Name] = ConvertTo-CanonicalObject $property.Value
        }
        return $result
    }
    if ($Value -is [System.Collections.IList]) {
        return ,@($Value | ForEach-Object { ConvertTo-CanonicalObject $_ })
    }
    return $Value
}

function Get-Registration {
    if ($HostClient -eq 'claude-code') {
        # Claude's get command has no JSON mode and performs a live health check.
        # Read only the user-scope entry that its native add/remove commands own.
        if (-not (Test-Path -LiteralPath $config)) { return $null }
        $document = Get-Content -LiteralPath $config -Raw | ConvertFrom-Json
        if (-not $document.PSObject.Properties['mcpServers']) { return $null }
        $entry = $document.mcpServers.PSObject.Properties[$name]
        if (-not $entry) { return $null }
        return (ConvertTo-CanonicalObject $entry.Value | ConvertTo-Json -Depth 30 -Compress)
    }
    # A missing server is an expected nonzero result, including through codex.ps1.
    $savedPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & $hostCommand mcp get $name --json 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        # Clients may emit environment maps in a different key order on every call.
        $parsed = ($output -join "`n") | ConvertFrom-Json
        return (ConvertTo-CanonicalObject $parsed | ConvertTo-Json -Depth 30 -Compress)
    } finally { $ErrorActionPreference = $savedPreference }
}

function Assert-Bundle([string]$Directory) {
    $root = (Get-Item -LiteralPath $Directory).FullName.TrimEnd('\')
    $all = @(Get-Item -LiteralPath $root) + @(Get-ChildItem -LiteralPath $root -Recurse -Force)
    foreach ($item in $all) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Bundle contains a link: $($item.FullName)" }
    }
    $manifest = Get-Content -LiteralPath (Join-Path $root 'bundle.json') -Raw | ConvertFrom-Json
    if ($manifest.format -ne 1 -or $manifest.platform -ne 'windows-x64') { throw 'Unsupported bundle format or platform.' }
    $entries = @($manifest.files.PSObject.Properties)
    if ($entries.Count -eq 0) { throw 'Empty bundle manifest.' }
    foreach ($entry in $entries) {
        $relative = $entry.Name
        if ($relative -match '(^/|\\|:|(^|/)\.\.?(/|$))' -or $entry.Value -notmatch '^[0-9a-f]{64}$') { throw 'Invalid bundle manifest entry.' }
        $file = Join-Path $root $relative
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Missing bundle file: $relative" }
        if ((Get-FileDigest $file) -ne $entry.Value) { throw "Bundle hash mismatch: $relative" }
    }
    $actual = @($all | Where-Object { -not $_.PSIsContainer -and $_.FullName -ne (Join-Path $root 'bundle.json') })
    if ($actual.Count -ne $entries.Count) { throw 'Bundle contains unexpected files.' }
    if (-not $manifest.files.PSObject.Properties['p4-history.exe']) { throw 'Bundle has no executable.' }
    return $manifest
}

. (Join-Path $PSScriptRoot 'source-runtime.ps1')
if (-not $BundlePath -and -not $SourcePath) { $SourcePath = Join-Path $PSScriptRoot '../../../..' }
$hostCommand = if ($HostClient -eq 'claude-code') { 'claude' } else { $HostClient }
if ($BundlePath -and (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64')) {
    throw 'This bundle supports Windows x64 only.'
}
$root = (Get-Item -LiteralPath $Workspace).FullName
if ($root.Length -gt 3) { $root = $root.TrimEnd('\') }
if (-not (Test-Path -LiteralPath $root -PathType Container)) { throw 'Workspace must be a directory.' }
$installation = [IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
if ($installation -eq $root -or $installation.StartsWith($root.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Install outside the Perforce workspace.'
}
foreach ($required in @('p4', 'git', $hostCommand)) {
    if (-not (Get-Command $required -ErrorAction SilentlyContinue)) { throw "Required command is unavailable: $required" }
}
$sourceCommit = $null
if ($BundlePath) {
    $bundle = (Get-Item -LiteralPath $BundlePath).FullName
    $null = Assert-Bundle $bundle
    $bundleId = Get-FileDigest (Join-Path $bundle 'bundle.json')
    $destination = Join-Path $installation $bundleId.Substring(0, 24)
    $candidate = Join-Path $bundle 'p4-history.exe'
} else {
    $runtime = Initialize-SourceRuntime $SourcePath $installation
    $destination = $runtime.destination
    $candidate = $runtime.executable
    $sourceCommit = $runtime.commit
}
$connection = @('--workspace', $root, '--client', $Client, '--port', $Port, '--user', $User)
if ($StateDirectory) { $connection += @('--state-dir', [IO.Path]::GetFullPath($StateDirectory)) }
$doctorOutput = & $candidate doctor @connection
if ($LASTEXITCODE -ne 0) { throw 'Workspace verification failed; client registration was not changed.' }
$doctor = ($doctorOutput -join "`n") | ConvertFrom-Json
$identity = (Get-Digest ($root.ToLowerInvariant() + "`n" + $Client + "`n" + $Port + "`n" + $User)).Substring(0, 16)
$name = 'p4-history-' + $identity
if ($CheckOnly) {
    [pscustomobject]@{ server = $name; destination = $destination; workspace = $doctor.workspace; storage = $doctor.storage; checked = $true } | ConvertTo-Json -Depth 10
    return
}
if (-not $BundlePath) {
    $executable = $candidate
} elseif (Test-Path -LiteralPath $destination) {
    $null = Assert-Bundle $destination
    if ((Get-FileDigest (Join-Path $destination 'bundle.json')) -ne $bundleId) { throw 'Existing installation has a different manifest.' }
} else {
    $null = New-Item -ItemType Directory -Path $installation -Force
    $staging = Join-Path $installation ('staging-' + [guid]::NewGuid().ToString('N'))
    Copy-Item -LiteralPath $bundle -Destination $staging -Recurse
    $null = Assert-Bundle $staging
    Move-Item -LiteralPath $staging -Destination $destination
}
if ($BundlePath) { $executable = Join-Path $destination 'p4-history.exe' }
# Check the installed copy before pointing a client at it.
$null = & $executable doctor @connection
if ($LASTEXITCODE -ne 0) { throw 'Installed bundle verification failed; client registration was not changed.' }
$clientProfile = if ($HostClient -eq 'copilot') {
    if ($env:COPILOT_HOME) { $env:COPILOT_HOME } else { Join-Path ([Environment]::GetFolderPath('UserProfile')) '.copilot' }
} elseif ($HostClient -eq 'codex') {
    if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path ([Environment]::GetFolderPath('UserProfile')) '.codex' }
} else {
    if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { [Environment]::GetFolderPath('UserProfile') }
}
$configName = switch ($HostClient) { 'copilot' { 'mcp-config.json' }; 'codex' { 'config.toml' }; 'claude-code' { '.claude.json' } }
$config = Join-Path $clientProfile $configName
$null = New-Item -ItemType Directory -Path $clientProfile -Force
$receipts = Join-Path $installation 'registrations'
$null = New-Item -ItemType Directory -Path $receipts -Force
$receiptPath = Join-Path $receipts ((Get-Digest ($config + "`n" + $name)) + '.json')
$previous = Get-Registration
if ($previous) {
    if (-not (Test-Path -LiteralPath $receiptPath)) { throw "Existing server $name is not owned by this installer; registration was preserved." }
    $receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
    if ($receipt.entry_digest -ne (Get-Digest $previous)) { throw "Server $name was customised outside this installer; registration was preserved." }
}
$backup = $null
if (Test-Path -LiteralPath $config) {
    $backup = $config + '.p4-history-' + [guid]::NewGuid().ToString('N') + '.bak'
    Copy-Item -LiteralPath $config -Destination $backup
}
$registration = @('mcp', 'add', $name)
if ($HostClient -eq 'claude-code') { $registration = @('mcp', 'add', '--scope', 'user', '--transport', 'stdio', $name) }
# Preserve custom authentication-file locations, never credential contents or P4PASSWD.
foreach ($variable in @('P4CONFIG', 'P4ENVIRO', 'P4TICKETS', 'P4TRUST', 'P4CHARSET')) {
    $value = [Environment]::GetEnvironmentVariable($variable)
    if ($value) { $registration += @('--env', ($variable + '=' + $value)) }
}
$registration += @('--', $executable, 'serve') + $connection
$requestDigest = Get-Digest ($registration | ConvertTo-Json -Compress)
if (-not $previous -or $receipt.request_digest -ne $requestDigest) {
    try {
        # Copilot and Claude require remove/add. Ownership protects custom tool settings.
        if ($previous -and $HostClient -ne 'codex') {
            $removal = @('mcp', 'remove', $name)
            if ($HostClient -eq 'claude-code') { $removal += @('--scope', 'user') }
            & $hostCommand @removal
            if ($LASTEXITCODE -ne 0) { throw 'Could not replace the previous registration.' }
        }
        & $hostCommand @registration
        if ($LASTEXITCODE -ne 0) { throw 'Native MCP registration command failed.' }
        $registered = Get-Registration
        if (-not $registered) { throw 'Client did not return the newly registered server.' }
        [pscustomobject]@{ entry_digest = (Get-Digest $registered); request_digest = $requestDigest } |
            ConvertTo-Json | Set-Content -LiteralPath $receiptPath -Encoding UTF8
    } catch {
        if ($backup) { Copy-Item -LiteralPath $backup -Destination $config -Force }
        throw "MCP registration failed; previous configuration restored when available. Backup: $backup. $_"
    }
}
$hookResult = $null
if (-not $SkipHooks -and $HostClient -ne 'codex') {
    $hookOutput = & $executable install-hooks --host $HostClient --hook-executable $executable @connection
    if ($LASTEXITCODE -ne 0) {
        throw 'MCP registration succeeded, but hook registration failed. Existing custom hooks were preserved. Resolve the reported conflict or repeat with -SkipHooks.'
    }
    $hookResult = ($hookOutput -join "`n") | ConvertFrom-Json
}
[pscustomobject]@{ server = $name; executable = $executable; source_commit = $sourceCommit; storage = $doctor.storage; config = $config; backup = $backup; hooks = $hookResult; restart_client = $true } | ConvertTo-Json -Depth 10
