#!/usr/bin/env python3
# Records agent prompt timeline events for the local team-info repository.
# Input may come from Claude/Codex hook JSON, explicit CLI arguments, or stdin.
# It appends JSONL and regenerates a browser-friendly events.js snapshot.

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


ROOT_MARKERS = ("AGENTS.md", ".dev-mode")
JST = ZoneInfo("Asia/Tokyo")


def find_repo_root() -> pathlib.Path:
    env_root = os.environ.get("TEAM_INFO_ROOT") or os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("CODEX_PROJECT_DIR")
    if env_root:
        return pathlib.Path(env_root).expanduser().resolve()

    current = pathlib.Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if all((candidate / marker).exists() for marker in ROOT_MARKERS):
            return candidate
    return current


REPO_ROOT = find_repo_root()
TIMELINE_DIR = REPO_ROOT / "prompt-timeline"
DATA_PATH = TIMELINE_DIR / "data" / "events.jsonl"
EVENTS_JS_PATH = TIMELINE_DIR / "assets" / "events.js"
LOCK_PATH = TIMELINE_DIR / ".record_event.lock"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def read_stdin() -> str:
    if sys.stdin.isatty():
        return ""
    return sys.stdin.read()


def safe_json_loads(value: str) -> object | None:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def first_string(payload: object, keys: tuple[str, ...]) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def extract_prompt(payload: object, raw_stdin: str) -> str:
    prompt_keys = (
        "prompt",
        "user_prompt",
        "message",
        "text",
        "input",
        "content",
    )
    prompt = first_string(payload, prompt_keys)
    if prompt:
        return prompt

    if isinstance(payload, dict):
        nested = payload.get("payload") or payload.get("data")
        prompt = first_string(nested, prompt_keys)
        if prompt:
            return prompt

    return raw_stdin.strip()


def extract_meta(payload: object) -> dict:
    if not isinstance(payload, dict):
        return {}
    meta_keys = (
        "session_id",
        "transcript_path",
        "cwd",
        "hook_event_name",
        "matcher",
        "tool_name",
    )
    meta = {}
    for key in meta_keys:
        value = payload.get(key)
        if isinstance(value, (str, int, float, bool)) and value != "":
            meta[key] = value
    return meta


SECRET_PATTERNS = (
    re.compile(r"(sk-[A-Za-z0-9_\-]{16,})"),
    re.compile(r"((?:api[_-]?key|token|secret|password)\s*[:=]\s*)([^\s]+)", re.IGNORECASE),
)


def redact_for_preview(value: str) -> str:
    result = value
    for pattern in SECRET_PATTERNS:
        if pattern.groups >= 2:
            result = pattern.sub(lambda m: f"{m.group(1)}[REDACTED]", result)
        else:
            result = pattern.sub("[REDACTED]", result)
    return result


def load_events() -> list[dict]:
    if not DATA_PATH.exists():
        return []
    events: list[dict] = []
    with DATA_PATH.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                events.append(payload)
    return events


class FileLock:
    def __init__(self, path: pathlib.Path, timeout_seconds: float = 5.0) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self.fd: int | None = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode("ascii", errors="ignore"))
                return self
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Could not acquire lock: {self.path}")
                time.sleep(0.05)

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def latest_prompt_id(events: list[dict]) -> str:
    summarized = {event.get("parent_id") for event in events if event.get("kind") == "summary" and event.get("parent_id")}
    for event in reversed(events):
        event_id = event.get("id")
        if event.get("kind") == "prompt" and event_id and event_id not in summarized:
            return str(event_id)
    for event in reversed(events):
        if event.get("kind") == "prompt" and event.get("id"):
            return str(event["id"])
    return ""


def append_event(event: dict) -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    with DATA_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def write_events_js(events: list[dict]) -> None:
    EVENTS_JS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(events, ensure_ascii=False, indent=2, sort_keys=True)
    content = (
        "/**\n"
        " * Browser snapshot generated from prompt-timeline/data/events.jsonl.\n"
        " * Do not hand-edit; use record_event.py so the source log stays append-only.\n"
        " */\n"
        f"window.PROMPT_TIMELINE_EVENTS = {payload};\n"
    )
    temp_path = EVENTS_JS_PATH.with_suffix(".js.tmp")
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(EVENTS_JS_PATH)


def build_event(args: argparse.Namespace, payload: object, raw_stdin: str, events: list[dict]) -> dict:
    now = utc_now()
    kind = args.kind
    prompt = args.prompt or extract_prompt(payload, raw_stdin)
    source = args.source or first_string(payload, ("source", "app", "client")) or "agent-hook"
    parent_id = args.parent_id or ""
    if kind == "summary" and args.update_latest and not parent_id:
        parent_id = latest_prompt_id(events)

    event = {
        "id": f"{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}",
        "timestamp": iso_z(now),
        "timestamp_jst": now.astimezone(JST).isoformat(timespec="seconds"),
        "kind": kind,
        "source": source,
        "actor": args.actor or "agent",
        "prompt_original": prompt if kind == "prompt" else "",
        "prompt_preview": redact_for_preview(prompt) if kind == "prompt" else "",
        "summary": args.summary or "",
        "actions": [item.strip() for item in args.actions if item.strip()],
        "parent_id": parent_id,
        "tags": [item.strip() for item in args.tags if item.strip()],
        "meta": extract_meta(payload),
    }
    return event


def print_latest(events: list[dict]) -> int:
    for event in reversed(events):
        if event.get("kind") == "prompt":
            print(json.dumps(event, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
    print("{}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record prompt timeline events.")
    parser.add_argument("--kind", choices=("prompt", "summary", "note"), default="prompt")
    parser.add_argument("--prompt", default="")
    parser.add_argument("--summary", default="")
    parser.add_argument("--actions", action="append", default=[])
    parser.add_argument("--tags", action="append", default=[])
    parser.add_argument("--source", default="")
    parser.add_argument("--actor", default="")
    parser.add_argument("--parent-id", default="")
    parser.add_argument("--update-latest", action="store_true")
    parser.add_argument("--stdin-json", action="store_true")
    parser.add_argument("--print-latest", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.print_latest:
        events = load_events()
        return print_latest(events)

    raw_stdin = read_stdin()
    payload = safe_json_loads(raw_stdin) if args.stdin_json and raw_stdin.strip() else {}

    with FileLock(LOCK_PATH):
        events = load_events()
        if args.rebuild:
            write_events_js(events)
            print(str(EVENTS_JS_PATH))
            return 0

        event = build_event(args, payload, raw_stdin, events)
        if event["kind"] == "prompt" and not event["prompt_original"].strip():
            return 0
        if event["kind"] == "summary" and not event["summary"].strip() and not event["actions"]:
            return 0

        append_event(event)
        events.append(event)
        write_events_js(events)
        print(event["id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
