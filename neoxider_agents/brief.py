"""Portable plain-text task contract builder."""
from pathlib import Path


def build(opts):
    if not opts.get("outcome"):
        raise ValueError("brief: add --outcome TEXT to describe the acceptance check")
    sections = ["Outcome and acceptance check:\n" + opts["outcome"],
                "Owned files:\n" + opts.get("owns", "Choose the smallest relevant set of files."),
                "Constraints:\n" + ("Do not touch: " + opts["not_touch"] + "\n" if opts.get("not_touch") else "") + "Do not commit, publish or expand scope.",
                "Return:\n" + opts.get("return", "Changed files, verification, evidence and remaining risks.")]
    if opts.get("context_file"):
        from .state import native_path
        try:
            context = Path(native_path(opts["context_file"])).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as error:
            raise ValueError("brief: --context-file is unreadable; supply a UTF-8 file") from error
        sections.append("Context:\n" + context.rstrip())
    return "\n\n".join(sections) + "\n"
