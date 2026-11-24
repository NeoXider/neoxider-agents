# OpenCode model/effort route

Verified on 2026-11-25 with the pinned local OpenCode CLI wrapper.

For OpenCode workers the model variant belongs inside the model id:

```powershell
& $SK run -t task -e opencode -m 'opencode/hy4-flash-free#high' -C D:/path 'prompt'
```

Do **not** add `-f high` when the model id already carries `#high`. The wrapper
translates a separate effort flag into `--variant`, which the pinned local
`opencode` CLI does not accept; the run then fails before provider activity with
`Unrecognized flag: --variant in command opencode run`. The same applies to
`ask`, `fan`, `probe` and `restart`: pass the variant in `-m`, not in `-f`.

A minimal live check:

```powershell
& $SK ask -t route-check -e opencode -m 'opencode/hy4-flash-free#high' `
  -C D:/path 'Reply exactly: HY4_HIGH_OK. No tools.'
```

`HY4_HIGH_OK` with exit 0 proves the route for that moment; it does not prove
remaining quota. Provider failures must be read from the task log before any
retry or fallback.
