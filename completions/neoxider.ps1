# Install: neoxider completion powershell --install
$neoxiderCommands = @('run','ask','fan','send','reply','stop','restart','peek','watch','top','dashboard','diff','log','last','result','pending','wait','status','list','clean','prune','doctor','provider-info','test-api','gui','openai-server','completion','config','brief','help')
$neoxiderFlags = @('-e','-m','-f','-C','-t','-P','-p','-v','-n','-l','--help','--debug','--engine','--model','--effort','--dir','--task','--parent','--prompt-file','--timeout','--poll','--progress','--no-progress','--no-terse','--verbose','--terminal','--log','--owns','--strict-owns','--notify','--now','--flush','--fresh','--all-mine','--all','--purge','--dry-run','--strict','--raw','--deep','--json','--once','--interval','--stat','--names','--base-url','--goal','--out','--outcome','--not-touch','--return','--context-file','--install','--profile','--lan','--localhost','--token','--api-key')
$neoxiderModels = @{
    codex = @('6-sol','6-luna','sol','luna','terra','spark','gpt-6.1-sol')
    claude = @('opus5','sonnet','opus','haiku','sonnet55','opus55')
    kimi = @('k3','k3-256k','coding','highspeed')
    opencode = @('free','spark','muse','muse-spark','ox','ox-alpha','alpha','pickle','big-pickle','hy3','mimo','nemotron','ultra','lightning','nemotron-fast')
    gemini = @('gemini-2.5-pro','gemini-2.5-flash')
}
Register-ArgumentCompleter -Native -CommandName neoxider,neoxider.cmd,neoxider.ps1,agent.ps1,agent.cmd -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)
    $elements = @($commandAst.CommandElements | Where-Object { $_.Extent.StartOffset -lt $cursorPosition })
    $words = @($elements | ForEach-Object { $_.Extent.Text.Trim("'",'"') })
    $previous = if ($words.Count -gt 0 -and $elements[-1].Extent.EndOffset -lt $cursorPosition) { $words[-1] } elseif ($words.Count -gt 1) { $words[-2] } else { '' }
    $choices = @()
    if ($previous -in @('-e','--engine') -or ($words.Count -gt 1 -and $words[1] -eq 'provider-info')) {
        $choices = @('codex','claude','kimi','opencode','gemini')
    } elseif ($previous -in @('-m','--model')) {
        $engine = ''
        for ($i = 1; $i -lt $words.Count - 1; $i++) { if ($words[$i] -in @('-e','--engine')) { $engine = $words[$i + 1] } }
        if ($neoxiderModels.ContainsKey($engine)) { $choices = $neoxiderModels[$engine] } else { $choices = @($neoxiderModels.Values | ForEach-Object { $_ } | Sort-Object -Unique) }
    } elseif ($previous -in @('-f','--effort') -and ($words.Count -lt 2 -or $words[1] -notin @('peek','watch','log'))) {
        $choices = @('minimal','low','medium','high','xhigh','max')
    } elseif ($wordToComplete.StartsWith('-')) {
        $choices = $neoxiderFlags
    } elseif ($words.Count -le 1 -or ($words.Count -eq 2 -and $elements[-1].Extent.EndOffset -ge $cursorPosition)) {
        $choices = $neoxiderCommands
    } elseif ($words[1] -eq 'completion') {
        $choices = @('powershell','bash','zsh')
    } elseif ($words[1] -eq 'config') {
        $choices = if ($previous -in @('get','set')) { @('engine','model','effort') } else { @('get','set','list') }
    } else {
        $stateDirectory = $env:AGENT_CLI_LOGS
        if (-not $stateDirectory) { $stateDirectory = Join-Path $HOME '.claude\agent-cli-logs' }
        $choices = @(Get-ChildItem -LiteralPath $stateDirectory -Filter '*.meta' -File -ErrorAction SilentlyContinue | ForEach-Object { $_.BaseName })
    }
    $choices | Where-Object { $_ -like "$wordToComplete*" } | ForEach-Object {
        [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)
    }
}
