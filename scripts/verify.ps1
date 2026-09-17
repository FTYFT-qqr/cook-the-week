[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $scriptRoot
$oldLocation = Get-Location
$trackedEnv = @('PYTHONUTF8', 'PYTHONIOENCODING', 'STORAGE', 'SMOKE_STORAGE')
$oldEnv = @{}
foreach ($name in $trackedEnv) {
    $oldEnv[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}

function Restore-ProcessEnvironment {
    foreach ($name in $trackedEnv) {
        $value = $oldEnv[$name]
        if ($null -eq $value) {
            Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
        } else {
            Set-Item -LiteralPath "Env:$name" -Value $value
        }
    }
}

function Invoke-VerificationStep {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    Write-Host "`n[$Label]"
    & $pythonCommand @pythonPrefix @Arguments
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "$Label failed with exit code $exitCode"
    }
}

try {
    Push-Location $projectRoot

    function Test-PythonCandidate {
        param(
            [Parameter(Mandatory = $true)][string]$Command,
            [string[]]$Prefix = @()
        )
        & $Command @Prefix -c 'import pytest' 2>$null
        return $LASTEXITCODE -eq 0
    }

    $pythonCommand = $null
    [string[]]$pythonPrefix = @()
    $candidates = @()
    if ($env:RECIPE_PLANNER_PYTHON) {
        $candidates += $env:RECIPE_PLANNER_PYTHON
    }
    $candidates += (Join-Path $projectRoot '.venv\Scripts\python.exe')
    # This is the environment used by the project's handoff document; an override
    # via RECIPE_PLANNER_PYTHON keeps the script portable to another machine.
    $candidates += 'D:\conda\cook\recipe-planner\python.exe'
    $pythonOnPath = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -ne $pythonOnPath) {
        $candidates += $pythonOnPath.Source
    }

    foreach ($candidate in $candidates | Select-Object -Unique) {
        if ((Test-Path -LiteralPath $candidate) -and
            (Test-PythonCandidate -Command $candidate -Prefix @())) {
            $pythonCommand = $candidate
            break
        }
    }

    if ($null -eq $pythonCommand) {
        $pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
        if ($null -ne $pyLauncher) {
            # Keep the launcher argument separate for Windows PowerShell 5.1 parsing.
            [string[]]$launcherPrefix = @(([string][char]45 + '3'))
            if (Test-PythonCandidate -Command $pyLauncher.Source -Prefix $launcherPrefix) {
                $pythonCommand = $pyLauncher.Source
                $pythonPrefix = $launcherPrefix
            }
        }
    }

    if ($null -eq $pythonCommand) {
        throw 'No Python environment with pytest was found. Set RECIPE_PLANNER_PYTHON or activate the project environment.'
    }

    # Force UTF-8 so the Python checks cannot fail while printing non-ASCII output.
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'

    Invoke-VerificationStep 'tracked secret scan' @('scripts\scan_tracked_secrets.py')
    Invoke-VerificationStep 'pytest' @('-m', 'pytest', 'tests')
    Invoke-VerificationStep 'recommendation evaluation' @('scripts\evaluate_recommendations.py')
    Invoke-VerificationStep 'self_check (DB)' @('scripts\self_check.py')

    $env:STORAGE = 'json'
    Invoke-VerificationStep 'self_check (JSON)' @('scripts\self_check.py')
    Remove-Item -LiteralPath Env:STORAGE -ErrorAction SilentlyContinue

    $env:SMOKE_STORAGE = 'json'
    Invoke-VerificationStep 'smoke_app (JSON)' @('scripts\smoke_app.py')
    $env:SMOKE_STORAGE = 'db'
    Invoke-VerificationStep 'smoke_app (DB)' @('scripts\smoke_app.py')
    Remove-Item -LiteralPath Env:SMOKE_STORAGE -ErrorAction SilentlyContinue

    Invoke-VerificationStep 'smoke_api_app' @('scripts\smoke_api_app.py')
    Invoke-VerificationStep 'verify_migration' @('scripts\verify_migration.py')

    Write-Host "`nAll verification gates passed." -ForegroundColor Green
} finally {
    Restore-ProcessEnvironment
    if ((Get-Location).Path -ne $oldLocation.Path) {
        Set-Location $oldLocation
    }
}
