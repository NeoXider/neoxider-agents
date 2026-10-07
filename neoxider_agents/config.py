"""Small opt-in user configuration; reading defaults never writes a config file."""
import json
import os
from pathlib import Path

KEYS = ("engine", "model", "effort")


def config_path():
    if os.environ.get("AGENT_CONFIG"):
        return Path(os.environ["AGENT_CONFIG"]).expanduser()
    if os.name == "nt" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "neoxider-agents/config.json"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "neoxider-agents/config.json"


def read():
    path = config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise ValueError("cannot read config %s; fix its JSON or set AGENT_CONFIG to another file" % path) from error
    if not isinstance(data, dict) or any(key not in KEYS or not isinstance(value, str) or "\n" in value or "\r" in value for key, value in data.items()):
        raise ValueError("invalid config %s; use string keys engine, model and effort" % path)
    return data


def installed_engine():
    # Same executable detection as doctor, without subprocess/version/auth probes.
    from .providers import ENGINES, executable
    found = []
    for engine in ENGINES:
        try:
            executable(engine)
            found.append(engine)
        except (OSError, ValueError):
            pass
    if "claude" in found:
        return "claude"
    if found:
        return found[0]
    raise ValueError("no agent CLI found; install codex, claude, kimi, opencode or gemini, then use neoxider config set engine ENGINE")


def launch_options(opts):
    result = dict(opts)
    data = read()
    configured_engine = os.environ.get("AGENT_ENGINE") or data.get("engine")
    engine = result.get("engine") or configured_engine or installed_engine()
    result["engine"] = engine
    use_defaults = not configured_engine or configured_engine == engine
    for key in ("model", "effort"):
        if key not in result and use_defaults:
            result[key] = os.environ.get("AGENT_" + key.upper()) or data.get(key, "")
    result.setdefault("dir", os.getcwd())
    return result


def command(args):
    if not args or args[0] not in ("get", "set", "list"):
        raise ValueError("config: use neoxider config get KEY, set KEY VALUE, or list")
    action = args[0]
    if action == "list":
        if len(args) != 1:
            raise ValueError("config list: remove extra arguments")
        print(json.dumps(read(), ensure_ascii=False, indent=2))
        return 0
    if len(args) != (3 if action == "set" else 2):
        raise ValueError("config %s: use neoxider config %s KEY%s" % (action, action, " VALUE" if action == "set" else ""))
    key = args[1]
    if key not in KEYS:
        raise ValueError("unknown config key '%s'; use engine, model or effort" % key)
    data = read()
    if action == "get":
        print(data.get(key, ""))
        return 0
    value = args[2]
    if "\n" in value or "\r" in value:
        raise ValueError("config values must be one line; remove the newline")
    if key == "engine":
        import re
        provider_root = Path(os.environ.get("AGENT_PROVIDER_DIR") or Path(__file__).resolve().parents[1] / "providers")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", value) or not (provider_root / value / "provider.py").is_file():
            raise ValueError("unknown engine '%s'; use neoxider provider-info ENGINE to inspect an installed provider" % value)
    from .state import Lock, atomic_write
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with Lock(path):
        data = read()
        data[key] = value
        atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    print("%s=%s" % (key, value))
    return 0
