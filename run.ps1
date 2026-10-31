param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$AppArguments
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$PythonExe = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3.12 -m venv (Join-Path $PSScriptRoot '.venv')
    } else {
        & python -m venv (Join-Path $PSScriptRoot '.venv')
    }
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to create Python virtual environment. Python 3.12+ is required.'
    }
}

# Probe dependencies without converting native stderr into a terminating error.
$Probe = 'import fastapi, uvicorn, pypdf, pdfminer, multipart'
$OldPreference = $ErrorActionPreference
try {
    $ErrorActionPreference = 'Continue'
    & $PythonExe -c $Probe 2>$null | Out-Null
    $DependenciesAvailable = ($LASTEXITCODE -eq 0)
} finally {
    $ErrorActionPreference = $OldPreference
}

if (-not $DependenciesAvailable) {
    # Dependencies are declared in pyproject.toml, not requirements.txt.
    & $PythonExe -m pip install -e $PSScriptRoot
    if ($LASTEXITCODE -ne 0) {
        throw 'Dependency installation failed. Check your Python version and network.'
    }
}

$SourcePath = Join-Path $PSScriptRoot 'src'
$ExistingPythonPath = [Environment]::GetEnvironmentVariable('PYTHONPATH')
if ([string]::IsNullOrEmpty($ExistingPythonPath)) {
    $env:PYTHONPATH = $SourcePath
} else {
    $env:PYTHONPATH = $SourcePath + [IO.Path]::PathSeparator + $ExistingPythonPath
}

& $PythonExe -m reference_topology @AppArguments
exit $LASTEXITCODE
