"""Pass a completed Codex response to ぼいぼサポーター.

Codex Stop hooks provide JSON on stdin.  The external ``notify`` command
provides closely related JSON as its final command-line argument.  Both carry
the completed assistant text directly; transcript parsing is only a fallback
for older Codex builds.
"""

import json
import subprocess
import sys
from pathlib import Path


def _content_text(content) -> str:
    """Return text from the current and older Codex message content shapes."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        item.get("text", "")
        for item in content
        if isinstance(item, dict) and item.get("type", "text") in ("text", "output_text")
    )


def latest_assistant_message(transcript: Path) -> str:
    """Read the last visible assistant message from a Codex JSONL transcript.

    ``transcript_path`` is a convenience field rather than a stable API, so
    accept both the current ``response_item.payload`` wrapper and the older
    direct-message records.  A malformed or concurrently-written line is
    ignored rather than affecting the Codex turn.
    """
    latest = ""
    try:
        lines = transcript.read_text(encoding="utf-8").splitlines()
    except OSError:
        return latest
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = record.get("payload", record.get("message", record))
        if not isinstance(payload, dict):
            continue
        if payload.get("type") == "message" and payload.get("role") == "assistant":
            text = _content_text(payload.get("content"))
            if text:
                latest = text
    return latest


def assistant_message_from_event(event: dict) -> str:
    """Return the completed response from a Stop-hook or notify event.

    Stop uses ``last_assistant_message`` while ``notify`` uses
    ``last-assistant-message``.  Prefer these documented fields over the
    convenience transcript, whose JSONL format is explicitly unstable.
    """
    for key in ("last_assistant_message", "last-assistant-message"):
        message = event.get(key)
        if isinstance(message, str) and message:
            return message

    transcript = event.get("transcript_path")
    if not isinstance(transcript, str) or not transcript:
        return ""
    path = Path(transcript)
    if not path.is_file():
        return ""
    return latest_assistant_message(path)


def _load_event() -> dict:
    """Load either a notify argv payload or a Stop-hook stdin payload."""
    try:
        if len(sys.argv) > 1:
            event = json.loads(sys.argv[-1])
        else:
            event = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return {}
    return event if isinstance(event, dict) else {}


def _supporter_command() -> list[str]:
    """Find the source or portable-exe entry point beside this adapter."""
    directory = Path(__file__).resolve().parent
    for candidate in (
        directory / "voibo-supporter.exe",
        directory.parent / "voibo-supporter.exe",
    ):
        if candidate.is_file():
            return [str(candidate), "--hook"]
    return [sys.executable, str(directory / "speak.py"), "--hook"]


def main() -> None:
    try:
        event = _load_event()
        message = assistant_message_from_event(event)
        if not message:
            return
        subprocess.Popen(
            _supporter_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).communicate(
            json.dumps({"last_assistant_message": message}, ensure_ascii=False).encode()
        )
    except Exception:
        # A notification must never affect the Codex turn.
        return


if __name__ == "__main__":
    main()
