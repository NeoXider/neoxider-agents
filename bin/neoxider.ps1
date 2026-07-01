# neoxider.ps1 — one-word launcher for neoxider-cockpit (PowerShell).
#   neoxider            -> opens the web GUI in your browser
#   neoxider <anything>  -> passed straight through to agent.sh (run/reply/log/doctor/...)
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Sk = Join-Path $Here "..\agent.sh"

if ($args.Count -eq 0) {
    & bash $Sk gui
} else {
    & bash $Sk @args
}
