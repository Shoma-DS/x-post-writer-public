# Xブックマークから下書き生成を定時自動化するオーケストレータ。
# Codex を優先し、失敗や token/context 制限時は Claude Code にフォールバックする。
# 保存、state 管理、Discord 通知、画像プロンプトファイル保存はこのスクリプトが担当する。

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from draft_manager import get_db_conn, save_draft, update_draft_image
from runtime_store import (
    get_logs_dir,
    load_processed_bookmarks,
    save_bookmark_status,
    save_processed_bookmarks,
    save_draft_metadata,
    write_image_prompt_file,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).parent
FETCH_BOOKMARKS_SCRIPT = SCRIPT_DIR / "fetch_bookmarks.py"
BOOKMARKS_FILE = SCRIPT_DIR / "bookmarks_latest.json"
CONFIG_FILE = SCRIPT_DIR / "accounts_config.json"
SCHEMA_FILE = SCRIPT_DIR / "draft_generation_schema.json"
CLAUDE_SETTINGS_FILE = REPO_ROOT / ".claude" / "settings.local.json"
DEFAULT_PREVIEW_URL = "https://your-domain.ngrok-free.dev"
DISCORD_WEBHOOK_ENV = "DISCORD_WEBHOOK_X_DRAFT"
TOKEN_LIMIT_PATTERNS = (
    r"token limit",
    r"context length",
    r"maximum context length",
    r"too many tokens",
    r"prompt is too long",
)


def normalize_x_profile_image_url(url: str | None) -> str:
    """Xのプロフィール画像URLを、画像生成の参照に使いやすい高解像度URLへ寄せる。"""
    if not url:
        return ""
    return str(url).replace("_normal.", "_400x400.")


class JobError(RuntimeError):
    pass


_IS_TTY = sys.stdout.isatty()


class _Spinner:
    """ステップ実行中にスピナーを表示し、完了/失敗を1行で上書きする。"""

    _FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, message: str) -> None:
        self._msg = message
        self._ok_msg: str | None = None
        self._warn_msg: str | None = None
        self._running = False
        self._thread: threading.Thread | None = None

    def update(self, message: str) -> None:
        self._msg = message

    def finish_ok(self, message: str) -> None:
        self._ok_msg = message
        self._warn_msg = None

    def finish_warn(self, message: str) -> None:
        self._warn_msg = message
        self._ok_msg = None

    def __enter__(self) -> "_Spinner":
        if _IS_TTY:
            self._running = True
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        else:
            print(f"  → {self._msg}", flush=True)
        return self

    def __exit__(self, exc_type, *_):
        self._running = False
        if self._thread:
            self._thread.join()
        if _IS_TTY:
            if exc_type is None and self._warn_msg:
                icon = "⚠️"
                label = self._warn_msg
            else:
                icon = "✅" if exc_type is None else "❌"
                label = self._ok_msg if (exc_type is None and self._ok_msg) else self._msg
            sys.stdout.write(f"\r\033[K{icon} {label}\n")
            sys.stdout.flush()
        elif exc_type is None and self._warn_msg:
            print(f"  ⚠️  {self._warn_msg}", flush=True)
        elif exc_type is None and self._ok_msg:
            print(f"  ✅ {self._ok_msg}", flush=True)

    def _spin(self) -> None:
        i = 0
        while self._running:
            frame = self._FRAMES[i % len(self._FRAMES)]
            sys.stdout.write(f"\r{frame} {self._msg}")
            sys.stdout.flush()
            time.sleep(0.08)
            i += 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_path(name: str) -> Path:
    return get_logs_dir() / name


def write_log(name: str, content: str) -> Path:
    path = log_path(name)
    path.write_text(content, encoding="utf-8")
    return path


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_json_with_comments(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    json_text = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("//")
    )
    return json.loads(json_text)


def load_dotenv(path: Path = REPO_ROOT / ".env") -> dict[str, str]:
    if not path.exists():
        return {}

    loaded: dict[str, str] = {}
    pattern = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        match = pattern.match(raw_line)
        if not match:
            continue
        key, raw_value = match.groups()
        raw_value = raw_value.strip()
        try:
            parts = shlex.split(raw_value, comments=True, posix=True)
            value = parts[0] if parts else ""
        except ValueError:
            value = raw_value.strip("\"'")
        loaded[key] = value
    return loaded


