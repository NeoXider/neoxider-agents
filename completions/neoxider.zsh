# Install: neoxider completion zsh --install
_neoxider() {
    local state_directory="${AGENT_CLI_LOGS:-$HOME/.claude/agent-cli-logs}" item
    local -a commands tasks models
    local previous="$words[CURRENT-1]" engine='' i
    commands=(run ask fan send reply stop restart peek watch top dashboard diff log last result pending wait status list clean prune doctor provider-info test-api gui openai-server completion config brief help)
    for ((i=2; i<CURRENT-1; i++)); do [[ "$words[i]" == -e || "$words[i]" == --engine ]] && engine="$words[i+1]"; done
    case "$engine" in
        codex) models=(6-sol 6-luna sol luna terra spark gpt-6.1-sol);;
        claude) models=(opus5 sonnet opus haiku sonnet55 opus55);;
        kimi) models=(k3 k3-256k coding highspeed);;
        opencode) models=(free spark muse muse-spark ox ox-alpha alpha pickle big-pickle hy3 mimo nemotron ultra lightning nemotron-fast);;
        gemini) models=(gemini-2.5-pro gemini-2.5-flash);;
        *) models=(6-sol 6-luna sol luna terra spark gpt-6.1-sol opus5 sonnet opus haiku sonnet55 opus55 k3 k3-256k coding highspeed free muse ox alpha pickle hy3 mimo nemotron ultra lightning gemini-2.5-pro gemini-2.5-flash);;
    esac
    if [[ "$previous" == -e || "$previous" == --engine || "$words[2]" == provider-info ]]; then
        compadd codex claude kimi opencode gemini
    elif [[ "$previous" == -m || "$previous" == --model ]]; then
        compadd -a models
    elif [[ "$previous" == --effort || "$previous" == -f && "$words[2]" != peek && "$words[2]" != watch && "$words[2]" != log ]]; then
        compadd minimal low medium high xhigh max
    elif [[ "$PREFIX" == -* ]]; then
        compadd -- -e -m -f -C -t -P -p -v -n -l --help --debug --engine --model --effort --dir --task --parent --prompt-file --timeout --poll --progress --no-progress --no-terse --verbose --terminal --log --owns --strict-owns --notify --now --flush --fresh --all-mine --all --purge --dry-run --strict --raw --deep --json --once --interval --stat --names --base-url --goal --out --outcome --not-touch --return --context-file --install --profile --lan --localhost --token --api-key
    elif (( CURRENT == 2 )); then
        _describe 'command' commands
    elif [[ "$words[2]" == completion ]]; then
        compadd powershell bash zsh
    elif [[ "$words[2]" == config ]]; then
        if (( CURRENT == 3 )); then compadd get set list; else compadd engine model effort; fi
    else
        for item in "$state_directory"/*.meta(N); do tasks+=("${item:t:r}"); done
        compadd -a tasks
        _files
    fi
}
compdef _neoxider neoxider agent.sh
