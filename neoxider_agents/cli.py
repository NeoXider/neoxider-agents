"""CLI parsing and lazy command routing; Python 3.8, stdlib only."""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMMON = {"-e": "engine", "--engine": "engine", "-m": "model", "--model": "model", "-f": "effort", "--effort": "effort", "-C": "dir", "--dir": "dir", "-t": "name", "--task": "name", "-P": "parent", "--parent": "parent", "--prompt-file": "prompt_file", "-p": "prompt_file", "--owns": "owns"}
SWITCHES = {"--progress", "--no-progress", "--no-terse", "-v", "--verbose", "--terminal", "--log", "--strict-owns", "--notify"}
COMMANDS = {
    "run": "[-e ENGINE] [-m MODEL] [-f EFFORT] [-C DIR] [-t NAME] [--progress] [-v] [--log] [--owns GLOBS] [--strict-owns] [--notify] (TEXT | -p FILE | -)",
    "ask": "[-e ENGINE] [-m MODEL] [-f EFFORT] [-C DIR] [-t NAME] (TEXT | -p FILE | -); prints only the final answer",
    "fan": "[-e ENGINE] [-m MODEL] [-f EFFORT] [-C DIR] [-t BASE] TEXT...",
    "reply": "NAME [--now] [-e ENGINE] [-m MODEL] [-f EFFORT] [-C DIR] (TEXT | --prompt-file F) | --flush NAME",
    "send": "NAME [--now] [-e ENGINE] [-m MODEL] [-f EFFORT] [-C DIR] (TEXT | --prompt-file F) | --flush NAME",
    "stop": "NAME... | --all-mine", "restart": "NAME [TEXT | --prompt-file F] [--fresh] [-C DIR]",
    "peek": "NAME [-n N] [-f] [--raw]", "watch": "NAME [-n N] [--raw] (follow activity with status)", "log": "[NAME] [-f] [-n N] [-l]",
    "last": "[NAME]", "result": "[NAME] [--json]", "status": "[NAME]", "list": "[LIMIT]",
    "top": "[--once] [--json] [--interval SECONDS]", "dashboard": "(alias for top)",
    "diff": "NAME [--stat|--names] (changes since the task baseline)",
    "pending": "[--strict]", "wait": "[NAME...] [--timeout SECONDS] [--poll SECONDS]",
    "clean": "[--all] [--purge] [-n|--dry-run]", "prune": "[--all] [--purge] [-n|--dry-run]",
    "doctor": "[--deep|--json]", "provider-info": "ENGINE",
    "test-api": "--base-url URL --goal TEXT [--out F] [-e ENGINE] [-m MODEL] [-f EFFORT] [-C DIR] [-t NAME]",
    "gui": "[PORT] [--lan] [--localhost] [--token SECRET]",
    "openai-server": "[-e ENGINE] [-m MODEL] [-f EFFORT] [-p PORT] [--api-key SECRET] (see openai_server.py --help)",
    "config": "get KEY | set KEY VALUE | list (keys: engine, model, effort)",
    "brief": "--outcome TEXT [--owns GLOBS] [--not-touch GLOBS] [--return TEXT] [--context-file FILE]",
    "completion": "[powershell|bash|zsh] [--install] [--profile FILE]", "help": "[COMMAND]"}


def usage(command=""):
    if command and command in COMMANDS:
        print("Usage: neoxider %s %s" % (command, COMMANDS[command]))
        if command in ("run", "ask", "fan", "test-api"):
            print("Defaults: current directory; prompt slug + 4-character id; configured or installed engine; provider model/effort.")
            print("Progress off, quiet output and bounded ephemeral raw logs. Use --progress, -v and --log to opt in. --debug shows tracebacks.")
        if command == "config":
            print("Config: AGENT_CONFIG, else %APPDATA%/neoxider-agents/config.json on Windows; ~/.config/neoxider-agents/config.json elsewhere.")
        if command == "brief":
            print("Pipe the contract: neoxider brief --outcome TEXT --owns GLOBS | neoxider run -t NAME -")
        if command == "completion":
            print("Install once: neoxider completion powershell --install; open a new shell to activate.")
        return
    print("neoxider — tracked CLI agents (PowerShell / cmd / POSIX; Python >=3.8)")
    for name, args in COMMANDS.items():
        print("  neoxider %-15s %s" % (name, args))
    print("No subcommand: neoxider \"prompt\" | neoxider -p FILE | neoxider - (stdin). --debug shows error tracebacks.")
    print("Exit: 0 success, 1 usage/preflight, 2 wait timeout, 3 empty/error, 124 timeout, 125 silent, 126 provider failure, 130 stopped.")