def truncate(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def load_claude_env() -> dict[str, str]:
    if not CLAUDE_SETTINGS_FILE.exists():
        return {}

    config = json.loads(CLAUDE_SETTINGS_FILE.read_text(encoding="utf-8"))
    env = config.get("env")
    if not isinstance(env, dict):
        return {}

    loaded: dict[str, str] = {}
    for key, value in env.items():
        if isinstance(key, str):
            loaded[key] = value if isinstance(value, str) else str(value)
    return loaded


def build_env() -> dict[str, str]:
    env = os.environ.copy()
    for key, value in load_claude_env().items():
        env.setdefault(key, value)
    env.update(load_dotenv())
    env.setdefault("X_POST_WRITER_ROOT", str(REPO_ROOT))
    home = Path.home()
    pyenv_root = Path(env.get("PYENV_ROOT") or home / ".pyenv")
    path_additions = [
        home / ".local" / "bin",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
        pyenv_root / "shims",
        pyenv_root / "bin",
    ]
    path_parts = [str(path) for path in path_additions] + (env.get("PATH") or "").split(":")
    env["PATH"] = ":".join(dict.fromkeys(part for part in path_parts if part))
    return env


def run_process(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    input_text: str | None = None,
    timeout: int = 900,
) -> tuple[int, str, str, bool]:
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    timed_out = False
    try:
        stdout, stderr = proc.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.terminate()
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
    return proc.returncode or 0, stdout, stderr, timed_out


def _run_streaming(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    input_text: str | None = None,
    timeout: int = 900,
    on_stdout_line=None,
) -> tuple[int, str, str, bool]:
    """stdout を行単位でリアルタイム読み取りしながら実行する。on_stdout_line(line) で各行を受け取れる。"""
    proc = subprocess.Popen(
        cmd, cwd=str(cwd), env=env,
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    timed_out = False

    def _kill() -> None:
        nonlocal timed_out
        timed_out = True
        try:
            proc.terminate()
        except OSError:
            pass

    timer = threading.Timer(timeout, _kill)
    timer.start()

    if input_text is not None:
        try:
            proc.stdin.write(input_text)
            proc.stdin.close()
        except BrokenPipeError:
            pass

    stderr_buf: list[str] = []

    def _read_stderr() -> None:
        for ln in proc.stderr:
            stderr_buf.append(ln)

    stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
    stderr_thread.start()

    stdout_parts: list[str] = []
    for ln in proc.stdout:
        stdout_parts.append(ln)
        if on_stdout_line:
            on_stdout_line(ln)

    stderr_thread.join(timeout=5)
    timer.cancel()
    proc.wait()

    return proc.returncode or 0, "".join(stdout_parts), "".join(stderr_buf), timed_out


def contains_token_limit(text: str) -> bool:
    lowered = text.lower()
    return any(re.search(pattern, lowered) for pattern in TOKEN_LIMIT_PATTERNS)


def fetch_bookmarks_payload(args, env: dict[str, str]) -> dict:
    if args.use_latest:
        if not BOOKMARKS_FILE.exists():
            raise JobError(f"bookmarks_latest.json が見つかりません: {BOOKMARKS_FILE}")
        return load_json(BOOKMARKS_FILE)

    cmd = [
        sys.executable,
        str(FETCH_BOOKMARKS_SCRIPT),
        "--account",
        args.account,
        "--count",
        str(args.count),
    ]
    if args.refresh_account_file:
        cmd.append("--refresh-account-file")

    code, stdout, stderr, timed_out = run_process(
        cmd,
        cwd=REPO_ROOT,
        env=env,
        timeout=args.fetch_timeout,
    )
    write_log("fetch_bookmarks.stdout.log", stdout)
    write_log("fetch_bookmarks.stderr.log", stderr)

    if timed_out:
        raise JobError("fetch_bookmarks.py がタイムアウトしました")
    if code != 0:
        raise JobError(f"fetch_bookmarks.py が失敗しました: {stderr.strip() or stdout.strip()}")
    if not BOOKMARKS_FILE.exists():
        raise JobError(f"bookmarks_latest.json が見つかりません: {BOOKMARKS_FILE}")
    return load_json(BOOKMARKS_FILE)


def fetch_db_drafts_payload(args) -> dict:
    """Neon DBの既存下書きを、生成プロンプト用のbookmark互換payloadへ変換する。"""
    account_cfg = next(
        (a for a in load_json_with_comments(CONFIG_FILE)["accounts"] if a["id"] == args.account),
        None,
    )
    if not account_cfg:
        raise JobError(f"accounts_config.json に account={args.account} が見つかりません")

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.draft_id, d.memo, d.created_at, a.x_username
                 FROM drafts d
                 JOIN accounts a ON a.account_id = d.account_id
                 WHERE a.x_username = %s
                   AND d.status = 'draft'
                   AND (d.memo IS NULL OR d.memo NOT LIKE 'rewrite from draft %%')
                 ORDER BY d.created_at ASC
                """,
                (account_cfg["x_username"],),
            )
            rows = cur.fetchall()

            bookmarks: list[dict] = []
            for draft_id, memo, created_at, x_username in rows:
                cur.execute(
                    """
                    SELECT position, content, image_url
                      FROM draft_parts
                     WHERE draft_id = %s
                     ORDER BY position ASC
                    """,
                    (draft_id,),
                )
                parts = cur.fetchall()
                text_parts = [str(content or "").strip() for _pos, content, _image_url in parts if str(content or "").strip()]
                if not text_parts:
                    continue
                bookmarks.append(
                    {
                        "tweet_id": f"draft-{draft_id}",
                        "author_username": f"{x_username}_db_draft",
                        "text": "\n---\n".join(text_parts),
                        "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at),
                        "conversation_id": f"draft-{draft_id}",
                        "thread_parts": [
                            {"tweet_id": f"draft-{draft_id}-p{position}", "text": str(content or "")}
                            for position, content, _image_url in parts
                            if str(content or "").strip()
                        ],
                        "media": [
                            {"type": "photo", "url": image_url}
                            for _position, _content, image_url in parts
                            if image_url
                        ],
                        "source_type": "db_draft",
                        "source_draft_id": str(draft_id),
                        "source_memo": memo or "",
                    }
                )
    finally:
        conn.close()

    account_profile = {}
    existing_payload = load_json(BOOKMARKS_FILE) if BOOKMARKS_FILE.exists() else {}
    if existing_payload.get("account") == args.account:
        account_profile = existing_payload.get("account_profile") or {}

    return {
        "source": "db_drafts",
        "account": args.account,
        "x_username": account_cfg["x_username"],
        "fetched_at": utc_now(),
        "account_profile": account_profile,
        "bookmarks": bookmarks,
    }


def filter_new_bookmarks(bookmarks: list[dict], processed: set[str]) -> tuple[list[dict], list[dict]]:
    new_items: list[dict] = []
    skipped_existing: list[dict] = []

    for bookmark in bookmarks:
        tweet_id = str(bookmark.get("tweet_id") or "").strip()
        if not tweet_id:
            continue
        if tweet_id in processed:
            skipped_existing.append(bookmark)
            continue
        new_items.append(bookmark)

    return new_items, skipped_existing


def _build_research_section(
    research: dict[str, list[dict]] | None,
    bookmarks: list[dict],
) -> str:
    """リサーチ結果をプロンプトに埋め込むセクション文字列を返す。"""
    if not research:
        return ""
    lines = [
        "[web_research]",
        "以下はブックマークの話題について事前にウェブ検索した結果です。",
        "投稿の比較・ノウハウ・背景情報として積極的に活用してください。",
        "",
    ]
    for bm in bookmarks:
        tweet_id = str(bm.get("tweet_id") or "")
        results  = research.get(tweet_id)
        if not results:
            continue
        author = bm.get("author_username", "?")
        lines.append(f"## tweet_id={tweet_id} (@{author})")
        for i, r in enumerate(results, 1):
            lines.append(f"  [{i}] {r['title']}")
            lines.append(f"      URL: {r['url']}")
            lines.append(f"      概要: {r['snippet'][:200]}")
        lines.append("")
    return "\n".join(lines) + "\n"


def build_generation_prompt(
    *,
    agent_name: str,
    payload: dict,
    bookmarks: list[dict],
    research: dict[str, list[dict]] | None = None,
) -> str:
    account_profile = payload.get("account_profile", {})
    profile_image_url = normalize_x_profile_image_url(account_profile.get("profile_image_url"))
    account_file_path = Path(account_profile.get("account_file_path") or "")
    account_text = account_file_path.read_text(encoding="utf-8") if account_file_path.exists() else ""

    # 全アカウント共通の図解ルールを読み込む
    skill_root = Path(__file__).resolve().parent.parent
    common_rules_path = skill_root / "infographic-rules-common.md"
    common_rules_text = common_rules_path.read_text(encoding="utf-8") if common_rules_path.exists() else ""

    # x-post-writer/accounts/{x_username}/ からアカウント別の図解設定を読み込む
    x_username = (payload.get("x_username") or "").lower().lstrip("@")
    accounts_dir = skill_root / "accounts"
    account_infographic_dir = accounts_dir / x_username
    char_prompt_path = account_infographic_dir / "character-prompt.md"
    rules_path       = account_infographic_dir / "infographic-rules.md"
    character_prompt_text  = char_prompt_path.read_text(encoding="utf-8") if char_prompt_path.exists() else ""
    account_rules_text     = rules_path.read_text(encoding="utf-8")       if rules_path.exists()       else ""
    # 共通ルール + アカウント固有ルールを合成（共通が先）
    infographic_rules_text = "\n\n".join(filter(None, [common_rules_text, account_rules_text]))

    # キャラクター参照画像のパスを検索（PNG/JPG/JPEG）
    char_image_extensions = ["*.png", "*.jpg", "*.jpeg"]
    char_image_files: list[Path] = []
    for ext in char_image_extensions:
        char_image_files.extend(sorted(account_infographic_dir.glob(ext)))
    char_image_path: Path | None = char_image_files[0] if char_image_files else None

    prompt = f"""
あなたは X 投稿自動化ワーカーです。
目的は、Xブックマークから「ぐーたらAI社長（@gutaraAikatuyou）」向けのツリー型下書きを生成し、構造化JSONだけを返すことです。

【基本制約】
- 出力は JSON のみ。Markdown・説明文・コードブロックは禁止。
- 元ポストをそのままコピペしない。切り口・論点・構造だけ借りて、自分ごとに解釈して書く。
- アカウントのトンマナ・口調・NG事項を最優先で守る。
- 各 draft に日本語の縦型3:4（960×1280px）インフォグラフィックプロンプトを1つ付ける（元メディアを使う場合は空文字）。
- 原則として、取得済みの bookmark はすべて `drafts` にして下書き化する。
- `skipped` に回してよいのは、本文がほぼ空で素材が足りない、成人向け・投資助言・自動売買・高額収益保証・危険行為など、アカウントのNGや安全性に明確に抵触する場合だけ。
- 話題が重複している、優先度が低い、開発者寄り、少し読者に遠い、という理由だけで skipped にしない。必ず別切り口に変換して draft にする。
- `source` が `db_drafts` の場合、入力は既存DB下書きです。元の下書きを改善対象として扱い、同じ内容の言い換えではなく、ターゲット・フック・説明粒度を今回のルールに合わせて全面リライトする。
- `source_type: db_draft` の項目は必ず `memo` に `rewrite from draft <source_draft_id>` を含める。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【投稿形式：全件ツリー型（最重要・必須）】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
すべての下書きは 3〜4 パーツのツリー型で生成すること。単発1パーツは禁止。

各パーツの役割（この順番を基本構造にする）:

■ Part 1 ― フック＋要約
  - 1行目で「読まなきゃ損」と思わせる。インパクト最優先。
  - 何が起きているか・何のツールか・どんな体験かを3〜5行で凝縮する。
  - ChatGPT以外のツール名を出す場合は、読者が知らない前提で「ざっくり何をする道具か」を1行で添える。
    例: 「Difyは、AIチャットボットを自分用に作れる道具」
  - 「つまりこういうこと」で締めると読者が次パーツを読む。
  - **最後の1行に必ず次パーツへの短い誘導（10〜20字）を入れること**
    例：「比較してみたら衝撃だった↓」「続きで全部変わる話」「次で一気に解説する」

■ Part 2 ― 比較・仕組み解説
  - 「今まで（従来の方法・よく使われるツール）と何が違うか」を対比で見せる。
  - ツリーで扱う中心ツールについて、必要なら最初に「そもそも何のツールか」を必ず説明する。
    ChatGPTがやっと普及したくらいの世の中という前提で、Claude / Gemini / Dify / n8n / Cursor / AIエージェントは初見でも分かる入口を作る。
  - ChatGPT / Claude / Gemini / Dify / n8n / Cursor / AIエージェント / 普通の音声bot / 手動作業など、具体的なツール名・専門用語は隠さず出す。
  - ただし読者はツール名すら知らない前提で書く。ツール名を初めて出すときは「何をする道具か」を必ず1フレーズで添える。
    例: 「Claude＝文章や資料作りを手伝うAI」「n8n＝作業の流れをつなぐ自動化ツール」
  - 専門用語やツール名を出した直後に、必ずメタファーか一言補足を入れる。
    例: 「AIエージェント＝指示したら作業を進めてくれる新人アシスタントみたいなもの」
  - 初心者が「あ、それなら分かる」「知らなかったけど面白そう」「自分でも触れそう」と感じる瞬間を作る。
  - **最後の1行に必ず次パーツへの短い橋渡しを入れること**
    例：「実際に試したらこうなった↓」「正直ここからが本題」「で、どうなったか」

■ Part 3 ― 実体験スタイルの具体例
  - 「実際に自分が使ったら/試したら/設定したらこうなった」という一人称視点で書く。
  - 架空でも「飲食店のオーナーがこう使ったら」「副業でnoteを書いている人が使ったら」など具体的シナリオを描く。
  - ①②③の番号付きステップ形式か、before/afterの構成にする。
  - 「むしろこっちのほうが気になった」「正直こっちのほうがやばい」という発見トーンを入れる。
  - **Part4がある場合は最後の1行に「まとめるとこういうこと↓」など短い誘導を入れること**

■ Part 4 ― まとめ＋差別化の気づき（情報量が多いときだけ追加）
  - 読者が「これを持ち帰ろう」と思える1つの教訓・視点・行動ヒントで締める。
  - 「AIを使う側に回れるか」「差別化ってこういうこと」という軸でまとめる。
  - ここだけで「読んだ価値あった」と感じさせること。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【情報密度のルール（必須）】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
元投稿のリライトや要約はNG。「元投稿を読んだ人間が自分ごとに解釈して投稿する」スタイルで書く。
以下のうち最低2つを各ドラフトに盛り込む:

  a) 実体験・実感の描写（「実際に試したら〜だった」「使ってみて気づいたのは〜」）
  b) 既存ツール・従来手法との具体的比較（「ChatGPTだと手動だが〜」「ClaudeのArtifactsなら〜」「これまでは外注が必要だったのに〜」）
  c) 非エンジニア・副業初心者に直接刺さるノウハウ（「副業でこれを使うなら〜」「稼げない人がやりがちなのは〜」「最初はこの画面を触ればOK」）
  d) 数字・ステップ・シナリオで「再現できる」感を出す（「①〜②〜③〜 これだけ」）

