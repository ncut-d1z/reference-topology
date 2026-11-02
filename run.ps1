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

# PowerShell 5.1 turns native stderr into NativeCommandError even when using
# a redirection file. Use the .NET process APIs to keep stdout, stderr and
# exit code separate. In particular, warnings on stderr do not imply failure.
function Invoke-NativeLogged([string]$Executable, [string[]]$CommandArgs) {
    # ProcessStartInfo.ArgumentList is unavailable in Windows PowerShell 5.1.
    # Quote each argument using Windows command-line parsing rules.
    function Quote-WindowsArgument([string]$Value) {
        $Builder = New-Object System.Text.StringBuilder
        [void]$Builder.Append([char]34)
        $Backslashes = 0
        foreach ($Character in $Value.ToCharArray()) {
            if ($Character -eq [char]92) {
                $Backslashes++
                continue
            }
            if ($Character -eq [char]34) {
                [void]$Builder.Append(('\' * (2 * $Backslashes + 1)))
                [void]$Builder.Append([char]34)
            } else {
                [void]$Builder.Append(('\' * $Backslashes))
                [void]$Builder.Append($Character)
            }
            $Backslashes = 0
        }
        [void]$Builder.Append(('\' * (2 * $Backslashes)))
        [void]$Builder.Append([char]34)
        return $Builder.ToString()
    }

    $StartInfo = New-Object System.Diagnostics.ProcessStartInfo
    $StartInfo.FileName = $Executable
    $StartInfo.Arguments = ((@($CommandArgs) | ForEach-Object { Quote-WindowsArgument $_ }) -join ' ')
    $StartInfo.UseShellExecute = $false
    $StartInfo.RedirectStandardOutput = $true
    $StartInfo.RedirectStandardError = $true
    $StartInfo.CreateNoWindow = $true

    $Process = New-Object System.Diagnostics.Process
    $Process.StartInfo = $StartInfo
    try {
        if (-not $Process.Start()) { throw "Failed to start $Executable" }
        # Read both streams concurrently to avoid pipe-buffer deadlocks.
        $OutTask = $Process.StandardOutput.ReadToEndAsync()
        $ErrTask = $Process.StandardError.ReadToEndAsync()
        $Process.WaitForExit()
        $OutText = $OutTask.GetAwaiter().GetResult()
        $ErrText = $ErrTask.GetAwaiter().GetResult()
        if ($OutText) { Write-Host $OutText -NoNewline }
        if ($ErrText) { Write-Host $ErrText -NoNewline }
        return [int]$Process.ExitCode
    } finally {
        $Process.Dispose()
    }
}

function Test-ReadyEnvironment([string]$Executable) {
    if (Test-Dependencies $Executable) { return $true }
    Write-Host "Installing dependencies for $Executable from $env:PIP_INDEX_URL"
    try {
        $InstallExitCode = Invoke-NativeLogged $Executable @('-m', 'pip', 'install', '--disable-pip-version-check', '--index-url', $env:PIP_INDEX_URL, '-e', $PSScriptRoot)
        $InstallSucceeded = ($InstallExitCode -eq 0)
    } catch {
        Write-Host "Unable to run pip with $Executable : $($_.Exception.Message)"
        $InstallSucceeded = $false
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
    # Prefer python already selected on PATH (including an activated toolchain).
    foreach ($CommandName in @('python', 'python3')) {
        foreach ($CommandInfo in @(Get-Command $CommandName -All -ErrorAction SilentlyContinue)) {
            $Candidate = $CommandInfo.Path
            if (-not $Candidate) { $Candidate = $CommandInfo.Definition }
            if ($Candidate -and -not $Candidates.Contains($Candidate)) {
                [void]$Candidates.Add($Candidate)
            }
        }
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
    Write-Host ("Discovered Python executables: " + ($Candidates -join ', '))
    $AttemptedVersions = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($Interpreter in $Candidates) {
        if (-not (Test-PythonExecutable $Interpreter)) {
            Write-Host "Skipping incompatible Python candidate: $Interpreter"
            continue
        }
        $OldPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            $VersionTag = (& $Interpreter -c 'import sys; print(sys.version_info.major * 100 + sys.version_info.minor)' 2>$null | Out-String).Trim()
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
        try {
            $CreateExitCode = Invoke-NativeLogged $Interpreter @('-m', 'venv', $Environment)
            $Created = ($CreateExitCode -eq 0)
        } catch {
            Write-Host "Unable to create virtual environment: $($_.Exception.Message)"
            $Created = $false
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
