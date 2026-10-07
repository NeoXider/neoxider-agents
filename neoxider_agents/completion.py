"""Shell-native completion scripts and idempotent profile installation."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUFFIXES = {"powershell": "ps1", "bash": "bash", "zsh": "zsh"}
START = "# >>> neoxider-agents completion >>>"
END = "# <<< neoxider-agents completion <<<"


def script(shell):
    return (ROOT / "completions" / ("neoxider." + SUFFIXES[shell])).read_text(encoding="utf-8-sig")


def profiles(shell):
    home = Path.home()
    if shell == "powershell":
        documents = home / "Documents"
        if os.name == "nt":
            # Resolve redirected Documents without spawning a visible helper.
            try:
                import winreg
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
                    documents = Path(os.path.expandvars(winreg.QueryValueEx(key, "Personal")[0]))
            except OSError:
                pass
        return [documents / "WindowsPowerShell/Microsoft.PowerShell_profile.ps1", documents / "PowerShell/Microsoft.PowerShell_profile.ps1"]
    return [home / (".zshrc" if shell == "zsh" else ".bashrc")]


def install(shell, profile=None):
    from .config import config_path
    from .state import atomic_write
    target = config_path().parent / ("completion." + SUFFIXES[shell])
    atomic_write(target, script(shell))
    path = target.as_posix()
    source = ". '%s'" % path.replace("'", "''") if shell == "powershell" else ". '%s'" % path.replace("'", "'\"'\"'")
    block = START + "\n" + source + "\n" + END
    for rc in [Path(profile).expanduser()] if profile else profiles(shell):
        try:
            text = rc.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            text = ""
        if START in text and END in text[text.index(START):]:
            begin = text.index(START)
            end = text.index(END, begin) + len(END)
            text = text[:begin] + block + text[end:]
        else:
            text = text.rstrip("\n") + ("\n\n" if text else "") + block + "\n"
        # BOM keeps non-ASCII profile paths readable in Windows PowerShell 5.1.
        atomic_write(rc, ("\ufeff" if shell == "powershell" else "") + text)
    print("Installed %s completion; open a new shell to activate." % shell)
    return 0


def complete(opts, args):
    if len(args) > 1:
        raise ValueError("completion: choose powershell, bash or zsh")
    shell = args[0] if args else ("powershell" if os.name == "nt" else "zsh" if os.environ.get("SHELL", "").endswith("zsh") else "bash")
    if shell not in SUFFIXES:
        raise ValueError("completion: choose powershell, bash or zsh")
    if opts.get("profile") and not opts.get("--install"):
        raise ValueError("completion: --profile requires --install")
    if opts.get("--install"):
        return install(shell, opts.get("profile"))
    print(script(shell), end="")
    return 0
