param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$AppArguments
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$PythonExe = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $PythonExe)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw '创建虚拟环境失败，请安装 Python 3.12 或更高版本。' }
}
& $PythonExe -c 'import fastapi, uvicorn, pypdf, pdfminer, multipart' 2>$null
if ($LASTEXITCODE -ne 0) {
    & $PythonExe -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw '依赖安装失败，请检查网络与 Python 版本。' }
}
$env:PYTHONPATH = (Join-Path $PSScriptRoot 'src') + [IO.Path]::PathSeparator + $env:PYTHONPATH
& $PythonExe -m reference_topology @AppArguments
exit $LASTEXITCODE
