"""Coordinator-recorded swarm evidence and advisory role routing."""
from collections import defaultdict
from datetime import datetime, timezone
from html import escape
import json
import math
import os
import re
from statistics import median

from activity import redact

from . import config
from .events import _append, _repair, _safe_file
from .providers import get_provider
from .state import Lock, atomic_write


ROLES = ("planner", "implementer", "reviewer", "verifier", "skeptic")
SETTLED = {"done", "error", "stopped", "limited", "silent", "waiting"}
TAG = re.compile(r"(good for|bad for|never use(?: for)?)(?:\s*:\s*|\s+)(.+)", re.I)
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]+")
ROUTE_PART = re.compile(r"[^\s,/\x00-\x1f\x7f]+")
GATE_REMINDER = "Estimate at least 15 minutes of useful work per worker by default."
PAID_REMINDER = "Get explicit permission before launching any paid agent, including Claude Code or Codex."
VERIFY_REMINDER = "Use fresh reviewer and verifier contexts. Choose a different verifier provider from the implementer when available."


def dump(data, **kwargs):
    return json.dumps(data, allow_nan=False, **kwargs)


def _threshold():
    try:
        value = int(os.environ.get("AGENT_SWARM_TRUST_MIN_PASSES", "5"))
        if value > 0:
            return value
    except ValueError:
        pass
    raise ValueError("AGENT_SWARM_TRUST_MIN_PASSES must be a positive integer")


def _role(value):
    if not IDENTIFIER.fullmatch(value):
        raise ValueError("swarm role must contain only ASCII letters, digits, '_' or '-'")
    return value


def _parse_route(value, with_effort):
    parts = value.split("/")
    if len(parts) < (3 if with_effort else 2) or not IDENTIFIER.fullmatch(parts[0]) or not all(
            ROUTE_PART.fullmatch(part) for part in parts[1:]):
        raise ValueError("swarm route must be ENGINE/MODEL" + ("/EFFORT" if with_effort else ""))
    engine = parts[0]
    if with_effort:
        effort = parts[-1]
        return engine, "/".join(parts[1:-1]), "" if effort == "default" else effort
    return engine, "/".join(parts[1:])


def _route_label(engine, model, effort):
    return "/".join((engine or "unknown", model or "default", effort or "default"))


def map_path():
    return config.config_path().with_name("swarm.map")


def _read_map(path):
    try:
        routes = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise ValueError("cannot read swarm map %s; fix its JSON" % path) from error
    if not isinstance(routes, dict):
        raise ValueError("swarm map must be a JSON object mapping roles to ordered route lists")
    for role, choices in routes.items():
        _role(role)
        if not isinstance(choices, list) or not choices or not all(isinstance(route, str) for route in choices):
            raise ValueError("swarm map role %s must have a nonempty list of routes" % role)
        for route in choices:
            _parse_route(route, with_effort=True)
    return routes


