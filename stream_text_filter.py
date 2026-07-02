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
"""
import json
import sys


def iter_text_blocks(message):
    for block in (message or {}).get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            yield block.get("text") or ""


def main(stdin=None, stdout=None):
    inp = stdin if stdin is not None else sys.stdin
    out = stdout if stdout is not None else sys.stdout
    printed_total = 0   # chars printed over the whole run
    printed_msg = 0     # chars printed via deltas for the CURRENT message
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
                        printed_total += len(text)
                        printed_msg += len(text)
        elif etype == "assistant":
            # Complete-message event. When partial deltas already printed this message's text,
            # printing it again would duplicate the answer -- only cover the no-deltas case.
            text = "".join(iter_text_blocks(ev.get("message")))
            if text and printed_msg == 0:
                out.write(text)
                out.flush()
                printed_total += len(text)
            printed_msg = 0
        elif etype == "result":
            # Final aggregate. Normally everything is already printed; error subtypes (limit
            # banners, refusals surfaced only here) must still reach the log.
            text = ev.get("result") or ""
            if text and printed_total == 0:
                out.write(text)
                out.flush()
                printed_total += len(text)
    if printed_total:
        out.write("\n")
        out.flush()


if __name__ == "__main__":
    main()
