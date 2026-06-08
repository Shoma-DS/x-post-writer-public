#!/usr/bin/env python3
# Imports recoverable local Claude/Codex user prompts into the prompt timeline.
# It scans non-deleted JSONL session logs for this repository, filters meta
# messages, redacts common secrets, appends new events, and rebuilds events.js.

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import record_event  # noqa: E402


REPO_ROOT = record_event.REPO_ROOT.resolve()
JST = ZoneInfo("Asia/Tokyo")


def claude_project_dir(repo_root: pathlib.Path) -> pathlib.Path:
    slug = str(repo_root).replace("/", "-")
    return pathlib.Path.home() / ".claude" / "projects" / slug


CLAUDE_PROJECT_DIR = claude_project_dir(REPO_ROOT)
CODEX_SESSION_DIRS = (
    pathlib.Path("/Users/deguchishouma/.codex/sessions"),
    pathlib.Path("/Users/deguchishouma/.codex/archived_sessions"),
)

META_MARKERS = (
    "<local-command-caveat>",
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<system-reminder>",
)

CONTROL_PROMPT_PREFIXES = (
    "# AGENTS.md instructions for ",
    "# Context from my IDE setup:",
    "<environment_context>",
    "<permissions instructions>",
    "<collaboration_mode>",
    "<turn_aborted>",
    "<subagent_notification>",
    "<task>",
    "<task-notification>",
    "<tool_use_error>",
    "<persisted-output>",
    "<ide_opened_file>",
    "<goal_context>",
    "<codex_internal_context",
    "<skill>",
)

SECRET_PATTERNS = record_event.SECRET_PATTERNS

PERSONAL_PATTERNS = (
    re.compile(r"personal/deguchishouma|/personal/|個人フォルダ|個人用|個人開発モード|personal\s*mode", re.IGNORECASE),
)

BUSINESS_SENSITIVE_PATTERNS = (
    re.compile(r"売上|売り上げ|収益|利益|粗利|月商|年商|原価|請求|入金|KPI|kpi|CVR|CPA|ROAS|広告費|売掛|決算|財務|口座|振込"),
    re.compile(r"(?:売上|売り上げ|収益|利益|粗利|広告費|請求|入金|単価|原価).{0,24}(?:[0-9０-９][0-9０-９,，.]*\s*(?:円|万円|億円|%|％)|[¥￥$])"),
    re.compile(r"(?:[0-9０-９][0-9０-９,，.]*\s*(?:円|万円|億円|%|％)|[¥￥$]).{0,24}(?:売上|売り上げ|収益|利益|粗利|広告費|請求|入金|単価|原価)"),
)

TERMINAL_NOISE_PATTERNS = (
    re.compile(r"https://accounts\.google\.com/o/oauth2/auth", re.IGNORECASE),
    re.compile(r"\.apps\.googleusercontent\.com", re.IGNORECASE),
    re.compile(r"^\[[^\]\n]+:[^\]\n]+\]\s+~?/"),
    re.compile(r"^\([^)]+\)\s*\n?\[[^\]\n]+:[^\]\n]+\]"),
    re.compile(r"^(?:[A-Za-z_][\w-]*:\s*)?error:\s", re.IGNORECASE),
    re.compile(r"^\S+:\s*\[Errno\s+[0-9]+\]"),
    re.compile(r"^whisper:\s*error:", re.IGNORECASE),
    re.compile(r"^(?:Downloading|Using cached|Installing collected packages|Successfully installed)\s+", re.IGNORECASE),
    re.compile(r"^Successfully loaded skill\b", re.IGNORECASE),
)


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]


def redact(value: str) -> str:
    return record_event.redact_for_preview(value)


def iso_to_jst(value: str) -> str:
    try:
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(JST).isoformat(timespec="seconds")
    except ValueError:
        return value


