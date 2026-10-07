# Load: source <(neoxider completion zsh)
_neoxider() {
    local state_directory="${AGENT_CLI_LOGS:-$HOME/.claude/agent-cli-logs}" item
    local -a commands tasks
    commands=(run fan send reply stop restart peek log last result pending wait status list clean prune doctor provider-info test-api gui openai-server completion help)
    if (( CURRENT == 2 )); then
        _describe 'command' commands
    elif [[ "$words[2]" == completion ]]; then
        compadd powershell bash zsh
    elif [[ "$PREFIX" == -* ]]; then
        compadd -- -e -m -f -C -t -P -p -n -l --help --prompt-file --timeout --poll --no-progress --no-terse --verbose --terminal --now --flush --fresh --all-mine --all --purge --dry-run --strict --raw --deep --json --base-url --goal --out --lan --localhost --token --api-key
    else
        for item in "$state_directory"/*.meta(N); do tasks+=("${item:t:r}"); done
        compadd -a tasks
        _files
    fi
}
compdef _neoxider neoxider agent.sh
