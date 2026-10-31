param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$AppArguments
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

# Use Tsinghua TUNA by default; an explicit PIP_INDEX_URL takes precedence.
if ([string]::IsNullOrWhiteSpace($env:PIP_INDEX_URL)) {
    $env:PIP_INDEX_URL = 'https://pypi.tuna.tsinghua.edu.cn/simple'
}

function Test-Python312([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $false
    }
    try {
        $Version = & $Path -c 'import sys; print(".".join(map(str, sys.version_info[:2])))' 2>$null
        return ($LASTEXITCODE -eq 0 -and $Version -eq '3.12')
    } catch {
        return $false
    }
}

$DefaultPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$SeparatePython = Join-Path $PSScriptRoot '.venv-py312\Scripts\python.exe'

if (Test-Python312 $DefaultPython) {
    $PythonExe = $DefaultPython
} elseif (Test-Python312 $SeparatePython) {
    $PythonExe = $SeparatePython
} else {
    # Preserve existing incompatible environments, including incomplete ones.
    if (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.venv')) {
        $PythonExe = $SeparatePython
        $VenvPath = Join-Path $PSScriptRoot '.venv-py312'
        Write-Host 'Existing .venv is not Python 3.12; using .venv-py312 without modifying it.'
    } else {
        $PythonExe = $DefaultPython
        $VenvPath = Join-Path $PSScriptRoot '.venv'
    }

    $Created = $false
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3.12 -m venv $VenvPath
        $Created = ($LASTEXITCODE -eq 0)
    }
    if (-not $Created -and (Get-Command python3.12 -ErrorAction SilentlyContinue)) {
        & python3.12 -m venv $VenvPath
        $Created = ($LASTEXITCODE -eq 0)
    }
    if (-not $Created -and (Get-Command python -ErrorAction SilentlyContinue)) {
        $SystemVersion = & python -c 'import sys; print(".".join(map(str, sys.version_info[:2])))'
        if ($LASTEXITCODE -eq 0 -and $SystemVersion -eq '3.12') {
            & python -m venv $VenvPath
            $Created = ($LASTEXITCODE -eq 0)
        }
    }
    if (-not $Created -or -not (Test-Python312 $PythonExe)) {
        throw 'Python 3.12 is required. Install Python 3.12 (py -3.12) and retry. Existing virtual environments have not been deleted.'
    }
}

$DependencyProbe = 'import fastapi, uvicorn, pypdf, pdfminer, multipart'
$OldPreference = $ErrorActionPreference
try {
    $ErrorActionPreference = 'Continue'
    & $PythonExe -c $DependencyProbe 2>$null | Out-Null
    $DependenciesAvailable = ($LASTEXITCODE -eq 0)
} finally {
    $ErrorActionPreference = $OldPreference
}

if (-not $DependenciesAvailable) {
    Write-Host "Installing Python dependencies from $env:PIP_INDEX_URL"
    & $PythonExe -m pip install --index-url $env:PIP_INDEX_URL -e $PSScriptRoot
    if ($LASTEXITCODE -ne 0) {
        throw 'Dependency installation failed. Check Python version, TUNA mirror availability, or override PIP_INDEX_URL.'
    }
}

$ExistingPythonPath = [Environment]::GetEnvironmentVariable('PYTHONPATH')
$SourcePath = Join-Path $PSScriptRoot 'src'
if ([string]::IsNullOrEmpty($ExistingPythonPath)) {
    $env:PYTHONPATH = $SourcePath
} else {
    $env:PYTHONPATH = $SourcePath + [IO.Path]::PathSeparator + $ExistingPythonPath
}

& $PythonExe -m reference_topology @AppArguments
exit $LASTEXITCODE
