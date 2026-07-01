# The `neoxider` command

`neoxider` with no arguments opens the neoxider-agents web GUI in your browser.
Any other argument is passed straight through to `agent.sh` — `neoxider run ...`,
`neoxider doctor`, `neoxider log -f <name>`, etc. all work exactly like
`bash agent.sh run ...`. It resolves its own location, so it works no matter where
you cloned the repo.

## Recommended: add `bin/` to your `PATH` (once, works in every shell)

This directory contains three entry points for the same command — `neoxider` (bash
script), `neoxider.cmd` (cmd.exe/PowerShell wrapper), `neoxider.ps1` (PowerShell
wrapper) — so once `bin/` is on your `PATH`, typing bare `neoxider` works from
git-bash, `cmd.exe`, **and** PowerShell, with no per-shell setup.

**Windows** (any shell): Win+R → `sysdm.cpl` → Advanced → Environment Variables →
edit your user `Path` → add `C:\path\to\neoxider-agents\bin`. Open a new terminal.

**macOS/Linux**: add to your shell rc file (`~/.bashrc`, `~/.zshrc`, ...):

```bash
export PATH="/path/to/neoxider-agents/bin:$PATH"
```

Verify it worked: `neoxider doctor` should print the engines/rate-limits table from
any shell, without a `bash` prefix.

## Alternative: per-shell alias/function (no PATH edit)

If you'd rather not touch `PATH`, these do the same thing for one shell only:

**bash / git-bash** — add to `~/.bashrc` (or `~/.bash_profile`), then `source` it:

```bash
alias neoxider='bash /path/to/neoxider-agents/bin/neoxider'
```

**PowerShell** — add to your profile (`notepad $PROFILE`), then `. $PROFILE` or open
a new window:

```powershell
function neoxider { & bash "C:\path\to\neoxider-agents\bin\neoxider.ps1" @args }
```

**Plain `cmd.exe`** has no per-session profile/rc file to hook into — use the `PATH`
method above, or always invoke it by full path
(`C:\path\to\neoxider-agents\bin\neoxider.cmd doctor`).
