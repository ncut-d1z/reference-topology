param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$AppArguments
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

# TUNA is the default for this process only. Explicit user settings win.
if ([string]::IsNullOrWhiteSpace($env:PIP_INDEX_URL)) {
    $env:PIP_INDEX_URL = 'https://pypi.tuna.tsinghua.edu.cn/simple'
}

# Match pyproject.toml (Python >= 3.12), not a fixed minor version.
$VersionCheck = 'import sys; raise SystemExit(sys.version_info < (3, 12))'
$ImportCheck = 'import fastapi, uvicorn, pypdf, pdfminer, multipart'

function Test-PythonExecutable([string]$Executable) {
    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) { return $false }
    $OldPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $Executable -c $VersionCheck 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $OldPreference
    }
}

function Test-Dependencies([string]$Executable) {
    $OldPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $Executable -c $ImportCheck 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $OldPreference
    }
}

function Test-ReadyEnvironment([string]$Executable) {
    if (Test-Dependencies $Executable) { return $true }
    Write-Host "Installing dependencies for $Executable from $env:PIP_INDEX_URL"
    $OldPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $Executable -m pip install --index-url $env:PIP_INDEX_URL -e $PSScriptRoot 2>&1 | Out-Host
        $InstallSucceeded = ($LASTEXITCODE -eq 0)
    } catch {
        $InstallSucceeded = $false
    } finally {
        $ErrorActionPreference = $OldPreference
    }
    if ($InstallSucceeded -and (Test-Dependencies $Executable)) { return $true }
    Write-Host "Dependencies unavailable for $Executable; trying another installed Python."
    return $false
}

$PythonExe = $null
$ExistingEnvironments = @((Join-Path $PSScriptRoot '.venv'))
$ExistingEnvironments += @(Get-ChildItem -LiteralPath $PSScriptRoot -Directory -Filter '.venv-py*' -ErrorAction SilentlyContinue | Sort-Object Name | ForEach-Object { $_.FullName })
foreach ($Environment in $ExistingEnvironments) {
    $Candidate = Join-Path $Environment 'Scripts\python.exe'
    if ((Test-PythonExecutable $Candidate) -and (Test-ReadyEnvironment $Candidate)) {
        $PythonExe = $Candidate
        break
    }
}

if (-not $PythonExe) {
    # Find installed interpreters through the Windows py launcher and PATH.
    # A system interpreter is only used to create a private virtual environment.
    $Candidates = New-Object 'System.Collections.Generic.List[string]'
    if (-not [string]::IsNullOrWhiteSpace($env:REFERENCE_TOPOLOGY_PYTHON)) {
        [void]$Candidates.Add($env:REFERENCE_TOPOLOGY_PYTHON)
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $OldPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            $Listings = @(& py -0p 2>$null)
        } catch {
            $Listings = @()
        } finally {
            $ErrorActionPreference = $OldPreference
        }
        foreach ($Listing in $Listings) {
            $Match = [regex]::Match([string]$Listing, '(?i)([a-z]:\\[^\r\n]*\\python(?:3(?:\.\d+)?)?\.exe)\s*$')
            if ($Match.Success -and -not $Candidates.Contains($Match.Groups[1].Value)) {
                [void]$Candidates.Add($Match.Groups[1].Value)
            }
        }
    }
    foreach ($CommandName in @('python', 'python3')) {
        foreach ($CommandInfo in @(Get-Command $CommandName -All -ErrorAction SilentlyContinue)) {
            $Candidate = $CommandInfo.Source
            if ($Candidate -and -not $Candidates.Contains($Candidate)) {
                [void]$Candidates.Add($Candidate)
            }
        }
    }

    $AttemptedVersions = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($Interpreter in $Candidates) {
        if (-not (Test-PythonExecutable $Interpreter)) { continue }
        $OldPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            $VersionTag = (& $Interpreter -c 'import sys; print("%d%d" % sys.version_info[:2])' 2>$null | Out-String).Trim()
        } finally {
            $ErrorActionPreference = $OldPreference
        }
        if (-not $VersionTag -or -not $AttemptedVersions.Add($VersionTag)) { continue }

        $Environment = Join-Path $PSScriptRoot '.venv'
        if (Test-Path -LiteralPath $Environment) {
            $Environment = Join-Path $PSScriptRoot ('.venv-py' + $VersionTag)
            $Suffix = 2
            while (Test-Path -LiteralPath $Environment) {
                $Environment = Join-Path $PSScriptRoot ('.venv-py' + $VersionTag + '-' + $Suffix)
                $Suffix++
            }
        }
        Write-Host "Trying Python $VersionTag in $Environment (existing environments are retained)"
        $OldPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            & $Interpreter -m venv $Environment 2>&1 | Out-Host
            $Created = ($LASTEXITCODE -eq 0)
        } catch {
            $Created = $false
        } finally {
            $ErrorActionPreference = $OldPreference
        }
        if (-not $Created) { continue }
        $Candidate = Join-Path $Environment 'Scripts\python.exe'
        if ((Test-PythonExecutable $Candidate) -and (Test-ReadyEnvironment $Candidate)) {
            $PythonExe = $Candidate
            break
        }
    }
}

if (-not $PythonExe) {
    throw 'No usable Python >= 3.12 environment could be prepared. Check installed Python versions, dependency wheels and PIP_INDEX_URL. Existing environments were retained.'
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