def _map_command(args):
    path = map_path()
    if args == ["show"]:
        print(dump(_read_map(path), indent=2))
        return
    if not args or {"set": 3, "unset": 2}.get(args[0]) != len(args):
        raise ValueError("swarm map: use show, set ROLE ROUTE[,ROUTE...], or unset ROLE")
    action, role = args[0], _role(args[1])
    choices = [route.strip() for route in args[2].split(",")] if action == "set" else []
    for route in choices:
        _parse_route(route, with_effort=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with Lock(path):
        routes = _read_map(path)
        if choices:
            routes[role] = choices
        elif role not in routes:
            return
        else:
            del routes[role]
        atomic_write(path, dump(routes, indent=2) + "\n")


def _identity(row):
    return os.path.normcase(row["task"]), row["generation"], row["role"]


def _scan(path):
    """Return complete rows and the offset of any uncommitted tail under Lock."""
    rows = []
    _safe_file(path)
    try:
        stream = path.open("rb")
    except FileNotFoundError:
        return rows, None
    with stream:
        offset = 0
        for number, line in enumerate(stream, 1):
            if not line.endswith(b"\n"):
                return rows, offset
            try:
                row = json.loads(line)
                if not isinstance(row, dict) or row.get("verdict") not in ("pass", "fail"):
                    raise ValueError("invalid swarm record")
            except ValueError as error:
                raise ValueError("invalid swarm memory %s at line %s" % (path, number)) from error
            rows.append(row)
            offset += len(line)
    return rows, None


def _score(rows, threshold):
    groups = defaultdict(list)
    for row in rows:
        groups[(row["role"], row["engine"], row["model"], row["effort"])].append(row)
    stats = []
    for (role, engine, model, effort), entries in sorted(groups.items()):
        failures = [row for row in entries if row["verdict"] == "fail" or "fail" in row["gates"].values()]
        successes = len(entries) - len(failures)
        durations = [row["duration_sec"] for row in entries if row["duration_sec"] is not None]
        hints = {"good_for": [], "bad_for": [], "never_use": []}
        for row in entries:
            for line in row["note"].splitlines():
                match = TAG.fullmatch(line.strip())
                if match:
                    key = match[1].lower().replace(" ", "_")
                    if key == "never_use_for":
                        key = "never_use"
                    if match[2] not in hints[key]:
                        hints[key].append(match[2])
        stats.append(dict(role=role, engine=engine, model=model, effort=effort,
                          route=_route_label(engine, model, effort), successes=successes, failures=len(failures),
                          watchdog_hits=sum(row["watchdog_hit"] for row in entries),
                          median_duration_sec=median(durations) if durations else None,
                          trusted=successes >= threshold and len(failures) < 2,
                          failure_evidence=[dict(task=row["task"], date=row["date"], generation=row["generation"],
                                                 verdict=row["verdict"], gates=row["gates"]) for row in failures],
                          **hints))
    return dict(trust_min_passes=threshold, routes=stats, trusted_pairs=[row for row in stats if row["trusted"]])


def _md(value):
    return escape(" ".join(str(value).split())).replace("\\", "&#92;").replace("|", "&#124;").replace("`", "&#96;")


def _hints(row):
    for key, label in (("good_for", "Good for"), ("bad_for", "Bad for"), ("never_use", "Never use")):
        for value in row[key]:
            yield "%s: %s" % (label, _md(value))


def _score_markdown(score):
    lines = ["# Swarm scoreboard", "", "| Role | Route | Pass | Fail | Watchdog | Median (s) |",
             "| --- | --- | ---: | ---: | ---: | ---: |"]
    for row in score["routes"]:
        duration = row["median_duration_sec"]
        lines.append("| %s | %s | %s | %s | %s | %s |" % (
            _md(row["role"]), _md(row["route"]), row["successes"], row["failures"], row["watchdog_hits"],
            "—" if duration is None else duration))
    for row in score["routes"]:
        hints = list(_hints(row))
        failures = row["failure_evidence"]
        if hints or failures:
            lines.extend(["", "### %s · %s" % (_md(row["role"]), _md(row["route"]))])
            lines.extend("- " + hint for hint in hints)
            lines.extend("- Confirmed failure: %s (%s, %s)" % (_md(item["task"]), _md(item["date"]),
                                                               _md(item["generation"])) for item in failures)
    lines.extend(["", "## Trusted pairs", "", "At least %s verified passes and fewer than two confirmed failures." %
                  score["trust_min_passes"], ""])
    lines.extend(["- %s · %s" % (_md(row["role"]), _md(row["route"])) for row in score["trusted_pairs"]] or ["None yet."])
    return "\n".join(lines) + "\n"


def _log_markdown(row):
    route = _route_label(row["engine"], row["model"], row["effort"])
    gates = ", ".join("%s=%s" % item for item in row["gates"].items())
    usage = row["reported"]
    return ("- %s `%s` (%s, `%s`, generation `%s`) — %s, exit %s, %s s, %s changed files, "
            "delta %s, %s, watchdog %s, reported tokens %s, reported cost USD %s%s%s\n" % (
                *map(_md, (row["date"], row["task"], row["role"], route, row["generation"], row["state"],
                           row["exit_code"], row["duration_sec"], row["changed_file_count"], row["verdict"], gates,
                           "hit" if row["watchdog_hit"] else "—", usage["tokens"], usage["cost_usd"])),
                " — overrides: " + _md(", ".join(row["overrides"])) if row["overrides"] else "",
                " — " + _md(row["note"]) if row["note"] else ""))


class Memory:
    def __init__(self, root):
        self.path = root / "swarm_memory.jsonl"
        self.markdown = root / "swarm_memory.md"

    def score(self, threshold):
        if not self.path.exists():
            return _score([], threshold)
        with Lock(self.path):
            rows, _ = _scan(self.path)
        return _score(rows, threshold)

    def append(self, row, threshold):
        with Lock(self.path):
            rows, tail = _scan(self.path)
            prior = next((item for item in rows if _identity(item) == _identity(row)), None)
            if prior is not None:
                if any(row[key] != prior[key] for key in ("verdict", "gates", "note", "overrides")):
                    raise ValueError("swarm record already exists for this task generation and role with different evidence")
                row = prior
            else:
                rows.append(row)
            # Prepare the derived view before committing a new journal line.
            markdown = _score_markdown(_score(rows, threshold)) + "\n## Record log\n\n"
            markdown += "".join(_log_markdown(item) for item in rows)
            if tail is not None:
                _repair(self.path, tail)
            if prior is None:
                _append(self.path, dump(row, ensure_ascii=True) + "\n")
            # The journal is authoritative. An identical retry repairs a stale projection.
            atomic_write(self.markdown, markdown)
            return row


def _reported_number(value):
    try:
        if type(value) in (int, float) and math.isfinite(value) and value >= 0:
            return value
    except OverflowError:
        pass
    return None


def _record(store, ref, opts, memory):
    from .ux_tracking import collect_result

    role = _role(opts.get("role", ""))
    verdict = opts.get("verdict")
    if verdict not in ("pass", "fail") or not opts.get("gate"):
        raise ValueError("swarm record requires --verdict pass|fail and at least one --gate")
    gates = dict(review="n/a", verify="n/a")
    seen = set()
    for entry in opts["gate"]:
        gate, _, outcome = entry.partition(":")
        if gate not in gates or outcome not in ("pass", "fail", "n/a") or gate in seen:
            raise ValueError("swarm record gates must be review:pass|fail|n/a or verify:pass|fail|n/a, once each")
        seen.add(gate)
        gates[gate] = outcome
    threshold = _threshold()
    name = store.resolve(ref)
    if os.path.normcase(name) == os.path.normcase(os.environ.get("AGENT_TASK", "")):
        raise ValueError("swarm record requires coordinator evidence; a worker cannot record its own task")
    before = store.read(name)
    if before.get("state") not in SETTLED:
        raise ValueError("swarm record requires a settled task with a final result")
    result = collect_result(store, name)
    meta = store.read(name)
    fields = ("generation", "state", "exit", "engine", "model", "resolved_model", "effort", "finished_epoch")
    if any(before.get(key) != meta.get(key) for key in fields) or result["state"] not in SETTLED or result["event_pending"]:
        raise ValueError("task result changed or its terminal event is pending; retry swarm record after settlement")
    effort = meta.get("effort", "")
    model = meta.get("resolved_model") or result["model"]
    if not meta.get("resolved_model") and effort and model.endswith("-" + effort):
        model = model[:-(len(effort) + 1)]
    generation = meta.get("generation") or meta.get("finished_epoch") or meta.get("stopped_at") or "legacy"
    failure = json.loads(meta.get("failure_json") or "{}")
    row = dict(date=datetime.now(timezone.utc).isoformat(timespec="seconds"), task=name, generation=generation,
               engine=result["engine"], model=model, effort=effort, role=role, state=result["state"],
               exit_code=result["exit_code"], duration_sec=result["duration_sec"], verdict=verdict, gates=gates,
               changed_file_count=len(result["changed_files"]), baseline_known=result["baseline_known"],
               baseline_partial=result["baseline_partial"],
               watchdog_hit=bool(meta.get("timeout") or meta.get("silence") or result["state"] == "silent" or
                                 failure.get("category") in ("watchdog_timeout", "watchdog_silence")),
               reported=dict(tokens=_reported_number(result["tokens"]), cost_usd=_reported_number(result["cost_usd"])),
               overrides=[redact(value) for value in opts.get("override", [])], note=redact(opts.get("note", "")))
    return memory.append(row, threshold)


def _resolve(engine, model="", effort=""):
    model, effort = get_provider(engine).resolve(model, effort)
    return dict(engine=engine, model=model, effort=effort, route=_route_label(engine, model, effort))


def _cli_route(override):
    options = {}
    if override is not None:
        options["engine"], options["model"] = _parse_route(override, with_effort=False)
    try:
        opts = config.launch_options(options)
    except config.NoInstalledEngine:
        return None
    return _resolve(opts["engine"], opts.get("model", ""), opts.get("effort", ""))


def _plan(objective, override, memory):
    overridden = override is not None
    routes = {} if overridden else _read_map(map_path())
    default = _cli_route(override) if overridden or any(role not in routes for role in ROLES) else None
    score = memory.score(_threshold())
    index = {(row["role"], row["engine"], row["model"], row["effort"]): row for row in score["routes"]}
    roles = []
    for role in ROLES:
        choices = routes.get(role, [])
        route = _resolve(*_parse_route(choices[0], with_effort=True)) if choices else default
        source = "override" if overridden else "swarm.map" if choices else "CLI default"
        hints = index.get((role, route["engine"], route["model"], route["effort"])) if route else None
        roles.append(dict(role=role, count="N" if role == "implementer" else 1, source=source,
                          route=route, preferences=choices, memory=hints))
    return dict(advisory=True, objective=objective, roles=roles, effort_gate=GATE_REMINDER,
                paid_permission=PAID_REMINDER, verification=VERIFY_REMINDER,
                estimate=dict(kind="estimate", input_tokens_per_worker=16000, output_tokens_per_worker=4000,
                              total_tokens="20,000 × (N + 4)", cost_usd=None,
                              cost_note="Per worker at USD per million tokens: (16,000 × input rate + 4,000 × output rate) / 1,000,000. Cache and final scope may change the total."))


def _print_plan(plan):
    print("Advisory swarm plan — " + plan["objective"])
    for row in plan["roles"]:
        label = row["role"] + (" × N" if row["count"] == "N" else "")
        route = row["route"]["route"] if row["route"] else "unresolved (no agent CLI detected)"
        print("- %s: %s [%s]" % (label, route, row["source"]))
        hints = row["memory"]
        if hints:
            print("  Memory: %s passes, %s failures, %s watchdog hits; %s" % (
                hints["successes"], hints["failures"], hints["watchdog_hits"],
                "trusted" if hints["trusted"] else "trust threshold unmet"))
            for hint in _hints(hints):
                print("  " + hint)
        else:
            print("  Memory: no verified records for this role and route.")
    for key in ("effort_gate", "paid_permission", "verification"):
        print(plan[key])
    estimate = plan["estimate"]
    print("Token estimate: %s (16,000 input + 4,000 output per worker)." % estimate["total_tokens"])
    print("Cost estimate: unknown. " + estimate["cost_note"])


def command(store, args, opts):
    action, *args = args or [""]
    allowed = {"record": {"role", "verdict", "gate", "note", "override"}, "map": set(),
               "score": {"--json"}, "plan": {"route", "--json"}}
    if action not in allowed:
        raise ValueError("swarm: use record, score, map, or plan")
    if opts.keys() - allowed[action]:
        raise ValueError("unsupported option for swarm " + action)
    memory = Memory(store.root)
    if action == "record" and len(args) == 1 and args[0]:
        row = _record(store, args[0], opts, memory)
        print("Recorded %s (%s, %s)" % (row["task"], row["role"], row["verdict"]))
    elif action == "map":
        _map_command(args)
    elif action == "score" and not args:
        score = memory.score(_threshold())
        print(dump(score, ensure_ascii=False, indent=2) if opts.get("--json") else _score_markdown(score).rstrip())
    elif action == "plan" and len(args) == 1 and args[0].strip():
        plan = _plan(args[0], opts.get("route"), memory)
        if opts.get("--json"):
            print(dump(plan, ensure_ascii=False, indent=2))
        else:
            _print_plan(plan)
    else:
        raise ValueError("invalid arguments for swarm " + action)
    return 0
