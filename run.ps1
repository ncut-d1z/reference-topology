param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$AppArguments
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$PythonExe = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    # Prefer the Windows Python launcher, then fall back to python on PATH.
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3.12 -m venv .venv
    } else {
        & python -m venv .venv
    }
    if ($LASTEXITCODE -ne 0) {
        throw '创建虚拟环境失败，请安装 Python 3.12 或更高版本。'
    }
}

# Keep the Python source separate from PowerShell's invocation syntax.
$DependencyProbe = 'import fastapi, uvicorn, pypdf, pdfminer, multipart'
& $PythonExe -c $DependencyProbe 2>$null
$DependenciesAvailable = ($LASTEXITCODE -eq 0)

if (-not $DependenciesAvailable) {
    & $PythonExe -m pip install -r (Join-Path $PSScriptRoot 'requirements.txt')
    if ($LASTEXITCODE -ne 0) {
        throw '依赖安装失败，请检查网络与 Python 版本。'
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