def parse(command, argv):
    operands = {}
    switches = set()
    if command in ("run", "ask", "fan", "reply", "send", "restart", "test-api"):
        operands.update(COMMON)
        switches.update(SWITCHES)
    if command in ("reply", "send"):
        switches.update(("--now", "--flush"))
    if command == "restart":
        switches.add("--fresh")
    if command == "test-api":
        operands.update({"--base-url": "base_url", "--goal": "goal", "--out": "out"})
    if command in ("log", "peek", "watch"):
        operands["-n"] = "-n"
        switches.add("-f")
        switches.add("--raw" if command in ("peek", "watch") else "-l")
    if command in ("top", "dashboard"):
        switches.update(("--once", "--json"))
        operands["--interval"] = "interval"
    if command == "result":
        switches.add("--json")
    if command == "diff":
        switches.update(("--stat", "--names"))
    if command == "brief":
        operands.update({"--outcome": "outcome", "--owns": "owns", "--not-touch": "not_touch", "--return": "return", "--context-file": "context_file"})
    if command == "completion":
        switches.add("--install")
        operands["--profile"] = "profile"
    if command == "stop":
        switches.add("--all-mine")
    if command == "wait":
        operands.update({"--timeout": "timeout", "--poll": "poll"})
    if command == "pending":
        switches.add("--strict")
    if command in ("clean", "prune"):
        switches.update(("--all", "--purge", "-n", "--dry-run"))
    if command == "doctor":
        switches.update(("--deep", "--json"))
    result, args, ended = {}, [], False
    iterator = iter(argv)
    while True:
        try:
            argument = next(iterator)
        except StopIteration:
            break
        if ended:
            args.append(argument)
        elif argument == "--":
            ended = True
        elif argument in ("--help", "-h"):
            usage(command)
            return None, None
        elif argument in operands:
            try:
                value = next(iterator)
            except StopIteration:
                if argument == "-p":
                    result["--progress"] = True
                    continue
                raise ValueError("%s: option '%s' needs an operand" % (command, argument))
            if argument == "-p" and value.startswith("-"):
                from itertools import chain
                result["--progress"] = True
                iterator = chain([value], iterator)
                continue
            if value.startswith("-") and value != "-":
                raise ValueError("%s: option '%s' needs an operand (got option '%s')" % (command, argument, value))
            result[operands[argument]] = value
        elif argument in switches:
            result[argument] = True
        elif argument.startswith("-") and argument != "-":
            import difflib
            suggestion = difflib.get_close_matches(argument, list(operands) + list(switches), n=1, cutoff=0.65)
            fix = "did you mean '%s'?" % suggestion[0] if suggestion else "use neoxider %s --help, or -- before literal option-like text" % command
            raise ValueError("%s: unknown option '%s'; %s" % (command, argument, fix))
        else:
            args.append(argument)
    return result, args


def integer(value, label, default=0):
    try:
        number = int(value if value is not None else default)
    except ValueError:
        raise ValueError(label + " must be a non-negative integer")
    if number < 0:
        raise ValueError(label + " must be a non-negative integer")
    return number


def text_prompt(opts, args, index=0):
    if "prompt_file" in opts:
        if len(args) > index:
            raise ValueError("use TEXT or --prompt-file, not both")
        from .state import native_path
        try:
            return Path(native_path(opts["prompt_file"])).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            raise ValueError("--prompt-file '%s' is not readable; supply an existing UTF-8 file; for progress use --progress TEXT" % opts["prompt_file"])
    if len(args) > index + 1:
        raise ValueError("quote the message as one argument")
    text = args[index] if len(args) > index else ""
    if text == "-":
        return sys.stdin.read()
    return text


