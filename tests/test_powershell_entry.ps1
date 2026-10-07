# Actual core smoke test with Git Bash and WSL removed from PATH.
$ErrorActionPreference = 'Stop'
$entry = Join-Path $PSScriptRoot '..\bin\neoxider.ps1'
$savedPath = $env:PATH
$savedLogs = $env:AGENT_CLI_LOGS
try {
    $env:PATH = (($savedPath -split ';') | Where-Object {
        $_ -notmatch '(?i)git[\\/]bin|git[\\/]usr[\\/]bin|wsl'
    }) -join ';'
    $env:AGENT_CLI_LOGS = 'D:/Temp/agents-ux/powershell-entry/state'
    & $entry list
    if ($LASTEXITCODE -ne 0) { throw "Native list failed: $LASTEXITCODE" }
    & $entry reply --help
    if ($LASTEXITCODE -ne 0) { throw "Native help failed: $LASTEXITCODE" }
    Write-Output 'PASS: PowerShell entry requires Python; Git Bash and WSL are excluded.'
} finally {
    $env:PATH = $savedPath
    $env:AGENT_CLI_LOGS = $savedLogs
}
