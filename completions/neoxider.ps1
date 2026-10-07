# Load: neoxider completion powershell | Out-String | Invoke-Expression
$neoxiderCommands = @('run','fan','send','reply','stop','restart','peek','log','last','result','pending','wait','status','list','clean','prune','doctor','provider-info','test-api','gui','openai-server','completion','help')
$neoxiderFlags = @('-e','-m','-f','-C','-t','-P','-p','-n','-l','--help','--prompt-file','--timeout','--poll','--no-progress','--no-terse','--verbose','--terminal','--now','--flush','--fresh','--all-mine','--all','--purge','--dry-run','--strict','--raw','--deep','--json','--base-url','--goal','--out','--lan','--localhost','--token','--api-key')
Register-ArgumentCompleter -Native -CommandName neoxider,neoxider.cmd,neoxider.ps1,agent.ps1,agent.cmd -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)
    $words = @($commandAst.CommandElements | ForEach-Object { $_.Extent.Text })
    $choices = @()
    if ($wordToComplete.StartsWith('-')) {
        $choices = $neoxiderFlags
    } elseif ($words.Count -le 2) {
        $choices = $neoxiderCommands
    } elseif ($words[1] -eq 'completion') {
        $choices = @('powershell','bash','zsh')
    } elseif ($words[1] -eq 'provider-info' -or ($words.Count -gt 2 -and $words[-2] -in @('-e','--engine'))) {
        $choices = @('codex','claude','kimi','opencode','gemini')
    } else {
        $stateDirectory = $env:AGENT_CLI_LOGS
        if (-not $stateDirectory) { $stateDirectory = Join-Path $HOME '.claude\agent-cli-logs' }
        $choices = @(Get-ChildItem -LiteralPath $stateDirectory -Filter '*.meta' -File -ErrorAction SilentlyContinue | ForEach-Object { $_.BaseName })
    }
    $choices | Where-Object { $_ -like "$wordToComplete*" } | ForEach-Object {
        [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)
    }
}
