#!/usr/bin/env python3
# Cleans prompt timeline data before it is shared as a public repo history.
# It removes personal-mode, personal-folder, business-sensitive, and terminal
# noise prompt events, then rebuilds the browser events snapshot.

from __future__ import annotations

import argparse
import json
import pathlib
import sys

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import import_history  # noqa: E402
import record_event  # noqa: E402


def event_text(event: dict) -> str:
    return "\n".join(
        str(event.get(key, "") or "")
        for key in ("prompt_original", "prompt_preview", "summary", "actions")
    )


def removal_reason(event: dict) -> str:
    if event.get("kind") != "prompt":
        return ""
    cwd = str((event.get("meta") or {}).get("cwd") or "").strip()
    if cwd:
        try:
            pathlib.Path(cwd).resolve().relative_to(record_event.REPO_ROOT)
        except ValueError:
            return "outside_repo"
    text = event_text(event)
    prompt = str(event.get("prompt_original") or event.get("prompt_preview") or "").strip()
    if import_history.should_skip(prompt):
        return "internal_or_noise"
    if any(pattern.search(text) for pattern in import_history.PERSONAL_PATTERNS):
        return "personal"
    if any(pattern.search(text) for pattern in import_history.BUSINESS_SENSITIVE_PATTERNS):
        return "business_sensitive"
    if any(pattern.search(prompt) for pattern in import_history.TERMINAL_NOISE_PATTERNS):
        return "terminal_noise"
    return ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Remove non-public prompt events from prompt-timeline data.")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    events = record_event.load_events()
    kept: list[dict] = []
    removed: list[dict] = []
    reasons: dict[str, int] = {}
    for event in events:
        reason = removal_reason(event)
        if reason:
            removed.append(event)
            reasons[reason] = reasons.get(reason, 0) + 1
            continue
        kept.append(event)

    if not args.dry_run:
        record_event.DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
        record_event.DATA_PATH.write_text(
            "\n".join(json.dumps(event, ensure_ascii=False, sort_keys=True) for event in kept) + ("\n" if kept else ""),
            encoding="utf-8",
        )
        record_event.write_events_js(kept)

    print(json.dumps({
        "repo": str(record_event.REPO_ROOT),
        "dry_run": args.dry_run,
        "removed": len(removed),
        "reasons": reasons,
        "kept_events": len(kept),
        "kept_prompts": sum(1 for event in kept if event.get("kind") == "prompt"),
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
