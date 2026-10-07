# Install: neoxider completion bash --install
_neoxider_complete() {
    local current="${COMP_WORDS[COMP_CWORD]}" state="${AGENT_CLI_LOGS:-$HOME/.claude/agent-cli-logs}" item
    local commands='run ask fan send reply stop restart peek watch top dashboard diff log last result pending wait status list clean prune doctor provider-info test-api gui openai-server completion config brief help'
    local flags='-e -m -f -C -t -P -p -v -n -l --help --debug --engine --model --effort --dir --task --parent --prompt-file --timeout --poll --progress --no-progress --no-terse --verbose --terminal --log --owns --strict-owns --notify --now --flush --fresh --all-mine --all --purge --dry-run --strict --raw --deep --json --once --interval --stat --names --base-url --goal --out --outcome --not-touch --return --context-file --install --profile --lan --localhost --token --api-key'
    local previous="${COMP_WORDS[COMP_CWORD-1]}" engine='' models='' i
    for ((i=1; i<COMP_CWORD-1; i++)); do
        [[ "${COMP_WORDS[i]}" == -e || "${COMP_WORDS[i]}" == --engine ]] && engine="${COMP_WORDS[i+1]}"
    done
    case "$engine" in
        codex) models='6-sol 6-luna sol luna terra spark gpt-6.1-sol';;
        claude) models='opus5 sonnet opus haiku sonnet55 opus55';;
        kimi) models='k3 k3-256k coding highspeed';;
        opencode) models='free spark muse muse-spark ox ox-alpha alpha pickle big-pickle hy3 mimo nemotron ultra lightning nemotron-fast';;
        gemini) models='gemini-2.5-pro gemini-2.5-flash';;
        *) models='6-sol 6-luna sol luna terra spark gpt-6.1-sol opus5 sonnet opus haiku sonnet55 opus55 k3 k3-256k coding highspeed free muse ox alpha pickle hy3 mimo nemotron ultra lightning gemini-2.5-pro gemini-2.5-flash';;
    esac
    if [[ "$previous" == -e || "$previous" == --engine || "${COMP_WORDS[1]}" == provider-info ]]; then
        COMPREPLY=($(compgen -W 'codex claude kimi opencode gemini' -- "$current"))
    elif [[ "$previous" == -m || "$previous" == --model ]]; then
        COMPREPLY=($(compgen -W "$models" -- "$current"))
    elif [[ "$previous" == --effort || "$previous" == -f && "${COMP_WORDS[1]}" != peek && "${COMP_WORDS[1]}" != watch && "${COMP_WORDS[1]}" != log ]]; then
        COMPREPLY=($(compgen -W 'minimal low medium high xhigh max' -- "$current"))
    elif [[ "$current" == -* ]]; then
        COMPREPLY=($(compgen -W "$flags" -- "$current"))
    elif [[ $COMP_CWORD -eq 1 ]]; then
        COMPREPLY=($(compgen -W "$commands" -- "$current"))
    elif [[ "${COMP_WORDS[1]}" == completion ]]; then
        COMPREPLY=($(compgen -W 'powershell bash zsh' -- "$current"))
    elif [[ "${COMP_WORDS[1]}" == config ]]; then
        if [[ $COMP_CWORD -eq 2 ]]; then COMPREPLY=($(compgen -W 'get set list' -- "$current")); else COMPREPLY=($(compgen -W 'engine model effort' -- "$current")); fi
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
