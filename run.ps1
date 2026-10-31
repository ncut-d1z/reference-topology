param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$AppArguments
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$PythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $PythonPath)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw '创建虚拟环境失败，请安装 Python 3.12 或更高版本。' }
}
& $PythonPath -c 'import reference_topology, fastapi, uvicorn, pypdf, pdfminer' 2>$null
if ($LASTEXITCODE -ne 0) {
    & $PythonPath -m pip install -e .
    if ($LASTEXITCODE -ne 0) { throw '依赖安装失败，请检查网络与 Python 版本。' }
}
& $PythonPath -m reference_topology @AppArguments
exit $LASTEXITCODE
