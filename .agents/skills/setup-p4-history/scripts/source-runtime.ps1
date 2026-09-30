# Dot-sourced by install.ps1. Owns source acquisition and the isolated Python runtime only.
function Get-SetupUv([string]$Installation) {
    $available = Get-Command uv -ErrorAction SilentlyContinue
    if ($available) { return $available.Source }
    $toolsDirectory = Join-Path $Installation 'tools\uv-0.12.5'
    $executable = Join-Path $toolsDirectory 'uv.exe'
    if (Test-Path -LiteralPath $executable -PathType Leaf) { return $executable }
    $null = New-Item -ItemType Directory -Path $toolsDirectory -Force
    $installer = Join-Path $toolsDirectory 'install.ps1'
    Invoke-WebRequest -UseBasicParsing -Uri 'https://astral.sh/uv/0.12.5/install.ps1' -OutFile $installer
    $previous = $env:UV_UNMANAGED_INSTALL
    try {
        # The official unattended mode avoids PATH, profile and updater changes.
        $env:UV_UNMANAGED_INSTALL = $toolsDirectory
        & $installer | Out-Host
        if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) { throw 'uv bootstrap did not produce uv.exe.' }
    } finally { $env:UV_UNMANAGED_INSTALL = $previous }
    return $executable
}

function Initialize-SourceRuntime([string]$SourceRoot, [string]$Installation) {
    $sourceRoot = (Get-Item -LiteralPath $SourceRoot).FullName
    $top = & git -C $sourceRoot rev-parse --show-toplevel
    if ($LASTEXITCODE -ne 0 -or [IO.Path]::GetFullPath($top) -ne $sourceRoot) {
        throw 'SourcePath must be the root of a p4-history Git checkout.'
    }
    $commit = & git -C $sourceRoot rev-parse --verify HEAD
    if ($LASTEXITCODE -ne 0 -or $commit -notmatch '^[0-9a-f]{40,64}$') { throw 'Cannot identify the source commit.' }
    $dirty = & git -C $sourceRoot status --porcelain --untracked-files=normal -- pyproject.toml uv.lock src
    if ($LASTEXITCODE -ne 0 -or $dirty) { throw 'Commit or preserve runtime source changes before installing; setup installs committed code.' }
    $destination = Join-Path $Installation ('source-' + $commit)
    $executable = Join-Path $destination 'runtime\Scripts\p4-history.exe'
    $receipt = Join-Path $destination 'installed.json'
    if (Test-Path -LiteralPath $receipt) {
        $installed = Get-Content -LiteralPath $receipt -Raw | ConvertFrom-Json
        if ($installed.commit -ne $commit -or -not (Test-Path -LiteralPath $executable -PathType Leaf)) {
            throw 'Source runtime is incomplete. Install to a new InstallRoot or repair this runtime.'
        }
        return [pscustomobject]@{ executable = $executable; destination = $destination; commit = $commit }
    }
    if (Test-Path -LiteralPath $destination) {
        throw "An incomplete source install exists at $destination. Preserve it for diagnosis and use a new InstallRoot to retry."
    }
    $uv = Get-SetupUv $Installation
    $null = New-Item -ItemType Directory -Path $destination -Force
    $archive = Join-Path $destination 'source.zip'
    & git -C $sourceRoot archive --format=zip --output=$archive $commit -- pyproject.toml uv.lock README.md src
    if ($LASTEXITCODE -ne 0) { throw 'Could not export the committed runtime source.' }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $source = Join-Path $destination 'source'
    [IO.Compression.ZipFile]::ExtractToDirectory($archive, $source)
    $previousEnvironment = $env:UV_PROJECT_ENVIRONMENT
    $previousPythonDirectory = $env:UV_PYTHON_INSTALL_DIR
    try {
        # Virtual environments contain absolute paths: create at their final location, never move.
        $env:UV_PROJECT_ENVIRONMENT = Join-Path $destination 'runtime'
        $env:UV_PYTHON_INSTALL_DIR = Join-Path $Installation 'python'
        & $uv sync --locked --no-default-groups --no-editable --python 3.12 --project $source | Out-Host
        if ($LASTEXITCODE -ne 0) { throw 'Installing the locked Python runtime failed; MCP registration was not changed.' }
    } finally {
        $env:UV_PROJECT_ENVIRONMENT = $previousEnvironment
        $env:UV_PYTHON_INSTALL_DIR = $previousPythonDirectory
    }
    if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) { throw 'The installed CLI is missing.' }
    [pscustomobject]@{ commit = $commit; executable = $executable } |
        ConvertTo-Json | Set-Content -LiteralPath $receipt -Encoding UTF8
    return [pscustomobject]@{ executable = $executable; destination = $destination; commit = $commit }
}
