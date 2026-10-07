# Native Windows PowerShell 5.1 / PowerShell 7 entry point.
$ErrorActionPreference = 'Stop'
$entry = Join-Path $PSScriptRoot '..\agent.py'
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$global:OutputEncoding = $utf8
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:AGENT_LAUNCHER_PID = [string]$PID
$pythonCommand = $null
$pythonPrefix = @()
foreach ($candidate in @('py', 'python', 'python3')) {
    $found = Get-Command $candidate -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) {
        $pythonCommand = $found.Source
        if ($candidate -eq 'py') {
            try {
                $resolvedPython = & $pythonCommand -3 -c 'import sys; print(sys.executable)' 2>$null
                if ($LASTEXITCODE -ne 0 -or -not $resolvedPython -or -not (Test-Path -LiteralPath $resolvedPython -PathType Leaf)) {
                    $pythonCommand = $null
                    continue
                }
                $pythonCommand = [string]$resolvedPython
            } catch {
                $pythonCommand = $null
                continue
            }
        }
        break
    }
}
if (-not $pythonCommand) {
    [Console]::Error.WriteLine('neoxider: Python 3.8+ is required. Install Python from python.org and enable "Add Python to PATH", then open a new shell.')
    exit 127
}
# JSON over stdin preserves quotes/newlines and never puts tokens in a file or argv.
$launchArguments = @($args | ForEach-Object { [string]$_ })
$pipelineLines = @($input | ForEach-Object { [string]$_ })
if ($launchArguments -contains '-') {
    $promptInput = $pipelineLines -join "`n"
    if ($pipelineLines.Count -eq 0 -and [Console]::IsInputRedirected) {
        $promptInput = [Console]::In.ReadToEnd()
    }
    if ($pipelineLines.Count -eq 0 -and -not [Console]::IsInputRedirected) {
        [Console]::Error.WriteLine('neoxider: stdin prompt is empty; pipe text into neoxider - or use -p FILE.')
        exit 1
    }
    $argumentJson = ConvertTo-Json -InputObject @{ argv = $launchArguments; stdin = $promptInput } -Compress -Depth 3
} else {
    $argumentJson = ConvertTo-Json -InputObject $launchArguments -Compress
}
$argumentJson | & $pythonCommand @pythonPrefix $entry --argv-stdin
exit $LASTEXITCODE
