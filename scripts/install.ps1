<#
.SYNOPSIS
    Install DesktopPilot on PATH as a uv tool: `pilot`.

.DESCRIPTION
    Idempotent - safe to re-run, and `--editable` means a later `git pull`
    needs no reinstall. Prints where the executable landed and runs
    `pilot apps` as a smoke check.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File C:\Projects\DesktopPilot\scripts\install.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$env:PYTHONIOENCODING = 'utf-8'   # the CLI writes UTF-8 itself; pytest and scripts that import the package need this

function Write-Step($text) { Write-Host "==> $text" -ForegroundColor Cyan }

Write-Step "Installing $repo as a uv tool"
uv tool install --editable $repo --force

$binDir = Join-Path $env:USERPROFILE '.local\bin'
if (([Environment]::GetEnvironmentVariable('Path', 'User') -split ';') -notcontains $binDir) {
    Write-Step 'Adding the uv tool bin directory to PATH'
    uv tool update-shell
}

# uv edits the User PATH for NEW shells only, so pick the change up here too.
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
            [Environment]::GetEnvironmentVariable('Path', 'User')

Write-Step 'Executable'
foreach ($exe in @('pilot')) {
    $found = Get-Command $exe -ErrorAction SilentlyContinue
    if ($found) { Write-Host ("{0,-10} {1}" -f $exe, $found.Source) }
    else { Write-Warning "$exe is not on PATH; run 'uv tool update-shell' and open a new shell." }
}

Write-Step 'Smoke check: pilot apps'
pilot apps

Write-Step 'Done'
@'
pilot <app> <group> <command>   e.g. pilot word ribbon tabs

If a NEW shell still cannot find it, run: uv tool update-shell
'@ | Write-Host