「これを読んだだけで何か学んだ」と感じさせる情報密度にする。
抽象的な精神論だけで終わらせない。ツール名・機能名・使い道・副業での具体例を必ず入れる。
ただしツール名の羅列は禁止。知らない人でも意味が分かるように「道具名 → 何ができる → 副業でどう使える」の順で説明する。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【フックのルール（必須）】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- インパクト最優先。具体的な数字・逆張り・原因指摘・意外な事実のどれかを必ず使う。
- 元投稿の1行目・冒頭3行・強い言い回しから「なぜ目が止まるか」を先に分解し、
  そのまま写さず、別の心理トリガーへ展開してから Part 1 の1行目を作る。
- 各 bookmark につき、内部で最低5種類のフック案を作ってから最強の1つだけを採用する。
  5種類は以下から重複なく選ぶ:
    1) 逆張り: 常識や思い込みを否定する
    2) 原因指摘: 失敗・伸びない理由をズバッと言う
    3) 体験告白: 「実際にやったら/見たら」の驚きで始める
    4) 数字・期間: 金額/時間/手順数/日数で具体化する
    5) 比較: 従来手法や有名ツールとの差を見せる
    6) 損失回避: 知らないと損・置いていかれる不安を出す
    7) 初心者救済: 「難しくない」「これだけでいい」で安心させる
    8) 中級者への刺し: 量産している人の盲点を突く
- 採用するフックは、元投稿と同じ語順・同じ決め台詞・同じ煽り文を避ける。
- `drafts[].rationale` には、採用したフック型を必ず `hook: 原因指摘` のように短く書く。
- 3層すべてが「自分ごと」に読める切り口を選ぶ:
    初心者: 「え、これだけでいいの？」という意外性・簡単さ
    副業探し層: 「なぜ稼げないか」の原因ズバリ指摘
    量産中級者: 「量産してる人が見落としていること」という気づき
- フックワード例（アカウントの実投稿より）:
    「ちょっとヤバいことに気づいた。」「やばい体験した。」「正直〜ちょっと驚いた。」
    「〜一択。」「勝ち確定ｗ」「フォロワー少なくてもマネタイズできます」
- ただし上の例文を毎回使い回さない。元投稿ごとに語尾・主語・感情の種類を変え、
  同一実行内で Part 1 の1行目が似た形に偏らないようにする。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【ターゲット設計（必須）】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
最重要ターゲットは「非エンジニアで、副業もAIも初心者」の人。
投稿を読んだ後に「AIエージェント触ってみようかな」「AIで副業した方がよさそう」と思わせることを最優先にする。
読者は ChatGPT 以外のツール名をほぼ知らない可能性がある。知らなくても理解でき、むしろ「そんな道具あるんだ、触ってみたい」と興味が湧く形にする。
世の中の前提は「ChatGPTがようやく普及したくらい」。ChatGPT以外のAIツール・AIエージェント・自動化ツールは、初出時に必ず“そもそも何か”を説明する。

以下の3層すべてを1スレッドで取りこぼさない構成にする。ただし比重は 1 と 2 を重くする:
- 非エンジニア完全初心者: AIをほぼ使っていない → 「コードが書けなくても触れる」「これだけでOK」「難しくない」という安心感で引き留める
- 副業初心者層: 稼ごうとしているが何から始めるか分からない → 「AIを使うと作業時間が減る」「小さく試せる」という入口を見せる
- AI量産中級者層: 大量生成しているが差別化できていない → 「量より設計・使い方」という気づきで刺す

避けること:
- エンジニア前提の説明にしない。
- 「API」「ワークフロー」「自動化」「プロンプト」「エージェント」やツール名を説明なしで置かない。
- 「Claudeなら〜」「Difyなら〜」のように、知っている人だけ分かる書き方にしない。
- 逆に「すごい」「便利」「未来」だけで抽象化しすぎない。必ず具体的なツール名・作業例・副業用途へ落とす。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【トーンのルール】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- キャラ: ぐーたら・怠け者・でも結果は出ている。自己矛盾をキャラにしている。
- 口調: カジュアル寄り。「ｗ」「〜だよね」「〜だけど」を適度に使う。断言調。
- 参考トーン @MakeAI_CEO: 専門知識を自信を持って伝えつつ、難しい概念を初心者向けに嚙み砕く。
- 専門用語は使った直後に必ず平易な言葉で補足する（メタファー・アナロジー必須）。
  例: 「API＝アプリ同士の受付窓口」「ワークフロー＝作業の流れをレシピ化したもの」「AIエージェント＝作業を任せられるAIアシスタント」
