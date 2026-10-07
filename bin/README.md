# The `neoxider` command

Python 3.8+ is the only runtime dependency. Windows PowerShell 5.1/7 and cmd entries
invoke the core directly; Git Bash and WSL are unnecessary. `neoxider` without arguments
prints help, `neoxider gui [port]` opens the local dashboard, and other arguments select
core commands. The default GUI port is 8765 (`AGENT_GUI_PORT` overrides it).

## Install on Windows

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\bin\install.ps1
```

The installer adds this `bin/` directory to your user PATH with
`[Environment]::SetEnvironmentVariable`, preserving a long PATH. Open a new shell:

```powershell
neoxider doctor
neoxider run -t hello -C D:/Git/MyProject "Say hello"
neoxider send hello "Continue with a short example"
```

No registry/configuration changes happen before you explicitly run the installer.
Without installing, use `.\bin\neoxider.ps1`, `.\bin\neoxider.cmd`, `.\agent.ps1`
or `.\agent.cmd`. PowerShell serializes arguments as UTF-8 JSON over stdin, preserving
Cyrillic, embedded quotes, empty arguments and newlines while keeping tokens out of files.
All entries propagate the core exit code. Missing Python produces one error with the install fix.

## Install on Linux/macOS

```sh
sh bin/install.sh
```

This idempotently appends a PATH export to the startup file selected from `$SHELL`
(`~/.zshrc`, `~/.bashrc` or `~/.profile`). The entry is a POSIX shell shim; Bash 4 is only
needed when explicitly selecting the retained `AGENT_LEGACY=1` engine. Python is discovered
as `py -3`, `python`, or `python3` where applicable.

## Completion

Load in the current shell; add the same line to your profile to persist it:

```powershell
neoxider completion powershell | Out-String | Invoke-Expression
```

```bash
source <(neoxider completion bash)
```

```zsh
source <(neoxider completion zsh)
```

Completion covers commands, task names from `AGENT_CLI_LOGS`, and flags. It does not
launch providers. A one-shell PowerShell function also works without editing PATH:

```powershell
function neoxider { & "D:/path/neoxider-agents/bin/neoxider.ps1" @args }
```

## Migration

Existing task state remains in `AGENT_CLI_LOGS/<task>.meta/.log/.md/.inbox`. The old Bash
engine is retained under `legacy/` for one release; `AGENT_LEGACY=1 ./agent.sh ...` selects
it on POSIX/Git Bash. Native PowerShell and cmd always execute Python. Use a separate
checkout during migration rather than replacing a running legacy wrapper's files.
