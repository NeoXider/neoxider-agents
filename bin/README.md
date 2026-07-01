# The `neoxider` command

`neoxider` with no arguments opens the neoxider-agents web GUI in your browser.
Any other argument is passed straight through to `agent.sh` — `neoxider run ...`,
`neoxider doctor`, `neoxider log -f <name>`, etc. all work exactly like
`bash agent.sh run ...`. It resolves its own location, so it works no matter where
you cloned the repo.

## bash / git-bash

```bash
alias neoxider='bash /path/to/neoxider-agents/bin/neoxider'
```

Add that line to `~/.bashrc` (or `~/.bash_profile`), then `source ~/.bashrc`.

## PowerShell

Add to your profile (`notepad $PROFILE`):

```powershell
function neoxider { & bash "C:\path\to\neoxider-agents\bin\neoxider.ps1" @args }
```

Reload with `. $PROFILE`, or just open a new PowerShell window.
