# install.ps1 — one-time setup for the `neoxider` command.
# Run this yourself (this script does not run itself): it only touches YOUR OWN user
# PATH environment variable, and only after you execute it.
#
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
#
# Adds this bin/ directory to your user PATH (persisted via the registry, not `setx` —
# setx has a documented bug where it silently truncates PATH if it's already long).
# After this, `neoxider` works from a NEW cmd.exe, PowerShell, or git-bash window.

$binDir = $PSScriptRoot
$currentPath = [Environment]::GetEnvironmentVariable("Path", "User")

if ($currentPath -split ";" -contains $binDir) {
    Write-Host "Already on PATH: $binDir"
} else {
    $newPath = if ($currentPath) { "$currentPath;$binDir" } else { $binDir }
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    Write-Host "Added to your user PATH: $binDir"
    Write-Host "Open a NEW terminal window, then run: neoxider doctor"
}
