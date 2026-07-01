# The `neoxider` command

`neoxider` with no arguments opens the neoxider-agents web GUI in your browser.
Any other argument is passed straight through to `agent.sh` — `neoxider run ...`,
`neoxider doctor`, `neoxider log -f <name>`, etc. all work exactly like
`bash agent.sh run ...`. It resolves its own location, so it works no matter where
you cloned the repo.

## Recommended: run the installer once (adds `bin/` to your `PATH`)

This directory contains three entry points for the same command — `neoxider` (bash
script), `neoxider.cmd` (cmd.exe/PowerShell wrapper), `neoxider.ps1` (PowerShell
wrapper) — so once `bin/` is on your `PATH`, typing bare `neoxider` works from
git-bash, `cmd.exe`, **and** PowerShell, with no per-shell setup.

These scripts only touch *your own* PATH, and only when *you* run them — nothing runs
itself automatically.

**Windows** (PowerShell):

```powershell
powershell -ExecutionPolicy Bypass -File .\bin\install.ps1
```

Uses `[Environment]::SetEnvironmentVariable(..., "User")`, not `setx` — `setx` has a
documented bug where it silently truncates an already-long `PATH`.

**macOS / Linux / git-bash**:

```bash
bash bin/install.sh
```

Appends a `PATH` export to `~/.bashrc` (or `~/.zshrc` if that's your shell).

Either way: open a **new** terminal window afterward, then verify with
`neoxider doctor` — it should print the engines/rate-limits table from any shell,
with no `bash` prefix needed. Both scripts are idempotent (safe to run more than once).

**Prefer to do it by hand instead?** Windows: Win+R → `sysdm.cpl` → Advanced →
Environment Variables → edit your user `Path` → add the full path to this `bin`
directory. macOS/Linux: add `export PATH="/path/to/neoxider-agents/bin:$PATH"` to your
shell rc file yourself.

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