- 専門用語を避けすぎない。ChatGPT / Claude / Gemini / Cursor / Dify / n8n / API / AIエージェントなど、読者が次に検索できる固有名詞は明示する。
- 固有名詞は「知らない人向けの短い説明」とセットにする。読み終えた人が検索する前から、おおよその価値が分かる状態にする。
- 「正直僕もよく分かってない。笑」のような非エンジニア共感フレーズを適所で使う。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【CTA・フォロー促しのルール】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- noteの固定投稿への定型誘導は一切禁止。
- フォロー促しも定型では入れない。価値提供に振り切る。
- CTAはLINE登録のみ、文脈が自然に合うときだけ最終パーツに1回まで。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【メディアのルール】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- 各ブックマークの `media` フィールドに画像・動画情報がある場合がある。
- 元のメディアをそのまま使う場合は `image_prompt.copy` と `image_prompt.prompt` を空文字にする。
- 自分で画像を作る場合は、以下のアカウント別ルールで **日本語** の縦型インフォグラフィックプロンプトを生成する。

【縦型図解プロンプトルール】
{infographic_rules_text or "（ルールファイルなし — 汎用的な縦型3:4（960×1280px）インフォグラフィックプロンプトを日本語で生成すること）"}

【キャラクター情報】
{character_prompt_text or "（キャラクター情報なし）"}

【キャラクター参照画像】
第1優先: アカウントのプロフィール画像URLをキャラクター参照として使うこと。
{profile_image_url or "（プロフィール画像URLなし）"}

第2優先: ローカルのキャラクター参照画像。
{f"以下の画像ファイルを Read ツールで読み込み、キャラクターの外見を実際に確認してから画像プロンプトに組み込んでください:{chr(10)}{str(char_image_path)}" if char_image_path and char_image_path.exists() else "（画像ファイルなし — character-prompt.md のテキスト説明のみ参照すること）"}

画像プロンプトには必ず次を明記すること。
- プロフィール画像URLをキャラクター参照として使う
- 同じ人物/マスコットの特徴を保つ
- 重要特徴: 半目、眠そうな表情、舌を少し出す、頬杖、黒い短髪、黒いスーツ、白シャツ、赤ネクタイ
- キャラクターは図解の右下など20〜25%以内に置き、主役は図解にする
- 明るい丸目の少年、パーカー、元気な笑顔、指差しポーズは禁止

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

アカウントID: {payload.get("account")}
Xユーザー名: {payload.get("x_username")}
利用エージェント: {agent_name}

[account_profile]
{json.dumps(account_profile, ensure_ascii=False, indent=2)}

[account_file]
{account_text}

[new_bookmarks]
{json.dumps(bookmarks, ensure_ascii=False, indent=2)}

