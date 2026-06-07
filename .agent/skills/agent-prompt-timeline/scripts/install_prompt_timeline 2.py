#!/usr/bin/env python3
# Installs the prompt timeline site, hook, and skill into one or more repos.
# It preserves existing timeline logs, avoids .gitignore changes, and updates
# Claude/Codex hook JSON files idempotently.

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
from typing import Iterable


SKILL_NAME = "agent-prompt-timeline"
HOOK_EVENT = "UserPromptSubmit"
SKILL_DIR = pathlib.Path(__file__).resolve().parents[1]
TEMPLATE_DIR = SKILL_DIR / "assets" / "template"
STALE_COPY_SUFFIXES = (" 2", " 3", " 4", " copy")


def is_repo(path: pathlib.Path) -> bool:
    return (path / ".git").exists() and path.is_dir()


def discover_repos(root: pathlib.Path, max_depth: int) -> list[pathlib.Path]:
    root = root.resolve()
    repos: list[pathlib.Path] = []
    for git_dir in root.rglob(".git"):
        repo = git_dir.parent
        try:
            relative_depth = len(repo.relative_to(root).parts)
        except ValueError:
            continue
        if relative_depth <= max_depth and is_repo(repo):
            repos.append(repo)
    return sorted(set(repos))


def copy_tree_contents(src: pathlib.Path, dst: pathlib.Path, overwrite: bool = True) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.name == "__pycache__" or item.suffix == ".pyc":
            continue
        target = dst / item.name
        if item.is_dir():
            copy_tree_contents(item, target, overwrite=overwrite)
        elif overwrite or not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def remove_numbered_stale_copies(root: pathlib.Path) -> None:
    if not root.exists():
        return
    for path in sorted(root.rglob("*"), reverse=True):
        if path.name == "data":
            continue
        stem = path.stem
        if not any(stem.endswith(suffix) for suffix in STALE_COPY_SUFFIXES):
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


def install_site(repo: pathlib.Path) -> None:
    target = repo / "prompt-timeline"
    target.mkdir(parents=True, exist_ok=True)
    for stale in target.iterdir():
        if stale.name == "data":
            continue
        if stale.is_dir():
            shutil.rmtree(stale)
        elif stale.exists():
            stale.unlink()
    copy_tree_contents(TEMPLATE_DIR, target, overwrite=True)
    remove_numbered_stale_copies(target)
    data_dir = target / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    events_jsonl = data_dir / "events.jsonl"
    if not events_jsonl.exists():
        events_jsonl.write_text("", encoding="utf-8")
    record_script = repo / ".agent" / "skills" / SKILL_NAME / "scripts" / "record_event.py"
    if record_script.exists():
        env = os.environ.copy()
        env["TEAM_INFO_ROOT"] = str(repo)
        env["CLAUDE_PROJECT_DIR"] = str(repo)
        env["CODEX_PROJECT_DIR"] = str(repo)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        subprocess.run([sys.executable, str(record_script), "--rebuild"], cwd=str(repo), env=env, check=False)
    sanitize_script = repo / ".agent" / "skills" / SKILL_NAME / "scripts" / "sanitize_public_events.py"
    if sanitize_script.exists():
        env = os.environ.copy()
        env["TEAM_INFO_ROOT"] = str(repo)
        env["CLAUDE_PROJECT_DIR"] = str(repo)
        env["CODEX_PROJECT_DIR"] = str(repo)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        subprocess.run([sys.executable, str(sanitize_script)], cwd=str(repo), env=env, check=False)


def install_skill(repo: pathlib.Path) -> None:
    target_skill = repo / ".agent" / "skills" / SKILL_NAME
    if target_skill.resolve() == SKILL_DIR.resolve():
        return
    target_skill.mkdir(parents=True, exist_ok=True)
    copy_tree_contents(SKILL_DIR, target_skill, overwrite=True)
    remove_numbered_stale_copies(target_skill)


def hook_command(source: str) -> str:
    return (
        'ROOT="${CLAUDE_PROJECT_DIR:-${CODEX_PROJECT_DIR:-$PWD}}"; '
        f'python "$ROOT/.agent/skills/{SKILL_NAME}/scripts/record_event.py" '
        f"--kind prompt --source {source} --stdin-json"
    )


def load_json_object(path: pathlib.Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def ensure_hook(settings: dict, source: str) -> bool:
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        settings["hooks"] = {}
        hooks = settings["hooks"]

    entries = hooks.setdefault(HOOK_EVENT, [])
    if not isinstance(entries, list):
        hooks[HOOK_EVENT] = []
        entries = hooks[HOOK_EVENT]

    command = hook_command(source)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for hook in entry.get("hooks", []):
            if isinstance(hook, dict) and hook.get("command") == command:
                return False

    entries.append(
        {
            "hooks": [
                {
                    "type": "command",
                    "command": command,
                    "timeout": 5,
                    "statusMessage": "プロンプトを時系列ログへ記録中...",
                }
            ]
        }
    )
    return True


def write_json(path: pathlib.Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def install_hooks(repo: pathlib.Path) -> None:
    claude_path = repo / ".claude" / "settings.json"
    codex_path = repo / ".codex" / "hooks.json"
    for path, source in ((claude_path, "claude-user-prompt"), (codex_path, "codex-user-prompt")):
        existed = path.exists()
        payload = load_json_object(path)
        changed = ensure_hook(payload, source)
        if changed or not existed:
            write_json(path, payload)


def install_repo(repo: pathlib.Path, dry_run: bool = False) -> dict:
    repo = repo.resolve()
    result = {"repo": str(repo), "installed": False, "error": ""}
    if not is_repo(repo):
        result["error"] = "not a git repository"
        return result
    if dry_run:
        result["installed"] = True
        return result
    try:
        install_skill(repo)
        install_site(repo)
        install_hooks(repo)
        result["installed"] = True
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
    return result


def target_repos(args: argparse.Namespace) -> Iterable[pathlib.Path]:
    if args.all_under:
        yield from discover_repos(pathlib.Path(args.all_under), args.max_depth)
    for item in args.target:
        yield pathlib.Path(item)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install prompt timeline into repositories.")
    parser.add_argument("--target", action="append", default=[], help="Repository root to install into.")
    parser.add_argument("--all-under", default="", help="Discover git repositories under this directory.")
    parser.add_argument("--max-depth", type=int, default=4)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repos = list(dict.fromkeys(path.resolve() for path in target_repos(args)))
    if not repos:
        repos = [pathlib.Path.cwd().resolve()]
    results = [install_repo(repo, dry_run=args.dry_run) for repo in repos]
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 1 if any(item["error"] for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
