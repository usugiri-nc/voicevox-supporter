# -*- coding: utf-8 -*-
"""Codex transcript extraction tests (standard library only)."""
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_tts_hook
from codex_tts_hook import assistant_message_from_event, latest_assistant_message


def record(value):
    return json.dumps(value, ensure_ascii=False)


rows = [
    record({"type": "event_msg", "payload": {"type": "user_message", "message": "ignore"}}),
    record({"type": "response_item", "payload": {
        "type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": "Ad『最初』"}],
    }}),
    record({"type": "message", "role": "assistant", "content": "Ad『最後』"}),
    "not json",
]

with tempfile.TemporaryDirectory() as directory:
    path = Path(directory) / "transcript.jsonl"
    path.write_text("\n".join(rows), encoding="utf-8")
    actual = latest_assistant_message(path)
    fallback_message = assistant_message_from_event({"transcript_path": str(path)})

if actual != "Ad『最後』":
    print(f"[NG] latest assistant message: {actual!r}")
    sys.exit(1)
print("[OK] Codex transcript extraction")

stop_message = assistant_message_from_event({
    "hook_event_name": "Stop",
    "last_assistant_message": "Ad『Stopの本文』",
    "transcript_path": str(path),
})
if stop_message != "Ad『Stopの本文』":
    print(f"[NG] Stop event message: {stop_message!r}")
    sys.exit(1)
print("[OK] Stop event uses the documented message field")

notify_message = assistant_message_from_event({
    "type": "agent-turn-complete",
    "last-assistant-message": "Ad『notifyの本文』",
})
if notify_message != "Ad『notifyの本文』":
    print(f"[NG] notify event message: {notify_message!r}")
    sys.exit(1)
print("[OK] notify event uses the documented message field")

if fallback_message != "Ad『最後』":
    print(f"[NG] transcript fallback: {fallback_message!r}")
    sys.exit(1)
print("[OK] transcript remains an older-build fallback")

notify_event = {
    "type": "agent-turn-complete",
    "last-assistant-message": "Ad『argvから同じ本文』",
}
with patch.object(sys, "argv", ["codex_tts_hook.py", record(notify_event)]), \
        patch.object(codex_tts_hook.subprocess, "Popen") as popen:
    codex_tts_hook.main()

sent = json.loads(popen.return_value.communicate.call_args.args[0])
if sent != {"last_assistant_message": "Ad『argvから同じ本文』"}:
    print(f"[NG] supporter payload: {sent!r}")
    sys.exit(1)
if popen.call_args.args[0][-1] != "--hook":
    print(f"[NG] supporter command: {popen.call_args.args[0]!r}")
    sys.exit(1)
print("[OK] notify argv is forwarded unchanged to the supporter")
