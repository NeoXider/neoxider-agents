# Load: source <(neoxider completion bash)
_neoxider_complete() {
    local current="${COMP_WORDS[COMP_CWORD]}" state="${AGENT_CLI_LOGS:-$HOME/.claude/agent-cli-logs}" item
    local commands='run fan send reply stop restart peek log last result pending wait status list clean prune doctor provider-info test-api gui openai-server completion help'
    local flags='-e -m -f -C -t -P -p -n -l --help --prompt-file --timeout --poll --no-progress --no-terse --verbose --terminal --now --flush --fresh --all-mine --all --purge --dry-run --strict --raw --deep --json --base-url --goal --out --lan --localhost --token --api-key'
    if [[ "$current" == -* ]]; then
        COMPREPLY=($(compgen -W "$flags" -- "$current"))
    elif [[ $COMP_CWORD -eq 1 ]]; then
        COMPREPLY=($(compgen -W "$commands" -- "$current"))
    elif [[ "${COMP_WORDS[1]}" == completion ]]; then
        COMPREPLY=($(compgen -W 'powershell bash zsh' -- "$current"))
    else
        COMPREPLY=()
        for item in "$state"/*.meta; do
            [[ -f "$item" ]] || continue
            item="${item##*/}"; item="${item%.meta}"
            [[ "$item" == "$current"* ]] && COMPREPLY+=("$item")
        done
        [[ ${#COMPREPLY[@]} -gt 0 ]] || COMPREPLY=($(compgen -f -- "$current"))
    fi
}
complete -F _neoxider_complete neoxider agent.sh
