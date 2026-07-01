# neoxider.ps1 — one-word launcher for neoxider-cockpit (PowerShell).
#   neoxider             -> prints a short usage summary (no side effects)
#   neoxider gui [port]  -> opens the web GUI in your browser
#   neoxider help        -> full agent.sh command reference
#   neoxider <anything>  -> passed straight through to agent.sh (run/reply/log/doctor/...)
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Sk = Join-Path $Here "..\agent.sh"

if ($args.Count -eq 0) {
    # bare invocation used to silently auto-open the GUI in a browser -- surprising side
    # effect for a bare command name. Now it just prints a pointer; `gui` is explicit.
    Write-Host "neoxider — control room for AI coding subagents. Run 'neoxider gui' for the web dashboard, or 'neoxider help' for the full command reference."
    & bash $Sk help
} else {
    & bash $Sk @args
}