{_build_research_section(research, bookmarks)}
返す JSON の意味:
- `run_summary`: 今回の判断の短い要約
- `drafts[].memo`: 例 `from @author — タイトル`
- `drafts[].rationale`: なぜこの構成にしたかを1〜2文
- `drafts[].image_prompt.copy`: 画像に入れる短い日本語コピー・キャッチコピー（元メディアをそのまま使う場合は空文字）
- `drafts[].image_prompt.prompt`: 日本語の縦型3:4（960×1280px）インフォグラフィックプロンプト（上記【縦型図解プロンプトルール】に従う、元メディアをそのまま使う場合は空文字）
- `skipped[].reason`: スキップ理由
""".strip()
    return prompt


def validate_thread_structure(result: dict, bookmarks: list[dict]) -> list[dict]:
    """生成された下書きが全件ツリー型になっているか検証する。単発1パーツは全件警告。"""
    bm_map = {str(bm.get("tweet_id")): bm for bm in bookmarks}
    warnings: list[dict] = []
    for item in result.get("drafts", []):
        tweet_id    = str(item.get("bookmark_tweet_id") or "")
        bm          = bm_map.get(tweet_id) or {}
        draft_parts = len(item.get("parts") or [])
        src_parts   = len(bm.get("thread_parts") or [])
        if draft_parts < 2:
            warnings.append({
                "tweet_id":    tweet_id,
                "author":      item.get("author_username", "?"),
                "src_parts":   src_parts,
                "draft_parts": draft_parts,
                "issue":       "単発（ツリー3〜4パーツ必須）",
            })
    return warnings


_NG_PATTERNS = [
    (r"note[でを]?公開中", "note誘導"),
    (r"noteはこちら", "note誘導"),
    (r"フォロー(?:をお願い|推奨|してね)", "定型フォロー促し"),
    (r"いいね[・＆&]リポスト", "エンゲージメント乞い"),
]

X_FREE_ACCOUNT_UNIT_LIMIT = 280
X_URL_WEIGHT = 23
_X_URL_PATTERN = re.compile(r"(?:https?://|www\.)[^\s<>\"']+")


def _is_fullwidth_codepoint(code_point: int) -> bool:
    return (
        0x1100 <= code_point <= 0x11FF
        or 0x2E80 <= code_point <= 0xA4CF
        or 0xAC00 <= code_point <= 0xD7AF
        or 0xF900 <= code_point <= 0xFAFF
        or 0xFE10 <= code_point <= 0xFE6F
        or 0xFF01 <= code_point <= 0xFF60
        or 0xFFE0 <= code_point <= 0xFFE6
    )


def _is_emoji_base(code_point: int) -> bool:
    return (
        0x1F000 <= code_point <= 0x1FAFF
        or 0x2600 <= code_point <= 0x27BF
    )


def _x_text_units_without_urls(text: str) -> int:
    units = 0
    i = 0
    chars = list(text)
    while i < len(chars):
        code_point = ord(chars[i])
        if chars[i].isspace():
            units += 1
        elif _is_emoji_base(code_point):
            units += 1
            i += 1
            while i < len(chars):
                next_point = ord(chars[i])
                if next_point in (0x200D, 0xFE0E, 0xFE0F) or 0x1F3FB <= next_point <= 0x1F3FF:
                    i += 1
                    continue
                if i > 0 and ord(chars[i - 1]) == 0x200D and _is_emoji_base(next_point):
                    i += 1
                    continue
                break
            continue
        elif _is_fullwidth_codepoint(code_point):
            units += 2
        else:
            units += 1
        i += 1
    return units


def x_free_account_text_units(text: str) -> int:
    """無料X通常ポスト換算。全角=2、半角/改行/絵文字=1、URL=23。"""
    value = str(text or "")
    units = 0
    cursor = 0
    for match in _X_URL_PATTERN.finditer(value):
        units += _x_text_units_without_urls(value[cursor:match.start()])
        units += X_URL_WEIGHT
        cursor = match.end()
    units += _x_text_units_without_urls(value[cursor:])
    return units


def validate_draft_quality(result: dict) -> list[dict]:
    """生成された下書きの品質チェック（スレッド・NGパターン）。"""
    import re as _re
    issues: list[dict] = []
    check_char_limit = os.environ.get("X_POST_CHECK_CHAR_LIMIT", "").lower() in {"1", "true", "yes", "on"}
    char_limit = int(os.environ.get("X_POST_CHAR_LIMIT", str(X_FREE_ACCOUNT_UNIT_LIMIT)) or X_FREE_ACCOUNT_UNIT_LIMIT)

    for item in result.get("drafts", []):
        author  = item.get("author_username", "?")
        tweet_id = str(item.get("bookmark_tweet_id") or "")
        parts   = item.get("parts") or []
        part_issues: list[str] = []

        # 単発チェック（validate_thread_structure と二重になるが独立してログ）
        if len(parts) < 2:
            part_issues.append(f"単発({len(parts)}パーツ) — ツリー3〜4パーツ必須")

        for i, text in enumerate(parts, 1):
            if check_char_limit:
                text_units = x_free_account_text_units(text)
                if text_units > char_limit:
                    part_issues.append(f"P{i}: 無料X換算{text_units}/{char_limit}（全角140/半角280相当・URL=23）")
            for pattern, label in _NG_PATTERNS:
                if _re.search(pattern, text):
                    part_issues.append(f"P{i}: NGパターン「{label}」")

        if part_issues:
            issues.append({
                "tweet_id": tweet_id,
                "author":   author,
                "issues":   part_issues,
            })

    return issues


def _draft_texts(parts: list) -> list[str]:
    texts: list[str] = []
    for part in parts or []:
        if isinstance(part, dict):
            texts.append(str(part.get("content") or ""))
        else:
            texts.append(str(part or ""))
    return texts


def _count_matches(text: str, patterns: list[str]) -> int:
    return sum(1 for pattern in patterns if re.search(pattern, text, re.IGNORECASE))


def _clamp_score(value: int, max_value: int = 20) -> int:
    return max(0, min(max_value, value))


def _known_tool_signals(text: str) -> list[str]:
    known = re.findall(
        r"\b(?:ChatGPT|Claude|Codex|OpenAI|GitHub|Remotion|HyperFrames|Editframe|Threads|Instagram|Blender|Unity|Cursor|Hacker News)\b",
        text,
    )
    latin_brands = re.findall(r"\b[A-Z][A-Za-z0-9]{2,}(?:[A-Z][A-Za-z0-9]+)?\b", text)
    seen: set[str] = set()
    result: list[str] = []
    for value in [*known, *latin_brands]:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result[:8]


def _criterion_insight(item: dict) -> dict:
    table = {
        "hook": {
            "good": "冒頭で疑問・数字・違和感を出せていて、スクロールを止める力があります。",
            "improve": "1行目に「数字」「逆張り」「知らないと損」のどれかを明確に入れると、さらに止まりやすくなります。",
            "rule": "投稿の1行目は、数字・逆張り・損失回避・原因指摘のいずれかを必ず入れ、読者が続きを見たくなる問いで始める。",
        },
        "target": {
            "good": "誰に向けた話かが読み取りやすく、読者が自分ごと化しやすい構成です。",
            "improve": "初心者・副業層・AI量産中級者のどの層に一番刺す投稿なのかを、冒頭かPart 2でより明示すると強くなります。",
            "rule": "投稿ごとに最重要ターゲットを1つ決め、冒頭またはPart 2で「誰のどんな悩みの話か」を明示する。",
        },
        "painBenefit": {
            "good": "課題と得られる変化がつながっていて、読む理由を作れています。",
            "improve": "読者の痛みをもう一段具体化し、「それを放置すると何を損するか」まで書くと刺さりやすくなります。",
            "rule": "本文では読者の痛み、放置した場合の損、得られる変化をセットで書く。",
        },
        "specificity": {
            "good": "固有名詞・数字・比較軸があり、情報としての信頼感があります。",
            "improve": "数字、ツール名、使う場面、比較対象をもう1つ足すと、抽象論から実用情報に寄ります。",
            "rule": "抽象的な主張で終わらせず、数字・固有名詞・具体的な使用場面・比較対象のうち最低2つを入れる。",
        },
        "flow": {
            "good": "ツリーの流れが読みやすく、次の投稿へ進む理由が作れています。",
            "improve": "各パートの最後に短い橋渡しを入れ、重複表現を削ると最後まで読まれやすくなります。",
            "rule": "ツリー投稿では各パート末尾に次を読みたくなる短い橋渡しを置き、同じ説明の繰り返しは削る。",
        },
        "action": {
            "good": "次に見る・試す・保存するなどの行動導線があります。",
            "improve": "最後に「保存」「試す」「比較する」など、読後にやることを1つだけ明確にすると反応が取りやすくなります。",
            "rule": "最終パートでは読者の次アクションを1つに絞り、保存・試す・比較・リプ確認など自然な導線で締める。",
        },
    }
    key = str(item.get("key") or "")
    insight = table.get(key) or {
        "good": f"{item.get('label') or '項目'}は一定の水準を満たしています。",
        "improve": f"{item.get('label') or '項目'}をもう少し具体化すると投稿全体の説得力が上がります。",
        "rule": f"{item.get('label') or '項目'}を次回以降の投稿でも具体的に確認する。",
    }
    max_value = int(item.get("max") or 20)
    score = int(item.get("score") or 0)
    return {
        "id": key or str(item.get("label") or "criterion"),
        "criterion_key": key,
        "title": str(item.get("label") or "採点項目"),
        "score": score,
        "max": max_value,
        "detail": insight["good"] if max_value and score / max_value >= 0.8 else insight["improve"],
        "good": insight["good"],
        "improvement": insight["improve"],
        "rule": insight["rule"],
    }


def _build_score_report(criteria: list[dict], total_score: int) -> dict:
    items = [_criterion_insight(item) for item in criteria]
    strengths = sorted(
        [item for item in items if item["max"] and item["score"] / item["max"] >= 0.8],
        key=lambda item: item["score"] / item["max"],
        reverse=True,
    )[:4]
    if not strengths:
        strengths = sorted(items, key=lambda item: item["score"] / item["max"], reverse=True)[:2]
    weak_items = sorted(
        [item for item in items if item["max"] and item["score"] / item["max"] < 0.8],
        key=lambda item: item["score"] / item["max"],
    )
    if not weak_items:
        weak_items = sorted(items, key=lambda item: item["score"] / item["max"])[:3]
    overview = (
        "全体として投稿の引きが強く、読者が続きを読む理由も作れています。反応をさらに伸ばすなら、弱い項目だけを運用ルールへ戻すのが効率的です。"
        if total_score >= 82
        else "土台はできていますが、刺す対象・読後アクション・具体例のどれかを強めると反応が伸びやすい状態です。"
        if total_score >= 68
        else "現状は読み手の自分ごと化が弱くなりやすいので、フック・ターゲット・具体性を優先して改善したい状態です。"
    )
    return {
        "overview": overview,
        "strengths": [
            {
                "id": item["id"],
                "title": item["title"],
                "score": item["score"],
                "max": item["max"],
                "detail": item["good"],
            }
            for item in strengths
        ],
        "improvement_points": [
            {
                "id": item["id"],
                "criterion_key": item["criterion_key"],
                "title": item["title"],
                "score": item["score"],
                "max": item["max"],
                "detail": item["improvement"],
                "rule": item["rule"],
                "selected": True,
            }
            for item in weak_items
        ],
    }


def _repeated_text_signals(texts: list[str]) -> list[str]:
    chunks: list[str] = []
    for text in texts:
        normalized = re.sub(r"https?://\S+", "", text)
        for chunk in re.split(r"[！？!?。．\n]+", normalized):
            value = chunk.strip()
            if len(value) >= 10:
                chunks.append(value)
    seen: set[str] = set()
    repeated: list[str] = []
    for chunk in chunks:
        key = re.sub(r"\s+", "", chunk).lower()
        if key in seen and chunk not in repeated:
            repeated.append(chunk)
        seen.add(key)
    return repeated[:3]


def score_draft_for_audience(parts: list, image_prompt: dict | None = None, memo: str = "") -> dict:
    """フック・ターゲット・具体性などから、刺さり度を概算する軽量スコア。"""
    texts = _draft_texts(parts)
    first_text = (texts[0] if texts else "").strip()
    full_text = "\n".join(texts)
    normalized = re.sub(r"\s+", " ", full_text)
    image_prompt = image_prompt or {}
    prompt_text = " ".join(str(image_prompt.get(key) or "") for key in ("copy", "prompt"))
    combined = " ".join(value for value in [normalized, prompt_text, memo] if value)
    tools = _known_tool_signals(combined)
    repeated = _repeated_text_signals(texts)

    hook_hits = _count_matches(first_text, [
        r"[？?]",
        r"(損|危険|注意|まだ|実は|知ら|なぜ|違う|遠回り|言い訳|比較|全部|まるっと)",
        r"(\d|万|円|%|倍|AI|API)",
    ])
    target_hits = _count_matches(combined, [
        r"(AI|SNS|X|投稿|運用|インスタ|Threads|マーケ|営業|制作|開発|個人|副業|経営|フリーランス)",
        r"(人|担当|社長|クリエイター|開発者|マーケター|店舗|チーム)",
        r"(伸びない|時間がない|毎回|手作業|課金|コスト|探す|作る|管理)",
    ])
    pain_benefit_hits = _count_matches(combined, [
        r"(損|悩|詰ま|面倒|消耗|遅い|高い|失敗|遠回り|課題|問題)",
        r"(任せ|自動|削減|増や|伸ば|差別化|時短|勝ち|資産|ラク|効率)",
        r"(だから|つまり|結論|要するに|ポイント|大事|先に)",
    ])
    specificity_patterns = [
        r"(\d|万|円|%|倍|件|ステップ|つ|個)",
        r"(月額|買い切り|無料|有料|URL|ツール|API|ライブラリ|フレームワーク)",
    ]
    if tools:
        specificity_patterns.append("|".join(re.escape(value) for value in tools))
    specificity_hits = _count_matches(combined, specificity_patterns)
    action_hits = _count_matches(combined, [
        r"(次|まず|やる|使う|試す|保存|チェック|見る|置いて|リプ|詳しく|導線)",
        r"(ステップ|手順|方法|比較|リスト|テンプレ|URL)",
    ])
    flow_base = 10 if 3 <= len(texts) <= 7 else 5 if len(texts) == 1 else 8
    flow_penalty = 4 if repeated else 0
    density_penalty = 3 if any(len(text) > 700 for text in texts) else 0

    criteria = [
        {
            "key": "hook",
            "label": "フック",
            "score": _clamp_score(8 + hook_hits * 4),
            "max": 20,
            "detail": "冒頭に疑問・違和感・数字があり止まりやすい" if hook_hits >= 2 else "冒頭の引っかかりをもう少し強くできる",
        },
        {
            "key": "target",
            "label": "ターゲット適合",
            "score": _clamp_score(6 + target_hits * 4),
            "max": 20,
            "detail": "誰の課題かが本文から読み取りやすい" if target_hits >= 2 else "誰向けの話かをもう一段具体化したい",
        },
        {
            "key": "painBenefit",
            "label": "痛みと得",
            "score": _clamp_score(6 + pain_benefit_hits * 4),
            "max": 20,
            "detail": "課題と得られる変化がつながっている" if pain_benefit_hits >= 2 else "悩みとベネフィットの接続を強めたい",
        },
        {
            "key": "specificity",
            "label": "具体性",
            "score": _clamp_score(6 + specificity_hits * 4),
            "max": 20,
            "detail": f"固有名詞・数字がある: {', '.join(tools[:3])}" if tools else "数字・固有名詞・比較軸を足すと信頼感が上がる",
        },
        {
            "key": "flow",
            "label": "読み進めやすさ",
            "score": _clamp_score(flow_base + 10 - flow_penalty - density_penalty),
            "max": 20,
            "detail": "重複表現が少し目立つ" if repeated else "構成は読み進めやすい範囲",
        },
        {
            "key": "action",
            "label": "行動導線",
            "score": _clamp_score(5 + action_hits * 5),
            "max": 20,
            "detail": "次に何を見る/試すかの導線がある" if action_hits else "保存・確認・試すなどの次アクションが弱い",
        },
    ]
    total_score = sum(int(item["score"]) for item in criteria)
    total_max = sum(int(item["max"]) for item in criteria) or 1
    weighted_total = round(total_score / total_max * 100)
    level = "ok" if weighted_total >= 82 else "warn" if weighted_total >= 68 else "danger"
    label = "強い" if weighted_total >= 82 else "改善余地" if weighted_total >= 68 else "弱い"
    return {
        "score": weighted_total,
        "level": level,
        "label": label,
        "criteria": criteria,
        "report": _build_score_report(criteria, weighted_total),
        "scored_at": utc_now(),
        "summary": f"刺さり度 {weighted_total} / {label}",
    }


_RESEARCH_PER_RUN = 5   # 1回の実行でリサーチするブックマークの上限（レート制限対策）
_SEARCH_RESULTS   = 4   # 1クエリあたり取得する件数


def _extract_search_query(bookmark: dict) -> str:
    """ブックマークのテキストから検索クエリを抽出する。"""
    text = (bookmark.get("text") or "").strip()
    # thread_parts の最初のパーツを優先
    parts = bookmark.get("thread_parts") or []
    if parts:
        text = parts[0].get("text", text)

    # URL・@メンション・ハッシュタグを除去
    clean = re.sub(r"https?://\S+", "", text)
    clean = re.sub(r"@\w+", "", clean)
    clean = re.sub(r"#\S+", "", clean)
    clean = " ".join(clean.split())

    # 最初の 80 文字を使ってキーワード的なクエリにする
    query_base = clean[:80].strip()
    return f"{query_base} AI 活用 解説" if query_base else "AI 活用 最新情報"


def _ddg_search(query: str, max_results: int = _SEARCH_RESULTS) -> list[dict]:
    """DuckDuckGo でウェブ検索し、タイトル・URL・スニペットのリストを返す。"""
    try:
        from ddgs import DDGS  # type: ignore
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results, region="jp-ja"))
        return [
            {"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")}
            for r in results
        ]
    except Exception as e:
        print(f"  ⚠️  DuckDuckGo 検索失敗（{e}）", flush=True)
        return []


def research_bookmarks(bookmarks: list[dict]) -> dict[str, list[dict]]:
    """
    各ブックマークについてウェブ検索を行い、
    tweet_id → 検索結果リスト のマップを返す。
    上限 _RESEARCH_PER_RUN 件まで処理する。
    """
    research: dict[str, list[dict]] = {}
    count = 0
    for bm in bookmarks:
        tweet_id = str(bm.get("tweet_id") or "")
        if not tweet_id:
            continue
        if count >= _RESEARCH_PER_RUN:
            break
        query   = _extract_search_query(bm)
        results = _ddg_search(query)
        if results:
            research[tweet_id] = results
            author = bm.get("author_username", "?")
            print(f"  🔍 リサーチ: @{author} → {len(results)} 件 ({query[:40]}…)", flush=True)
        count += 1
    return research


def parse_json_output(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def run_codex(
    prompt: str,
    env: dict[str, str],
    timeout: int,
    *,
    on_progress=None,
    total: int = 0,
) -> tuple[dict | None, str]:
    codex_path = shutil.which("codex", path=env.get("PATH"))
    if not codex_path:
        return None, "codex コマンドが見つかりません"

    output_path = get_logs_dir() / "codex_last_message.json"
    cmd = [
        codex_path,
        "exec",
        "-C",
        str(REPO_ROOT),
        "--sandbox",
        "read-only",
        "--color",
        "never",
        "--output-schema",
        str(SCHEMA_FILE),
        "-o",
        str(output_path),
        "-",
    ]

    draft_seen = 0

    def on_line(ln: str) -> None:
        nonlocal draft_seen
        count = ln.count('"bookmark_tweet_id"')
        if count and on_progress:
            draft_seen = min(draft_seen + count, total)
            on_progress(draft_seen, total)

    code, stdout, stderr, timed_out = _run_streaming(
        cmd,
        cwd=REPO_ROOT,
        env=env,
        input_text=prompt,
        timeout=timeout,
        on_stdout_line=on_line,
    )
    write_log("codex.stdout.log", stdout)
    write_log("codex.stderr.log", stderr)

    combined = "\n".join([stdout, stderr]).strip()
    if timed_out:
        return None, "codex 実行がタイムアウトしたため終了しました"
    if contains_token_limit(combined):
        return None, f"codex token/context 制限: {truncate(combined, 400)}"
    if code != 0:
        return None, truncate(combined, 400) or f"codex 実行失敗 (exit={code})"

    raw = output_path.read_text(encoding="utf-8") if output_path.exists() else stdout
    try:
        return parse_json_output(raw), "ok"
    except json.JSONDecodeError as exc:
        return None, f"codex の JSON 解析に失敗しました: {exc}"


def run_claude(
    prompt: str,
    env: dict[str, str],
    timeout: int,
    *,
    on_progress=None,
    total: int = 0,
) -> tuple[dict, str]:
    claude_path = shutil.which("claude", path=env.get("PATH"))
    if not claude_path:
        raise JobError("claude コマンドが見つかりません")

    schema_text = SCHEMA_FILE.read_text(encoding="utf-8")
    # stream-json で中間出力をリアルタイム受信し、draft の進捗をカウントする（--verbose 必須）
    cmd = [
        claude_path,
        "-p",
        "--verbose",
        "--setting-sources",
        "local",
        "--permission-mode",
        "dontAsk",
        "--output-format",
        "stream-json",
        "--json-schema",
        schema_text,
        "--tools",
        "",
    ]

    accumulated_text = ""
    draft_seen = 0
    result_text = ""
    result_error = ""
    tool_use_input: dict | None = None  # --json-schema 使用時の構造化出力

    def on_line(ln: str) -> None:
        nonlocal accumulated_text, draft_seen, result_text, result_error, tool_use_input
        ln = ln.strip()
        if not ln:
            return
        try:
            event = json.loads(ln)
        except json.JSONDecodeError:
            return

        etype = event.get("type")

        # 最終結果を取り出す
        if etype == "result":
            result_text = event.get("result", "")
            if event.get("is_error") or event.get("api_error_status"):
                result_error = result_text or json.dumps(event, ensure_ascii=False)
            return

        # assistant ブロックを処理
        if etype == "assistant":
            for block in event.get("message", {}).get("content", []):
                btype = block.get("type")
                if btype == "text":
                    # テキスト出力から draft 進捗をカウント
                    new_text = block.get("text", "")
                    new_part = new_text[len(accumulated_text):]
                    count = new_part.count('"bookmark_tweet_id"')
                    if count and on_progress:
                        draft_seen = min(draft_seen + count, total)
                        on_progress(draft_seen, total)
                    accumulated_text = new_text
                elif btype == "tool_use":
                    # --json-schema 使用時はここに構造化出力が入る
                    inp = block.get("input")
                    if isinstance(inp, dict):
                        tool_use_input = inp
                        count = str(inp).count('"bookmark_tweet_id"')
                        if count and on_progress:
                            on_progress(min(count, total), total)

    code, stdout, stderr, timed_out = _run_streaming(
        cmd,
        cwd=REPO_ROOT,
        env=env,
        input_text=prompt,
        timeout=timeout,
        on_stdout_line=on_line,
    )
    write_log("claude.stdout.log", stdout)
    write_log("claude.stderr.log", stderr)

    combined = "\n".join([stdout, stderr]).strip()
    if timed_out:
        raise JobError("claude 実行がタイムアウトしました")
    if code != 0:
        raise JobError(truncate(combined, 500) or f"claude 実行失敗 (exit={code})")
    if result_error:
        raise JobError(f"claude 実行エラー: {truncate(result_error, 500)}")

    # --json-schema 使用時は tool_use の input を優先して返す
    if tool_use_input is not None:
        return tool_use_input, "claude"

    # テキスト出力のフォールバック（--json-schema なし、または旧動作）
    output = result_text or accumulated_text or stdout
    try:
        return parse_json_output(output), "claude"
    except json.JSONDecodeError as exc:
        raise JobError(f"claude の JSON 解析に失敗しました: {exc}") from exc


def persist_generation(
    *,
    payload: dict,
    result: dict,
    agent_used: str,
    processed: set[str],
) -> tuple[list[dict], set[str]]:
    handled_ids: set[str] = set()
    created: list[dict] = []
    preview_base = os.environ.get("X_PREVIEW_PUBLIC_URL", DEFAULT_PREVIEW_URL).rstrip("/")

    for item in result.get("drafts", []):
        tweet_id = str(item["bookmark_tweet_id"])
        author_username = item["author_username"]
        memo = item["memo"] or f"from @{author_username}"
        draft_id = save_draft(payload["account"], item["parts"], memo=memo)
        save_bookmark_status(
            tweet_id=tweet_id,
            status="draft_saved",
            draft_id=draft_id,
        )

        image_prompt = item.get("image_prompt", {})
        character_reference_url = normalize_x_profile_image_url(
            (payload.get("account_profile") or {}).get("profile_image_url")
        )
        prompt_file = write_image_prompt_file(
            draft_id=draft_id,
            position=1,
            copy_text=image_prompt.get("copy", ""),
            prompt_text=image_prompt.get("prompt", ""),
            source_tweet_id=tweet_id,
        )
        save_bookmark_status(
            tweet_id=tweet_id,
            status="image_prompt_saved",
            draft_id=draft_id,
            image_prompt_path=str(prompt_file),
        )

        # image_prompt が空なら元ブックマークのメディアをフォールバックとして draft_parts に保存する
        use_source_media = not (image_prompt.get("prompt") or "").strip()
        source_media: list[dict] = []
        if use_source_media:
            bm = next((b for b in payload.get("bookmarks", []) if str(b.get("tweet_id")) == tweet_id), None)
            if bm:
                source_media = bm.get("media") or []
                if source_media:
                    first = source_media[0]
                    image_url = first.get("url") or first.get("preview_url")
                    if image_url:
                        try:
                            update_draft_image(draft_id, 1, image_url)
                            print(f"  📎 元メディアを image_url に設定: {image_url[:80]}")
                        except Exception as e:
                            print(f"  ⚠️  元メディア保存失敗: {e}")

        preview_url = f"{preview_base}?draft={draft_id}"
        draft_score = score_draft_for_audience(
            item.get("parts") or [],
            image_prompt=image_prompt,
            memo=memo,
        )
        metadata = {
            "draft_id": draft_id,
            "account_id": payload["account"],
            "x_username": payload["x_username"],
            "source_agent": agent_used,
            "bookmark_tweet_id": tweet_id,
            "author_username": author_username,
            "memo": memo,
            "created_at": utc_now(),
            "preview_url": preview_url,
            "rationale": item.get("rationale", ""),
            "draft_score": draft_score,
            "image_prompts": [
                {
                    "position": 1,
                    "copy": image_prompt.get("copy", ""),
                    "prompt": image_prompt.get("prompt", ""),
                    "file_path": str(prompt_file),
                    "character_reference_url": character_reference_url,
                }
            ],
            "character_reference": {
                "type": "x_profile_image",
                "url": character_reference_url,
                "instruction": "このプロフィール画像のキャラクター外見を参照して、図解内の右下などに20〜25%以内で入れる。",
            },
        }
        metadata_path = save_draft_metadata(draft_id, metadata)
        # 処理済みフラグは、下書き本体・画像プロンプト・メタデータが揃った後だけ付ける。
        save_bookmark_status(
            tweet_id=tweet_id,
            status="completed",
            draft_id=draft_id,
            metadata_path=str(metadata_path),
            image_prompt_path=str(prompt_file),
        )
        handled_ids.add(tweet_id)
        processed.add(tweet_id)
        save_processed_bookmarks(processed)

        created.append(
            {
                "draft_id": draft_id,
                "bookmark_tweet_id": tweet_id,
                "author_username": author_username,
                "memo": memo,
                "parts": item["parts"],
                "preview_url": preview_url,
                "image_prompt": metadata["image_prompts"][0],
                "draft_score": draft_score,
                "metadata_path": str(metadata_path),
            }
        )

    return created, handled_ids


def send_discord_message(webhook_url: str, content: str) -> None:
    body = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (https://github.com/x-post-writer, 1.0)",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10):
        return


def send_discord_report(
    webhook_url: str,
    *,
    payload: dict,
    created: list[dict],
    skipped: list[dict],
    agent_used: str,
) -> None:
    header = (
        f"📝 X自動下書き作成完了\n"
        f"- account: @{payload['x_username']}\n"
        f"- source: {agent_used}\n"
        f"- created: {len(created)}\n"
        f"- skipped: {len(skipped)}\n"
        "- preview: start_preview.sh 起動中なら URL から確認可能"
    )
    send_discord_message(webhook_url, header)

    for draft in created:
        prompt = draft["image_prompt"]["prompt"]
        copy_text = draft["image_prompt"]["copy"]
        content = (
            f"**draft_id:** `{draft['draft_id']}`\n"
            f"**memo:** {draft['memo']}\n"
            f"**preview:** {draft['preview_url']}\n"
            f"**image prompt file:** `{draft['image_prompt']['file_path']}`\n\n"
            f"**本文プレビュー:**\n"
            f"```\n{truncate(draft['parts'][0], 700)}\n```\n"
            f"**画像コピー:** {truncate(copy_text, 120)}\n"
            f"**画像プロンプト:**\n"
            f"```\n{truncate(prompt, 900)}\n```"
        )
        send_discord_message(webhook_url, content)


def send_discord_error(webhook_url: str, message: str) -> None:
    send_discord_message(webhook_url, f"❌ X自動下書き作成エラー\n```\n{truncate(message, 1600)}\n```")


def main() -> int:
    parser = argparse.ArgumentParser(description="Xブックマーク定時下書き生成")
    parser.add_argument("--account", default="GUTARA")
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--codex-timeout", type=int, default=1800)
    parser.add_argument("--claude-timeout", type=int, default=1800)
    parser.add_argument("--fetch-timeout", type=int, default=120)
    parser.add_argument("--refresh-account-file", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="既処理ブックマークも再生成対象にする")
    parser.add_argument("--use-latest", action="store_true", help="X APIを呼ばず、既存のbookmarks_latest.jsonだけを使う")
    parser.add_argument("--source", choices=["bookmarks", "db-drafts"], default="bookmarks", help="入力元。db-drafts はDB内の既存下書きを全件リライトする")
    args = parser.parse_args()

    env = build_env()
    os.environ.update(env)
    webhook_url = env.get(DISCORD_WEBHOOK_ENV, "").strip()

    print(f"\n📋 X 自動下書きパイプライン開始  (account={args.account})\n")

    try:
        # ── Step 1: 入力取得 ──────────────────────────────
        if args.source == "db-drafts":
            source_label = "DB下書き全件読込"
        else:
            source_label = "既存bookmarks_latest.json読込" if args.use_latest else f"ブックマーク取得中 (最大 {args.count} 件)"
        with _Spinner(f"[1/4] {source_label}...") as sp:
            payload = fetch_db_drafts_payload(args) if args.source == "db-drafts" else fetch_bookmarks_payload(args, env)
            total = len(payload.get("bookmarks", []))
            if args.source == "db-drafts":
                done_label = "DB下書き読込完了"
            else:
                done_label = "既存ブックマーク読込完了" if args.use_latest else "ブックマーク取得完了"
            sp.finish_ok(f"[1/4] {done_label}: {total} 件")

        processed = load_processed_bookmarks()
        new_bookmarks, already_done = filter_new_bookmarks(payload.get("bookmarks", []), processed)
        if args.force:
            new_bookmarks = payload.get("bookmarks", [])
            already_done = []
            print(f"     🔄 全件リライトモード: {len(new_bookmarks)} 件")
        else:
            print(f"     🆕 新規: {len(new_bookmarks)} 件  /  ⏭ 既処理: {len(already_done)} 件")

        if args.source == "db-drafts" and args.count > 0 and len(new_bookmarks) > args.count:
            print(f"     📦 DB下書きバッチ処理: {len(new_bookmarks)} 件中 {args.count} 件だけ処理")
            new_bookmarks = new_bookmarks[:args.count]

        if not new_bookmarks:
            write_log(
                "scheduled_pipeline.log",
                f"{utc_now()} no new bookmarks. already_done={len(already_done)}\n",
            )
            print("\n📭 新規ブックマークはありませんでした。終了します。")
            return 0

        # ── Step 1.5: ウェブリサーチ ─────────────────────────────
        research: dict[str, list[dict]] = {}
        with _Spinner(f"[1.5/4] ウェブリサーチ中... (最大 {_RESEARCH_PER_RUN} 件)") as sp:
            research = research_bookmarks(new_bookmarks)
            sp.finish_ok(f"[1.5/4] リサーチ完了: {len(research)} 件")

        # ── Step 2: 下書き生成 ────────────────────────────────────
        prompt = build_generation_prompt(
            agent_name="codex",
            payload=payload,
            bookmarks=new_bookmarks,
            research=research,
        )

        result: dict | None = None
        agent_used = "codex"
        total_new = len(new_bookmarks)

        with _Spinner(f"[2/4] Codex で下書き生成中... 0/{total_new} 件") as sp:
            def _codex_progress(n: int, t: int) -> None:
                sp.update(f"[2/4] Codex で下書き生成中... {n}/{t} 件")

            result, codex_status = run_codex(
                prompt, env, args.codex_timeout,
                on_progress=_codex_progress, total=total_new,
            )
            if result is not None:
                generated_count = len(result.get("drafts", []))
                if generated_count < total_new:
                    codex_status = f"codex generated only {generated_count}/{total_new} drafts"
                    result = None
                    sp.finish_warn(f"[2/4] Codex 生成不足: {generated_count}/{total_new} 件")
                else:
                    sp.finish_ok(f"[2/4] Codex 生成完了: {generated_count}/{total_new} 件")

        if result is None:
            print(f"     ⚠️  Codex フォールバック: {truncate(codex_status, 100)}")
            write_log("codex_fallback_reason.log", codex_status + "\n")
            fallback_prompt = build_generation_prompt(
                agent_name="claude",
                payload=payload,
                bookmarks=new_bookmarks,
                research=research,
            )
            with _Spinner(f"[2/4] Claude で下書き生成中... 0/{total_new} 件") as sp:
                def _claude_progress(n: int, t: int) -> None:
                    sp.update(f"[2/4] Claude で下書き生成中... {n}/{t} 件")

                result, agent_used = run_claude(
                    fallback_prompt, env, args.claude_timeout,
                    on_progress=_claude_progress, total=total_new,
                )
                sp.finish_ok(f"[2/4] Claude 生成完了: {len(result.get('drafts', []))}/{total_new} 件")

        # ── Step 2.5: 品質検証 ────────────────────────────────────
        thread_warnings = validate_thread_structure(result, new_bookmarks)
        quality_issues  = validate_draft_quality(result)

        all_ok = not thread_warnings and not quality_issues
        if all_ok:
            print("  ✅ 品質検証: 問題なし（全件ツリー・NGパターンなし）")
        else:
            if thread_warnings:
                print(f"  ⚠️  ツリー不足 {len(thread_warnings)} 件:")
                for w in thread_warnings:
                    print(f"     @{w['author']} — 下書き{w['draft_parts']}パーツ (tweet_id={w['tweet_id']})")
            if quality_issues:
                print(f"  ⚠️  品質問題 {len(quality_issues)} 件:")
                for q in quality_issues:
                    for iss in q["issues"]:
                        print(f"     @{q['author']}: {iss}")
            write_log(
                "quality_validation.json",
                json.dumps({"thread_warnings": thread_warnings,
                            "quality_issues": quality_issues},
                           ensure_ascii=False, indent=2) + "\n",
            )

        if args.dry_run:
            write_log(
                "scheduled_pipeline_dry_run.json",
                json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            )
            print("\n🧪 dry-run 完了 (保存・通知はスキップ)")
            return 0

        # ── Step 3: 下書き保存 ────────────────────────────────────
        with _Spinner("[3/4] 下書き保存中...") as sp:
            created, handled_ids = persist_generation(
                payload=payload,
                result=result,
                agent_used=agent_used,
                processed=processed,
            )
            skipped_count = len(result.get("skipped", []))
            sp.finish_ok(f"[3/4] 保存完了: {len(created)} 件作成 / {skipped_count} 件スキップ")

        processed.update(handled_ids)
        save_processed_bookmarks(processed)

        for draft in created:
            print(f"     📝 {draft['draft_id']}  {draft['memo']}")
            print(f"        preview → {draft['preview_url']}")

        write_log(
            "scheduled_pipeline.log",
            (
                f"{utc_now()} created={len(created)} "
                f"skipped={skipped_count} "
                f"already_done={len(already_done)} source={agent_used}\n"
            ),
        )

        # ── Step 4: Discord 通知 ──────────────────────────────────
        if webhook_url and (created or result.get("skipped")):
            with _Spinner("[4/4] Discord へ通知中...") as sp:
                send_discord_report(
                    webhook_url,
                    payload=payload,
                    created=created,
                    skipped=result.get("skipped", []),
                    agent_used=agent_used,
                )
                sp.finish_ok("[4/4] Discord 通知完了")
        else:
            print("     💬 Discord Webhook 未設定のためスキップ")

        print(
            f"\n🎉 完了  created={len(created)}  skipped={skipped_count}  source={agent_used}\n"
        )
        return 0

    except Exception as exc:
        message = str(exc)
        write_log("scheduled_pipeline_error.log", f"{utc_now()} {message}\n")
        if webhook_url:
            try:
                send_discord_error(webhook_url, message)
            except Exception:
                pass
        print(f"\n❌ {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
