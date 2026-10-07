"""CLI parsing and lazy command routing; Python 3.8, stdlib only."""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMMON = {"-e": "engine", "-m": "model", "-f": "effort", "-C": "dir", "-t": "name", "-P": "parent", "--prompt-file": "prompt_file"}
SWITCHES = {"-p", "--no-progress", "--no-terse", "--verbose", "--terminal"}
COMMANDS = {
    "run": "[-e ENGINE] [-m MODEL] [-f EFFORT] [-C DIR] [-t NAME] [--no-progress] [--no-terse] (TEXT | --prompt-file F)",
    "fan": "[-e ENGINE] [-m MODEL] [-f EFFORT] [-C DIR] [-t BASE] TEXT...",
    "reply": "NAME [--now] [-e ENGINE] [-m MODEL] [-f EFFORT] [-C DIR] (TEXT | --prompt-file F) | --flush NAME",
    "send": "NAME [--now] [-e ENGINE] [-m MODEL] [-f EFFORT] [-C DIR] (TEXT | --prompt-file F) | --flush NAME",
    "stop": "NAME... | --all-mine", "restart": "NAME [TEXT | --prompt-file F] [--fresh] [-C DIR]",
    "peek": "NAME [-n N] [-f] [--raw]", "log": "[NAME] [-f] [-n N] [-l]",
    "last": "[NAME]", "result": "[NAME]", "status": "[NAME]", "list": "[LIMIT]",
    "pending": "[--strict]", "wait": "[NAME...] [--timeout SECONDS] [--poll SECONDS]",
    "clean": "[--all] [--purge] [-n|--dry-run]", "prune": "[--all] [--purge] [-n|--dry-run]",
    "doctor": "[--deep|--json]", "provider-info": "ENGINE",
    "test-api": "--base-url URL --goal TEXT [--out F] [-e ENGINE] [-m MODEL] [-f EFFORT] [-C DIR] [-t NAME]",
    "gui": "[PORT] [--lan] [--localhost] [--token SECRET]",
    "openai-server": "[-e ENGINE] [-m MODEL] [-f EFFORT] [-p PORT] [--api-key SECRET] (see openai_server.py --help)",
    "completion": "powershell|bash|zsh", "help": "[COMMAND]", "ask": "(alias for run)"}


def usage(command=""):
    if command and command in COMMANDS:
        print("Usage: agent.sh %s %s" % (command, COMMANDS[command]))
        return
    print("neoxider — tracked CLI agents (PowerShell / cmd / POSIX; Python >=3.8)")
    for name, args in COMMANDS.items():
        print("  neoxider %-15s %s" % (name, args))
    print("Exit: 0 success, 1 usage/preflight, 2 wait timeout, 3 empty/error, 124 timeout, 125 silent, 126 provider failure, 130 stopped.")


def parse(command, argv):
    operands = {}
    switches = set()
    if command in ("run", "fan", "reply", "send", "restart", "test-api"):
        operands.update(COMMON)
        switches.update(SWITCHES)
    if command in ("reply", "send"):
        switches.update(("--now", "--flush"))
    if command == "restart":
        switches.add("--fresh")
    if command == "test-api":
        operands.update({"--base-url": "base_url", "--goal": "goal", "--out": "out"})
    if command in ("log", "peek"):
        operands["-n"] = "-n"
        switches.add("-f")
        switches.add("--raw" if command == "peek" else "-l")
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
    for argument in iterator:
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
                raise ValueError("%s: option '%s' needs an operand" % (command, argument))
            if value.startswith("-"):
                raise ValueError("%s: option '%s' needs an operand (got option '%s')" % (command, argument, value))
            result[operands[argument]] = value
        elif argument in switches:
            result[argument] = True
        elif argument.startswith("-"):
            raise ValueError("%s: unknown option '%s' (use -- before literal option-like text)" % (command, argument))
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
        except OSError:
            raise ValueError("--prompt-file '%s' is not readable" % opts["prompt_file"])
    if len(args) > index + 1:
        raise ValueError("quote the message as one argument")
    return args[index] if len(args) > index else ""


def dispatch(command, opts, args, store):
    from . import views
    from .state import valid_name
    opts["terminal"] = opts.get("--terminal", False)
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
    if command in ("last", "result", "status", "log", "peek"):
        if len(args) > 1:
            raise ValueError(command + ": needs at most one NAME")
        name = store.resolve(args[0] if args else "")
        if command in ("last", "result"):
            return views.last(store, name, command == "result")
        if command == "status":
            return views.status(store, name)
        if command == "log":
            if "-n" in opts:
                integer(opts["-n"], "log: -n")
            return views.log(store, name, opts)
        from activity import main as peek
        flags = [str(store.path(name, ".log"))]
        for key in ("-f", "--raw"):
            if opts.get(key):
                flags.append(key)
        if "-n" in opts:
            flags += ["-n", str(integer(opts["-n"], "peek: -n"))]
        return peek(flags)
    if command == "completion":
        if len(args) != 1 or args[0] not in ("powershell", "bash", "zsh"):
            raise ValueError("completion: needs powershell, bash or zsh")
        candidates = list((ROOT / "completions").glob("*" + args[0] + "*"))
        if not candidates:
            suffix = {"powershell": ".ps1", "bash": ".bash", "zsh": ".zsh"}[args[0]]
            candidates = list((ROOT / "completions").glob("*" + suffix))
        print(candidates[0].read_text(encoding="utf-8-sig"), end="")
        return 0
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
    if command in ("run", "test-api", "fan"):
        import time
        name = opts.get("name", "task-%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), os.getpid()))
        valid_name(name)
        opts["terminal"] = opts.get("--terminal", False)
        if command == "fan":
            from .fan import fan
            return fan(store, name, args, opts)
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
        if not prompt:
            raise ValueError("run: needs a prompt")
        code = lifecycle.run(store, name, prompt, opts)
        if command == "test-api":
            store.update(name, kind="api-test", base_url=opts["base_url"])
            if opts.get("out"):
                answer = lifecycle.last_output(store.path(name, ".log"))
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
    command = argv.pop(0) if argv else "help"
    if command in ("--help", "-h", "help"):
        usage(argv[0] if argv else "")
        return 0
    if command == "ask":
        command = "run"
    if any(arg in ("--help", "-h") for arg in argv[:argv.index("--") if "--" in argv else len(argv)]):
        usage(command)
        return 0
    if command in ("gui", "openai-server"):
        from .services import launch_service
        try:
            return launch_service(command, argv)
        except (ValueError, OSError) as error:
            print("agent.sh: " + str(error), file=sys.stderr)
            return 1
    store = None
    try:
        opts, args = parse(command, argv)
        if opts is None:
            return 0
        from .state import Store
        store = Store()
        return dispatch(command, opts, args, store)
    except (ValueError, RuntimeError, OSError) as error:
        if command in ("send", "reply", "restart") and store:
            ref = "" if command == "reply" and len(args) == 1 and not opts.get("prompt_file") and not opts.get("--flush") else args[0] if args else ""
            try:
                name = store.resolve(ref)
                if store.read(name):
                    store.update(name, last_send_error=" ".join(str(error).split()))
            except (ValueError, OSError):
                pass
        print("agent.sh: " + str(error), file=sys.stderr)
        return 1
