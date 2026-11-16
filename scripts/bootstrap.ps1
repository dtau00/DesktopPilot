<#
.SYNOPSIS
    Prepare this machine for DesktopPilot: uv, the dev venv, a Word check.

.DESCRIPTION
    Idempotent. Installs uv through winget if it is missing, syncs the dev
    virtualenv (for working ON the tool), and reports whether Word is
    present, because the word plugin needs it. To just USE the tool, run
    scripts\install.ps1 instead.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot

function Write-Step($text) { Write-Host "==> $text" -ForegroundColor Cyan }

Write-Step 'Checking uv'
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host 'uv not found; installing via winget'
    winget install --id astral-sh.uv -e --accept-source-agreements --accept-package-agreements --disable-interactivity
    # winget edits PATH for new shells only, so pick it up for this one too.
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + [Environment]::GetEnvironmentVariable('Path', 'Machine')
}
uv --version

Write-Step 'Syncing the dev virtualenv'
Push-Location $repo
try {
    uv sync --extra dev
}
finally {
    Pop-Location
}

Write-Step 'Checking Microsoft Word'
$wordKey = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\winword.exe'
if (Test-Path $wordKey) {
    Write-Host ('Word: ' + (Get-ItemProperty $wordKey).'(default)')
}
else {
    Write-Warning 'WINWORD.EXE not registered - the word plugin cannot run here.'
}

Write-Step 'Done'
@'
Install on PATH:  powershell -File scripts\install.ps1     -> pilot
Tool tests:       uv run pytest -m "not word and not ui"
Lint:             uvx ruff check src tests
'@ | Write-Host
