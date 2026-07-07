# neoxider.ps1 — one-word launcher for neoxider-cockpit (PowerShell).
#   neoxider             -> prints a short usage summary (no side effects)
#   neoxider gui [port]  -> opens the web GUI in your browser
#   neoxider help        -> full agent.sh command reference
#   neoxider <anything>  -> passed straight through to agent.sh (run/reply/log/doctor/...)
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Sk = Join-Path $Here "..\agent.sh"

# A bare `bash` can resolve to the WSL stub (C:\Windows\System32\bash.exe), which cannot
# run this MSYS script. Prefer Git Bash from its common install locations.
$bash = @(
    (Join-Path $env:ProgramFiles 'Git\usr\bin\bash.exe'),
    (Join-Path ${env:ProgramFiles(x86)} 'Git\usr\bin\bash.exe'),
    (Join-Path $env:LOCALAPPDATA 'Programs\Git\usr\bin\bash.exe')
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $bash) { $bash = "bash" }

if ($args.Count -eq 0) {
    # bare invocation used to silently auto-open the GUI in a browser -- surprising side
    # effect for a bare command name. Now it just prints a pointer; `gui` is explicit.
    Write-Host "neoxider — control room for AI coding subagents. Run 'neoxider gui' for the web dashboard, or 'neoxider help' for the full command reference."
    & $bash $Sk help
} else {
    & $bash $Sk @args
}
