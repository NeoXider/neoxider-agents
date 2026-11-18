"""Validate the repository's root Claude Code plugin and orchestration skill."""

import json
import re
import sys
from pathlib import Path


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key %r" % key)
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError("invalid JSON constant %s" % value)


def read_manifest(root, name):
    path = root / ".claude-plugin" / name
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object,
                          parse_constant=reject_constant)
    except ValueError as error:
        raise ValueError("%s: %s" % (name, error)) from error


def require_fields(value, label, *fields, nested=()):
    expected = set(fields) | set(nested)
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("%s must contain exactly: %s" % (label, ", ".join(sorted(expected))))
    for field in fields:
        if not isinstance(value[field], str) or not value[field].strip():
            raise ValueError("%s.%s must be nonempty text" % (label, field))


def read_skill(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        raise ValueError("SKILL.md must begin with a delimited frontmatter block")
    end = lines.index("---", 1)
    fields = []
    for line in lines[1:end]:
        # The repository uses two single-line plain YAML scalars.
        match = re.fullmatch(r"(name|description): ([^\s\[\]{}&*!|>'\"%@`#,?:-].*)", line)
        if not match or re.search(r":(?:\s|$)|\s#", match[2]):
            raise ValueError("SKILL.md frontmatter expects plain name and description scalars")
        fields.append(match.groups())
    if not any(line.strip() for line in lines[end + 1:]):
        raise ValueError("SKILL.md must include skill instructions")
    return unique_object(fields)


def validate(root):
    plugin = read_manifest(root, "plugin.json")
    require_fields(plugin, "plugin", "name", "description", "version", "homepage", "license",
                   nested=("author",))
    require_fields(plugin["author"], "plugin.author", "name", "url")
    if not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", plugin["version"], re.ASCII):
        raise ValueError("plugin.version must be MAJOR.MINOR.PATCH")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", plugin["name"]):
        raise ValueError("plugin.name must be lowercase kebab-case")

    marketplace = read_manifest(root, "marketplace.json")
    require_fields(marketplace, "marketplace", "name", "description", nested=("owner", "plugins"))
    require_fields(marketplace["owner"], "marketplace.owner", "name", "url")
    entries = marketplace["plugins"]
    if not isinstance(entries, list) or len(entries) != 1:
        raise ValueError("marketplace.plugins must contain the root plugin")
    entry = entries[0]
    require_fields(entry, "marketplace.plugins[0]", "name", "source", "description")
    if entry["source"] != "./" or any(entry[key] != plugin[key] for key in ("name", "description")):
        raise ValueError("marketplace entry must match plugin.json and point at './'")

    skill = read_skill(root / "SKILL.md")
    require_fields(skill, "SKILL.md frontmatter", "name", "description")
    if skill["name"] != plugin["name"] or len(skill["name"]) > 64 or len(skill["description"]) > 1024:
        raise ValueError("SKILL.md name must match the plugin (max 64 chars), description max 1024 chars")


if __name__ == "__main__":
    try:
        validate(Path(__file__).resolve().parents[1])
    except (OSError, ValueError) as error:
        print("Plugin validation failed: %s" % error, file=sys.stderr)
        sys.exit(1)
    print("Plugin metadata and skill frontmatter passed strict validation.")
