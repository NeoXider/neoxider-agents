#!/usr/bin/env python
"""Turns `claude -p --output-format stream-json --include-partial-messages --verbose` output
into the SAME plain answer text `claude -p` would print -- but written incrementally, one
text delta at a time, so a consumer tailing the destination (tee -> task log) sees the answer
GROW while the model generates instead of appearing all at once at the end.

Used by providers/claude/provider.sh when AGENT_STREAM_TEXT=1 (set by openai_server.py's live
streaming path). Everything that is not a recognized stream-json event line passes through
verbatim -- CLI error banners and limit notices must reach the log exactly as before so the
bridge's limit-banner detection keeps working.

Event handling (defensive -- unknown event types are simply ignored):
  stream_event/content_block_delta/text_delta  -> print the delta text, flush
  assistant (full message)                     -> print its text ONLY if no deltas covered it
                                                  (older CLI without partial messages)
  result                                       -> print ONLY if nothing was printed at all
                                                  (error/limit banners surface here)

AGENT_STREAM_ACTIVITY=1 additionally emits one compact ACTIVITY MARKER line per tool call,
prefixed with ACTIVITY_PREFIX. agent.sh sets it for background run/reply tasks and strips those
lines back out when it reports the agent's answer. WHY it exists: a `claude -p` turn that spends
twenty minutes reading and editing files emits no assistant TEXT at all, so a log carrying only
text deltas stays byte-identical while the engine is working hard -- and the no-output watchdog
then kills a healthy worker as "stuck". The markers make the log grow in step with what the
engine is actually doing. The bridge (AGENT_STREAM_TEXT=1) leaves them off: its consumer
forwards the log verbatim as answer text.
"""
import json
import os
import sys

# Opens every activity line. ASCII on purpose: this is written to a console whose encoding
# is whatever Windows picked, and no real answer text starts a line with it, letting
# agent.sh strip these by prefix without eating output.
ACTIVITY_PREFIX = "[agent-activity] "


def iter_text_blocks(message):
    for block in (message or {}).get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            yield block.get("text") or ""


def iter_tool_names(message):
    for block in (message or {}).get("content") or []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            yield block.get("name") or "tool"


def main(stdin=None, stdout=None, activity=None):
    inp = stdin if stdin is not None else sys.stdin
    out = stdout if stdout is not None else sys.stdout
    if activity is None:
        activity = os.environ.get("AGENT_STREAM_ACTIVITY") == "1"
    printed_total = 0   # chars printed over the whole run
    printed_msg = 0     # chars printed via deltas for the CURRENT message
    state = {"at_line_start": True}  # a marker must never land mid-sentence in streamed text
    seen_session = False

    def mark(note):
        if not activity:
            return
        out.write(("" if state["at_line_start"] else "\n") + ACTIVITY_PREFIX + note + "\n")
        out.flush()
        state["at_line_start"] = True
    for raw in inp:
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            # Not an event: a CLI banner / OS notification / error line. Pass through so the
            # log looks exactly like the non-streaming mode's log for these.
            out.write(line + "\n")
            out.flush()
            printed_total += len(line) + 1
            continue
        if not isinstance(ev, dict):
            continue
        etype = ev.get("type")
        if etype == "system" and ev.get("subtype") == "init" and not seen_session:
            sid = ev.get("session_id") or ""
            if sid:
                seen_session = True
                # WHY this exact spelling: agent.sh sniffs the resumable session out of the
                # step log with `session id: <id>`. Plain `claude -p` prints no session id at
                # all, so resume fell back to --continue, which picks the wrong session when
                # several tasks share a directory. The stream carries the real one.
                mark("session id: " + sid)
        if etype == "stream_event":
            event = ev.get("event") or {}
            if event.get("type") == "message_start":
                printed_msg = 0
            elif event.get("type") == "content_block_delta":
                delta = event.get("delta") or {}
                if delta.get("type") == "text_delta":
                    text = delta.get("text") or ""
                    if text:
                        out.write(text)
                        out.flush()
                        state["at_line_start"] = text.endswith("\n")
                        printed_total += len(text)
                        printed_msg += len(text)
        elif etype == "assistant":
            # Complete-message event. When partial deltas already printed this message's text,
            # printing it again would duplicate the answer -- only cover the no-deltas case.
            text = "".join(iter_text_blocks(ev.get("message")))
            if text and printed_msg == 0:
                out.write(text)
                out.flush()
                state["at_line_start"] = text.endswith("\n")
                printed_total += len(text)
            for name in iter_tool_names(ev.get("message")):
                mark("tool " + name)
            printed_msg = 0
        elif etype == "result":
            # Final aggregate. Normally everything is already printed; error subtypes (limit
            # banners, refusals surfaced only here) must still reach the log.
            text = ev.get("result") or ""
            if text and printed_total == 0:
                out.write(text)
                out.flush()
                # WHY at_line_start is tracked here too, like the two branches above: without it
                # the final newline below was skipped for a result-only run, so a limit banner
                # reached the log with no line of its own and ran into whatever was appended next
                # -- and the provider-failure scan that reads those logs matches line by line.
                state["at_line_start"] = text.endswith("\n")
                printed_total += len(text)
    if printed_total and not state["at_line_start"]:
        out.write("\n")
        out.flush()


if __name__ == "__main__":
    main()