def json_lines(path: pathlib.Path):
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield line_number, json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def content_to_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def should_skip(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    if any(marker in stripped for marker in META_MARKERS):
        return True
    if stripped == "OK":
        return False
    if any(stripped.startswith(prefix) for prefix in CONTROL_PROMPT_PREFIXES):
        return True
    if stripped.startswith("Bash ") and "\nIN\n" in stripped and "\nOUT\n" in stripped:
        return True
    if should_exclude_public_prompt(stripped):
        return True
    return False


def should_exclude_public_prompt(text: str) -> bool:
    stripped = text.strip()
    if any(pattern.search(stripped) for pattern in PERSONAL_PATTERNS):
        return True
    if any(pattern.search(stripped) for pattern in BUSINESS_SENSITIVE_PATTERNS):
        return True
    if any(pattern.search(stripped) for pattern in TERMINAL_NOISE_PATTERNS):
        return True
    return False


def clean_prompt_text(text: str) -> str:
    stripped = text.strip()
    stripped = re.sub(r"^<image(?:\s+[^>]*)?>\s*</image>\s*", "", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"^\[Image #[0-9]+\]\s*", "画像添付: ", stripped)
    return stripped


def short_subject(text: str, limit: int = 74) -> str:
    value = normalize(text)
    value = re.sub(r"https?://\S+", "URL", value)
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def summary_for_prompt(text: str) -> str:
    lowered = text.lower()
    subject = short_subject(text)

    approval_words = ("ok", "OK", "実行していい", "進めて", "お願いします", "コミットOK", "はい")
    if normalize(text) in approval_words or any(word in text for word in ("実行していい", "進めて", "コミットOK")):
        return f"直前まで相談していた作業を進めるための承認・続行指示です。流れとしては、確認待ちだった操作を実行段階へ移す意図があります。原文:「{subject}」"

    if any(word in lowered for word in ("git", "コミット", "プッシュ", "push", "pull", "ブランチ", "pr")):
        return f"リポジトリの変更を整理して Git に反映するための指示です。流れとしては、差分確認、必要なコミット作成、push または同期まで進める意図があります。原文:「{subject}」"

    if any(word in lowered for word in ("削除", "消して", "いらない", "除外", "不要")):
        return f"不要なファイル、表示、履歴、または処理対象を取り除くための指示です。流れとしては、対象を特定して、今後混ざらないよう整理する意図があります。原文:「{subject}」"

    if any(word in lowered for word in ("remotion", "レンダー", "レンダリング", "動画", "字幕", "tts", "voicevox", "voicebox", "カラオケ", "台本", "音声")):
        return f"Remotion や音声・字幕まわりの動画制作を進めるための指示です。流れとしては、素材や台本の確認、実行コマンドやレンダー手順の整理、必要な修正につなげる意図があります。原文:「{subject}」"

    if any(word in lowered for word in ("jmty", "ジモティ", "weekly", "banner", "drive", "googleドライブ", "スプレッドシート", "sheets", "gas")):
        return f"ジモティー運用や Google Drive / Sheets 連携作業を進めるための指示です。流れとしては、投稿データ・画像・シート反映・Drive 配置を確認して運用成果物を整える意図があります。原文:「{subject}」"

    if any(word in lowered for word in ("x投稿", "x長文", "twitter", "x.com", "ポスト", "記事", "kimi", "webbridge")):
        return f"X 投稿・長文記事・ブラウザ操作を進めるための指示です。流れとしては、下書き作成、画面確認、投稿または記事編集の実行へつなげる意図があります。原文:「{subject}」"

    if any(word in lowered for word in ("skill", "スキル", "agents.md", "hook", "フック", "slash", "コマンド", ".agent", ".claude", ".codex")):
        return f"エージェント運用ルールや Skill / hook / command の仕組みを整えるための指示です。流れとしては、既存ルールを確認し、再利用できる手順や自動記録の仕組みへ落とし込む意図があります。原文:「{subject}」"

    if any(word in lowered for word in ("vercel", "netlify", "デプロイ", "公開", "サイト", "webサイト", "lp", "html", "css", "ui")):
        return f"Web サイトや公開ページを作成・改善・公開するための指示です。流れとしては、表示内容の整理、UI 修正、デプロイ確認まで進める意図があります。原文:「{subject}」"

    if any(word in lowered for word in ("画像", "バナー", "サムネ", "イラスト", "png", "jpg", "デザイン")):
        return f"画像・バナー・サムネイルなどの視覚素材を作る、修正する、または配置を確認するための指示です。流れとしては、元データや用途を確認して成果物の見た目を整える意図があります。原文:「{subject}」"

    if any(word in lowered for word in ("修正", "直して", "エラー", "失敗", "動かない", "確認", "検証", "ちゃんと")):
        return f"既存の不具合や不十分な結果を確認し、期待どおりに直すための指示です。流れとしては、現状確認、原因の切り分け、修正と検証まで求めています。原文:「{subject}」"

    if any(word in lowered for word in ("調べ", "リサーチ", "検索", "探して", "比較")):
        return f"必要な情報を調査して判断材料を集めるための指示です。流れとしては、関連情報を探し、要点を整理して次の作業判断につなげる意図があります。原文:「{subject}」"

    if len(normalize(text)) <= 28:
        return f"短い追加指示または確認返答です。直前の作業文脈を前提に、次の対応を進める意図があります。原文:「{subject}」"

    return f"team-info 内の作業を進めるためのユーザー指示です。流れとしては、原文で指定された対象を確認し、必要な作成・修正・整理・検証につなげる意図があります。原文:「{subject}」"


def session_cwd(path: pathlib.Path) -> str:
    latest_cwd = ""
    for _, item in json_lines(path):
        if item.get("type") == "session_meta":
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            cwd = payload.get("cwd")
            if isinstance(cwd, str):
                return cwd
        if item.get("type") == "turn_context":
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            cwd = payload.get("cwd")
            if isinstance(cwd, str):
                latest_cwd = cwd
    return latest_cwd


def event(source: str, path: pathlib.Path, line_number: int, timestamp: str, text: str) -> dict:
    safe_text = redact(text)
    digest = sha(f"{source}\n{timestamp}\n{normalize(safe_text)}")
    return {
        "id": f"import-{digest}",
        "timestamp": timestamp,
        "timestamp_jst": iso_to_jst(timestamp),
        "kind": "prompt",
        "source": source,
        "actor": "user",
        "prompt_original": safe_text,
        "prompt_preview": safe_text,
        "summary": summary_for_prompt(safe_text),
        "actions": [
            f"source: {path}",
            f"line: {line_number}",
        ],
        "parent_id": "",
        "tags": ["imported", "history", source.replace("import-", "")],
        "meta": {
            "cwd": str(REPO_ROOT),
            "source_file": str(path),
            "source_line": line_number,
            "imported_by": "agent-prompt-timeline/import_history.py",
        },
    }


def iter_claude() -> tuple[list[dict], dict[str, int]]:
    stats = {"files": 0, "lines": 0, "candidates": 0, "skipped_meta": 0}
    events: list[dict] = []
    if not CLAUDE_PROJECT_DIR.exists():
        return events, stats
    for path in sorted(CLAUDE_PROJECT_DIR.glob("*.jsonl")):
        stats["files"] += 1
        for line_number, item in json_lines(path):
            stats["lines"] += 1
            if item.get("type") != "user":
                continue
            if item.get("isMeta"):
                stats["skipped_meta"] += 1
                continue
            cwd = item.get("cwd")
            if isinstance(cwd, str) and pathlib.Path(cwd).resolve() != REPO_ROOT:
                continue
            message = item.get("message") if isinstance(item.get("message"), dict) else {}
            if message.get("role") != "user":
                continue
            text = content_to_text(message.get("content"))
            text = clean_prompt_text(text)
            if should_skip(text):
                stats["skipped_meta"] += 1
                continue
            timestamp = item.get("timestamp")
            if not isinstance(timestamp, str):
                continue
            stats["candidates"] += 1
            events.append(event("import-claude-history", path, line_number, timestamp, text))
    return events, stats


def iter_codex() -> tuple[list[dict], dict[str, int]]:
    stats = {"files": 0, "team_files": 0, "lines": 0, "candidates": 0, "skipped_meta": 0}
    events: list[dict] = []
    paths: list[pathlib.Path] = []
    for directory in CODEX_SESSION_DIRS:
        if directory.exists():
            paths.extend(sorted(directory.glob("**/*.jsonl")))
    for path in paths:
        stats["files"] += 1
        cwd = session_cwd(path)
        if not cwd:
            continue
        try:
            if pathlib.Path(cwd).resolve() != REPO_ROOT:
                continue
        except OSError:
            if cwd != str(REPO_ROOT):
                continue
        stats["team_files"] += 1
        for line_number, item in json_lines(path):
            stats["lines"] += 1
            timestamp = item.get("timestamp")
            if not isinstance(timestamp, str):
                continue
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            text = ""
            source = "import-codex-history"
            if item.get("type") == "event_msg" and payload.get("type") == "user_message":
                message = payload.get("message")
                if isinstance(message, str):
                    text = message
            elif item.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
                text = content_to_text(payload.get("content"))
            elif item.get("type") == "compacted":
                replacement = payload.get("replacement_history")
                if isinstance(replacement, list):
                    for index, message in enumerate(replacement, 1):
                        if not isinstance(message, dict) or message.get("role") != "user":
                            continue
                        compacted_text = content_to_text(message.get("content"))
                        compacted_text = clean_prompt_text(compacted_text)
                        if should_skip(compacted_text):
                            stats["skipped_meta"] += 1
                            continue
                        stats["candidates"] += 1
                        events.append(event("import-codex-compacted", path, line_number * 1000 + index, timestamp, compacted_text))
                continue
            if should_skip(text):
                if text:
                    stats["skipped_meta"] += 1
                continue
            text = clean_prompt_text(text)
            if should_skip(text):
                stats["skipped_meta"] += 1
                continue
            stats["candidates"] += 1
            events.append(event(source, path, line_number, timestamp, text))
    return events, stats


def has_secret(value: str) -> bool:
    return any(pattern.search(value) for pattern in SECRET_PATTERNS)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import local prompt history into prompt-timeline.")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    existing_events = record_event.load_events()
    existing_norm = {
        normalize(redact(str(item.get("prompt_original") or item.get("prompt_preview") or "")))
        for item in existing_events
        if item.get("kind") == "prompt"
    }
    existing_ids = {str(item.get("id")) for item in existing_events if item.get("id")}

    claude_events, claude_stats = iter_claude()
    codex_events, codex_stats = iter_codex()
    candidates = sorted(claude_events + codex_events, key=lambda item: (item.get("timestamp", ""), item.get("id", "")))

    additions: list[dict] = []
    seen = set(existing_norm)
    redacted_secret_count = 0
    for item in candidates:
        text = str(item.get("prompt_original") or "")
        if has_secret(text):
            redacted_secret_count += 1
            item["prompt_original"] = redact(text)
            item["prompt_preview"] = redact(text)
        key = normalize(str(item.get("prompt_original") or item.get("prompt_preview") or ""))
        if not key or key in seen or str(item.get("id")) in existing_ids:
            continue
        seen.add(key)
        if not item.get("id"):
            item["id"] = f"import-{uuid.uuid4().hex[:16]}"
        additions.append(item)

    if not args.dry_run and additions:
        record_event.DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
        with record_event.FileLock(record_event.LOCK_PATH, timeout_seconds=10):
            with record_event.DATA_PATH.open("a", encoding="utf-8") as handle:
                for item in additions:
                    handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
            record_event.write_events_js(record_event.load_events())
    elif not args.dry_run:
        record_event.write_events_js(existing_events)

    earliest = additions[0]["timestamp_jst"] if additions else ""
    latest = additions[-1]["timestamp_jst"] if additions else ""
    print(json.dumps({
        "dry_run": args.dry_run,
        "repo": str(REPO_ROOT),
        "claude": claude_stats,
        "codex": codex_stats,
        "candidate_events": len(candidates),
        "added_events": len(additions),
        "redacted_secret_like_events": redacted_secret_count,
        "earliest_added_jst": earliest,
        "latest_added_jst": latest,
        "timeline_data": str(record_event.DATA_PATH),
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