def task_name(store, prompt):
    """Reserve a prompt-derived name atomically so concurrent launches cannot collide."""
    import re
    import unicodedata
    import uuid
    russian = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя", ("a", "b", "v", "g", "d", "e", "yo", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t", "u", "f", "kh", "ts", "ch", "sh", "shch", "", "y", "", "e", "yu", "ya")))
    from activity import redact
    text = "".join(russian.get(c, c) for c in redact(prompt).lower())
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:36].rstrip("-") or "task"
    for _ in range(1024):
        name = slug + "-" + uuid.uuid4().hex[:4]
        reservation = store.path(name, ".reserve")
        if store.path(name, ".meta").exists():
            continue
        try:
            fd = os.open(str(reservation), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        if store.path(name, ".meta").exists():
            reservation.unlink()
            continue
        return name, reservation
    raise ValueError("could not reserve a task name; choose an explicit unique name with -t NAME")


def dispatch(command, opts, args, store):
    from . import views
    from .state import valid_name
    opts["terminal"] = opts.get("--terminal", False)
    if opts.get("-v"):
        opts["--verbose"] = True
    if command in ("top", "dashboard"):
        if args:
            raise ValueError("top: remove extra arguments; use --once or --json")
        if "interval" in opts:
            try:
                interval = float(opts["interval"])
            except ValueError:
                raise ValueError("top: --interval must be a positive number")
            if interval <= 0:
                raise ValueError("top: --interval must be a positive number")
        from .ux_tracking import top
        return top(store, opts)
    if command in ("list", "pending", "wait", "clean", "prune"):
        if command == "list":
            if len(args) > 1:
                raise ValueError("list: needs at most one limit")
            return views.task_list(store, integer(args[0] if args else None, "list: limit", 20))
        if command == "pending":
            return views.pending(store, opts.get("--strict", False))
        if command == "wait":
            poll = integer(opts.get("poll", os.environ.get("AGENT_WAIT_POLL", 5)), "wait: --poll", 5)
            return views.wait(store, args, integer(opts.get("timeout", os.environ.get("AGENT_WAIT_TIMEOUT", 0)), "wait: --timeout"), max(0.1, poll))
        return views.clean(store, opts)
    if command in ("last", "result", "status", "log", "peek", "watch", "diff"):
        if len(args) > 1:
            raise ValueError(command + ": needs at most one NAME")
        name = store.resolve(args[0] if args else "")
        if command == "last":
            return views.last(store, name)
        if command in ("result", "diff"):
            from . import ux_tracking
            return getattr(ux_tracking, command)(store, name, opts)
        if command == "status":
            return views.status(store, name)
        if command == "log":
            if "-n" in opts:
                integer(opts["-n"], "log: -n")
            return views.log(store, name, opts)
        if "-n" in opts:
            integer(opts["-n"], command + ": -n")
        from . import logs
        return getattr(logs, command)(store, name, opts)
    if command in ("doctor", "provider-info"):
        from .diagnostics import doctor
        return doctor(store, opts, args, command == "provider-info")
    from . import lifecycle
    if command == "stop":
        if opts.get("--all-mine"):
            if args:
                raise ValueError("stop: --all-mine cannot be combined with names")
            if not (os.environ.get("AGENT_PARENT") or os.environ.get("AGENT_ORCHESTRATOR_ID")):
                raise ValueError("stop: --all-mine requires AGENT_PARENT or AGENT_ORCHESTRATOR_ID; ownership is unknown")
            args = [name for name, meta, _ in store.scan() if views.mine(meta)]
        elif not args:
            raise ValueError("stop: needs NAME... or --all-mine")
        for name in args:
            lifecycle.stop(store, name, os.environ.get("AGENT_STOP_BY", os.environ.get("AGENT_CONTROL_BY", "orchestrator")))
        return 0
    if command in ("send", "reply", "restart"):
        if command == "reply" and len(args) == 1 and not opts.get("prompt_file") and not opts.get("--flush"):
            ref, text = "", args[0]
        else:
            ref = args[0] if args else ""
            text = text_prompt(opts, args, 1)
        if command == "restart":
            if not ref:
                raise ValueError("restart needs NAME [TEXT]")
            return lifecycle.restart(store, ref, text, opts)
        if opts.get("--flush"):
            if text or len(args) != 1:
                raise ValueError("--flush needs only NAME")
        elif not text:
            raise ValueError("needs a message text")
        return lifecycle.send(store, ref, text, opts)
    if command in ("run", "ask", "test-api", "fan"):
        from .config import launch_options
        opts["ask"] = command == "ask"
        opts["terminal"] = opts.get("--terminal", False)
        if command == "fan":
            from .fan import fan
            if not args:
                raise ValueError("fan: supply quoted prompts to launch")
            opts = launch_options(opts)
            reservation = None
            name = opts.get("name")
            if not name:
                name, reservation = task_name(store, args[0])
            valid_name(name)
            try:
                return fan(store, name, args, opts)
            finally:
                if reservation:
                    reservation.unlink(missing_ok=True)
        if command == "test-api":
            if not opts.get("base_url") or not opts.get("goal"):
                raise ValueError("test-api: needs --base-url URL and --goal TEXT")
            prompt = ('Exercise the HTTP API at %s using real HTTP requests. Goal: %s. '
                      'Return only strict JSON {"base_url":"...","goal":"...","overall":"pass|fail|partial",'
                      '"endpoints":[{"method":"...","path":"...","assertion":"...","result":"pass|fail","reason":"..."}],'
                      '"summary":{"total":1,"passed":1,"failed":0}}. '
                      'Never invent a passed request. Do NOT run git commit. Do NOT modify any files unless the goal explicitly requires it.') % (opts["base_url"], opts["goal"])
        else:
            prompt = text_prompt(opts, args)
        if not prompt.strip():
            raise ValueError(command + ": needs a prompt; use TEXT, -p FILE or - for stdin")
        opts = launch_options(opts)
        reservation = None
        name = opts.get("name")
        if not name:
            name, reservation = task_name(store, prompt)
        valid_name(name)
        try:
            code = lifecycle.run(store, name, prompt, opts)
        finally:
            if reservation:
                reservation.unlink(missing_ok=True)
        if command == "test-api":
            store.update(name, kind="api-test", base_url=opts["base_url"])
            if opts.get("out"):
                from .logs import answer_text
                answer = answer_text(store, name)
                try:
                    body = json.loads(answer[answer.index("{"):answer.rindex("}") + 1])
                    answer = json.dumps(body, ensure_ascii=False, indent=2)
                except (ValueError, IndexError):
                    pass
                Path(opts["out"]).write_text(answer, encoding="utf-8")
        return code
    raise ValueError("unknown command: " + command)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    debug = False
    before_end = argv.index("--") if "--" in argv else len(argv)
    for index in range(before_end - 1, -1, -1):
        if argv[index] == "--debug":
            debug = True
            argv.pop(index)
    first = argv[0] if argv else "help"
    command = argv.pop(0) if first in COMMANDS or first in ("--help", "-h") else "run"
    if command in ("--help", "-h", "help"):
        usage(argv[0] if argv else "")
        return 0
    if any(arg in ("--help", "-h") for arg in argv[:argv.index("--") if "--" in argv else len(argv)]):
        usage(command)
        return 0
    store = None
    opts, args = {}, []
    try:
        if first not in COMMANDS and not first.startswith("-") and not any(character.isspace() for character in first):
            import difflib
            suggestion = difflib.get_close_matches(first, COMMANDS, n=1, cutoff=0.8)
            if suggestion:
                raise ValueError("unknown command '%s'; did you mean '%s'? Use neoxider run -- '%s' for a literal prompt" % (first, suggestion[0], first))
        if command in ("gui", "openai-server"):
            from .services import launch_service
            return launch_service(command, argv)
        opts, args = parse(command, argv)
        if opts is None:
            return 0
        if command == "config":
            from .config import command as configure
            return configure(args)
        if command == "brief":
            if args:
                raise ValueError("brief: use --outcome TEXT and the contract flags; remove extra arguments")
            from .brief import build
            print(build(opts), end="")
            return 0
        if command == "completion":
            from .completion import complete
            return complete(opts, args)
        from .state import Store
        store = Store()
        return dispatch(command, opts, args, store)
    except (BrokenPipeError, KeyboardInterrupt):
        raise
    except Exception as error:
        if command in ("send", "reply", "restart") and store:
            ref = "" if command == "reply" and len(args) == 1 and not opts.get("prompt_file") and not opts.get("--flush") else args[0] if args else ""
            try:
                name = store.resolve(ref)
                if store.read(name):
                    store.update(name, last_send_error=" ".join(str(error).split()))
            except (ValueError, OSError):
                pass
        if debug:
            import traceback
            traceback.print_exc()
        else:
            message = " ".join(str(error).split()) or type(error).__name__
            from activity import redact
            message = redact(message)
            if isinstance(error, FileNotFoundError) and "CLI not found" in message:
                message += "; install that CLI or set AGENT_ENGINE_BIN to its executable"
            elif not any(hint in message.lower() for hint in (";", " use ", "fix ", "install ", "supply ", "remove ", "choose ", "add ", "must ")):
                message += "; use neoxider %s --help or --debug to diagnose" % command
            print("neoxider: " + message, file=sys.stderr)
        return 1
