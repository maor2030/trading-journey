# Starts the trading agent hidden, logging to %LOCALAPPDATA%\trading-journey.
# Used by the "trading-journey agent" scheduled task (see README, "Start on login").
$root = Split-Path $PSScriptRoot -Parent
$exe = Join-Path $root ".venv\Scripts\trading-journey.exe"
$data = Join-Path $env:LOCALAPPDATA "trading-journey"
New-Item -ItemType Directory -Force $data | Out-Null
# The native Claude installer puts claude.exe here without adding it to the user PATH.
$env:Path = (Join-Path $env:USERPROFILE ".local\bin") + ";" + $env:Path

if (Get-Process trading-journey -ErrorAction SilentlyContinue) {
    Write-Output "Agent already running"
    exit 0
}

$p = Start-Process -FilePath $exe -ArgumentList "agent" -WorkingDirectory $root -NoNewWindow -Wait -PassThru `
    -RedirectStandardOutput (Join-Path $data "runner.out.log") -RedirectStandardError (Join-Path $data "runner.err.log")
exit $p.ExitCode
