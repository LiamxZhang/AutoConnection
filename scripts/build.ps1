[CmdletBinding()]
param(
    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$buildVenv = Join-Path $repoRoot ".venv-build"
$buildDir = Join-Path $repoRoot "build"
$distDir = Join-Path $repoRoot "dist"
$specPath = Join-Path $repoRoot "WorkNetConnector.spec"
$venvPython = Join-Path $buildVenv "Scripts\python.exe"
$pyinstaller = Join-Path $buildVenv "Scripts\pyinstaller.exe"
$pyinstallerArguments = @(
    "--noconfirm",
    "--clean",
    "--onefile",
    "--windowed",
    "--name",
    "WorkNetConnector",
    "--collect-all",
    "keyring",
    "--collect-all",
    "pystray",
    "--hidden-import",
    "PIL._tkinter_finder",
    "src/net_connector/__main__.py"
)
$allowedRemovalPaths = @($buildVenv, $buildDir, $distDir, $specPath)

function Assert-AllowedRemoval {
    param([Parameter(Mandatory = $true)][string]$Path)

    $candidate = [System.IO.Path]::GetFullPath($Path).TrimEnd("\", "/")
    $isAllowed = $false
    foreach ($allowedPath in $allowedRemovalPaths) {
        $allowed = [System.IO.Path]::GetFullPath($allowedPath).TrimEnd("\", "/")
        if ($candidate.Equals($allowed, [System.StringComparison]::OrdinalIgnoreCase)) {
            $isAllowed = $true
            break
        }
    }
    if (-not $isAllowed) {
        throw "Refusing to remove a path outside the build allowlist: $Path"
    }
}

function Remove-BuildPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    Assert-AllowedRemoval -Path $Path
    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    $item = Get-Item -LiteralPath $Path -Force
    $isReparsePoint = ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
    if ($item.PSIsContainer -and -not $isReparsePoint) {
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
    else {
        Remove-Item -LiteralPath $Path -Force
    }
}

function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [string[]]$CommandArguments = @()
    )

    & $Executable @CommandArguments
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "Native command failed with exit code ${exitCode}: $Executable"
    }
}

function Invoke-NativeCapture {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [string[]]$CommandArguments = @()
    )

    $output = & $Executable @CommandArguments
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "Native command failed with exit code ${exitCode}: $Executable"
    }
    return ($output -join [Environment]::NewLine)
}

function Test-Python312 {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [string[]]$PrefixArguments = @()
    )

    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
        return $false
    }
    $arguments = @($PrefixArguments) + @(
        "-c",
        "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)"
    )
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $Executable @arguments *> $null
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    return $exitCode -eq 0
}

function Find-Python312 {
    $launcher = Get-Command "py" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $launcher -and (Test-Python312 -Executable $launcher.Source -PrefixArguments @("-3.12"))) {
        return @($launcher.Source, "-3.12")
    }

    $python = Get-Command "python" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $python -and (Test-Python312 -Executable $python.Source)) {
        return @($python.Source)
    }
    throw "Python 3.12 is required to build WorkNetConnector."
}

if ($ValidateOnly) {
    Write-Output "Repository root: $repoRoot"
    Write-Output "PyInstaller arguments: $($pyinstallerArguments -join ' ')"
    return
}

Push-Location -LiteralPath $repoRoot
try {
    if ((Test-Path -LiteralPath $buildVenv) -and -not (Test-Python312 -Executable $venvPython)) {
        Remove-BuildPath -Path $buildVenv
    }

    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        $basePython = @(Find-Python312)
        $executable = $basePython[0]
        $arguments = @($basePython | Select-Object -Skip 1) + @("-m", "venv", $buildVenv)
        Invoke-Native -Executable $executable -CommandArguments $arguments
    }

    if (-not (Test-Python312 -Executable $venvPython)) {
        throw "The repository-local build environment is not a valid Python 3.12 environment."
    }

    $basePrefixOutput = Invoke-NativeCapture -Executable $venvPython -CommandArguments @(
        "-c",
        "import sys; print(sys.base_prefix)"
    )
    $basePrefix = ($basePrefixOutput -split "`r?`n")[-1].Trim()
    $tclLibrary = Join-Path $basePrefix "Library\lib\tcl8.6"
    $tkLibrary = Join-Path $basePrefix "Library\lib\tk8.6"
    if ((Test-Path -LiteralPath $tclLibrary -PathType Container) -and
        (Test-Path -LiteralPath $tkLibrary -PathType Container)) {
        $env:TCL_LIBRARY = $tclLibrary
        $env:TK_LIBRARY = $tkLibrary
    }

    Invoke-Native -Executable $venvPython -CommandArguments @("-m", "pip", "install", ".[dev]")
    Invoke-Native -Executable $venvPython -CommandArguments @("-m", "pytest")

    Remove-BuildPath -Path $buildDir
    Remove-BuildPath -Path $distDir
    Remove-BuildPath -Path $specPath

    if (-not (Test-Path -LiteralPath $pyinstaller -PathType Leaf)) {
        throw "PyInstaller was not installed in the repository-local build environment."
    }
    Invoke-Native -Executable $pyinstaller -CommandArguments $pyinstallerArguments
}
finally {
    Pop-Location
}
