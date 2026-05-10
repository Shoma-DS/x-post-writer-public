# XのブックマークをAPIで取得し、初回のみアカウント情報ファイルも初期化するスクリプト。
# 実アカウントと accounts/*.md の対応を照合し、2回目以降は既存ファイルを再利用する。
# OAuth 2.0 アクセストークンが期限切れの場合はリフレッシュトークンで自動更新する。
# 使い方: python fetch_bookmarks.py [--account GUTARA] [--count 5] [--refresh-account-file]

import argparse
import json
import os
import re
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
import tweepy
from requests_oauthlib import OAuth1

SCRIPT_DIR = Path(__file__).parent
SKILL_DIR = SCRIPT_DIR.parent
REPO_ROOT = SCRIPT_DIR.parent
ACCOUNT_DIR = SKILL_DIR / "accounts"
CONFIG_FILE = SCRIPT_DIR / "accounts_config.json"
OUTPUT_FILE = SCRIPT_DIR / "bookmarks_latest.json"
ENV_FILE = REPO_ROOT / ".env"
AUTO_BLOCK_START = "<!-- x-account:auto:start -->"
AUTO_BLOCK_END = "<!-- x-account:auto:end -->"
PROFILE_USER_FIELDS = [
    "created_at",
    "description",
    "name",
    "pinned_tweet_id",
    "profile_image_url",
    "public_metrics",
    "username",
]
PINNED_TWEET_FIELDS = ["created_at", "public_metrics", "text"]


def load_config():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        raw = "".join(l for l in f if not l.strip().startswith("//"))
        return json.loads(raw)


def normalize_handle(value):
    return value.strip().lstrip("@").lower()


def format_dt(value):
    return value.isoformat() if value else ""


def truncate_text(text, limit=180):
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def get_bookmarks_access_token(account_cfg):
    account_id = account_cfg["id"]
    candidates = [
        account_cfg.get("bookmarks_access_token_env"),
        account_cfg.get("oauth2_access_token_env"),
        f"X_BOOKMARKS_ACCESS_TOKEN_{account_id}",
        f"X_OAUTH2_ACCESS_TOKEN_{account_id}",
        "X_BEARER_TOKEN",
    ]
    for key in candidates:
        if not key:
            continue
        value = os.environ.get(key)
        if value:
            return value, key
    return None, " or ".join(candidates)


def load_dotenv(path=ENV_FILE):
    if not path.exists():
        return {}

    loaded = {}
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


def dotenv_quote(value):
    if value == "":
        return '""'
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def save_env_values(updates):
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    pattern = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*).*$")
    seen = set()
    next_lines = []

    for line in lines:
        match = pattern.match(line)
        if not match:
            next_lines.append(line)
            continue
        prefix, key, sep = match.groups()
        if key not in updates:
            next_lines.append(line)
            continue
        next_lines.append(f"{prefix}{key}{sep}{dotenv_quote(updates[key])}")
        seen.add(key)

    for key in sorted(set(updates) - seen):
        next_lines.append(f"{key}={dotenv_quote(updates[key])}")

    ENV_FILE.write_text("\n".join(next_lines).rstrip() + "\n", encoding="utf-8")


def save_tokens_to_settings(account_cfg, access_token, refresh_token=None):
    """新しいトークンを repo 直下の .env に書き戻す。"""
    account_id = account_cfg["id"]
    access_token_env = account_cfg.get("bookmarks_access_token_env") or f"X_BOOKMARKS_ACCESS_TOKEN_{account_id}"
    refresh_token_env = account_cfg.get("bookmarks_refresh_token_env") or f"X_BOOKMARKS_REFRESH_TOKEN_{account_id}"
    try:
        updates = {access_token_env: access_token}
        if refresh_token:
            updates[refresh_token_env] = refresh_token
        save_env_values(updates)
        print(".env のトークンを更新しました")
    except Exception as e:
        print(f"⚠️  .env の更新に失敗しました: {e}", file=sys.stderr)


def oauth2_setup_command(account_id):
    return (
        "python3 "
        "\"$X_POST_WRITER_ROOT/scripts/oauth2_setup.py\" "
        f"--account {account_id}"
    )


def print_oauth2_reauth_message(account_id, reason):
    print(
        f"❌ X OAuth 2.0 認証に失敗しました: {reason}\n"
        f"   X Developer Portal のOAuth設定、認可スコープ、または保存済みトークンが無効な可能性があります。\n"
        f"   以下を実行して再認証してください:\n"
        f"   {oauth2_setup_command(account_id)}",
        file=sys.stderr,
    )


def refresh_oauth2_token(account_cfg):
    """リフレッシュトークンを使って新しいアクセストークンを取得する。
    成功時は (access_token, None)、失敗時は (None, error_message) を返す。"""
    account_id = account_cfg["id"]
    refresh_token_env = account_cfg.get("bookmarks_refresh_token_env") or f"X_BOOKMARKS_REFRESH_TOKEN_{account_id}"
    client_id_env = account_cfg.get("oauth2_client_id_env") or f"X_OAUTH2_CLIENT_ID_{account_id}"
    client_secret_env = account_cfg.get("oauth2_client_secret_env") or f"X_OAUTH2_CLIENT_SECRET_{account_id}"
    refresh_token = os.environ.get(refresh_token_env)
    client_id = (
        os.environ.get(client_id_env)
        or os.environ.get("X_OAUTH2_CLIENT_ID")
    )
    client_secret = (
        os.environ.get(client_secret_env)
        or os.environ.get("X_OAUTH2_CLIENT_SECRET")
    )

    if not refresh_token:
        return None, f"{refresh_token_env} が未設定のため自動リフレッシュできません"
    if not client_id:
        return None, f"{client_id_env} または X_OAUTH2_CLIENT_ID が未設定です"

    print(f"🔄 アクセストークンが期限切れです。リフレッシュトークンで更新中...")

    data = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    auth = (
        requests.auth.HTTPBasicAuth(client_id, client_secret)
        if client_secret
        else None
    )
    if not client_secret:
        data["client_id"] = client_id

    try:
        resp = requests.post(
            "https://api.twitter.com/2/oauth2/token",
            data=data,
            auth=auth,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
    except requests.RequestException as e:
        return None, f"トークンリフレッシュのHTTPリクエストに失敗しました: {e}"

    if resp.status_code != 200:
        return None, f"トークンリフレッシュに失敗しました: {resp.status_code} {resp.text}"

    token_data = resp.json()
    new_access_token = token_data.get("access_token")
    new_refresh_token = token_data.get("refresh_token")

    if not new_access_token:
        return None, f"レスポンスにアクセストークンが含まれていません: {token_data}"

    # プロセス内の環境変数を即座に更新
    access_token_env = account_cfg.get("bookmarks_access_token_env") or f"X_BOOKMARKS_ACCESS_TOKEN_{account_id}"
    os.environ[access_token_env] = new_access_token
    if new_refresh_token:
        os.environ[refresh_token_env] = new_refresh_token

    # .env にも書き戻して次回起動後も有効にする
    save_tokens_to_settings(account_cfg, new_access_token, new_refresh_token)

    print(f"✅ アクセストークンを更新しました")
    return new_access_token, None


def build_oauth1_client(account_cfg):
    access_token = os.environ.get(account_cfg["token_env"])
    access_token_secret = os.environ.get(account_cfg["token_secret_env"])
    consumer_key = os.environ.get("X_CONSUMER_KEY") or os.environ.get("X_API_KEY")
    consumer_secret = os.environ.get("X_CONSUMER_SECRET") or os.environ.get("X_API_SECRET")

    missing = []
    if not access_token:
        missing.append(account_cfg["token_env"])
    if not access_token_secret:
        missing.append(account_cfg["token_secret_env"])
    if not consumer_key:
        missing.append("X_CONSUMER_KEY or X_API_KEY")
    if not consumer_secret:
        missing.append("X_CONSUMER_SECRET or X_API_SECRET")
    if missing:
        print(f"❌ 環境変数が未設定: {missing}", file=sys.stderr)
        sys.exit(1)

    return tweepy.Client(
        consumer_key=consumer_key,
        consumer_secret=consumer_secret,
        access_token=access_token,
        access_token_secret=access_token_secret,
        wait_on_rate_limit=True,
    )


def build_bookmarks_client(account_cfg, access_token=None):
    # ブックマークAPIは OAuth 2.0 User Context のみ対応。
    # access_token が指定されない場合は環境変数から取得する。
    account_id = account_cfg["id"]
    if not access_token:
        access_token = (
            os.environ.get(account_cfg.get("bookmarks_access_token_env") or "")
            or os.environ.get(account_cfg.get("oauth2_access_token_env") or "")
            or os.environ.get(f"X_BOOKMARKS_ACCESS_TOKEN_{account_id}")
            or os.environ.get(f"X_OAUTH2_ACCESS_TOKEN_{account_id}")
        )
    if not access_token:
        print(
            f"❌ ブックマーク取得には OAuth 2.0 アクセストークンが必要です。\n"
            f"   以下を実行して一度だけ認証してください:\n"
            f"   python3 \"$X_POST_WRITER_ROOT/scripts/oauth2_setup.py\" --account {account_id}",
            file=sys.stderr,
        )
        sys.exit(1)
    # OAuth 2.0 PKCE トークンは bearer_token に渡す (tweepy はこれを User Context として扱う)
    return tweepy.Client(bearer_token=access_token, wait_on_rate_limit=True)


def fetch_account_profile(client, account_cfg, user_auth=True):
    me = client.get_me(user_auth=user_auth)
    if not me.data:
        print("❌ X API から自分のアカウント情報を取得できませんでした", file=sys.stderr)
        sys.exit(1)

    user_id = str(me.data.id)
    user_response = client.get_user(
        id=user_id,
        user_fields=PROFILE_USER_FIELDS,
        user_auth=user_auth,
    )
    user = user_response.data if user_response and user_response.data else me.data

    actual_username = getattr(user, "username", None) or account_cfg["x_username"]
    display_name = getattr(user, "name", None) or actual_username
    bio = getattr(user, "description", None) or ""
    profile_image_url = getattr(user, "profile_image_url", None) or ""
    created_at = getattr(user, "created_at", None)
    public_metrics = getattr(user, "public_metrics", None) or {}
    pinned_tweet_id = getattr(user, "pinned_tweet_id", None)

    if normalize_handle(actual_username) != normalize_handle(account_cfg["x_username"]):
        print(
            f"⚠️  設定の x_username と認証アカウントが違います: "
            f"{account_cfg['x_username']} -> {actual_username}",
            file=sys.stderr,
        )

    pinned_tweet = None
    if pinned_tweet_id:
        try:
            pinned_response = client.get_tweet(
                pinned_tweet_id,
                tweet_fields=PINNED_TWEET_FIELDS,
                user_auth=user_auth,
            )
            if pinned_response and pinned_response.data:
                tweet = pinned_response.data
                pinned_tweet = {
                    "tweet_id": str(tweet.id),
                    "text": tweet.text or "",
                    "created_at": format_dt(getattr(tweet, "created_at", None)),
                    "public_metrics": getattr(tweet, "public_metrics", None) or {},
                    "url": f"https://x.com/{actual_username}/status/{tweet.id}",
                }
        except tweepy.TweepyException as exc:
            print(f"⚠️  固定投稿の取得に失敗しました: {exc}", file=sys.stderr)

    return {
        "config_account_id": account_cfg["id"],
        "x_user_id": user_id,
        "x_username": actual_username,
        "display_name": display_name,
        "bio": bio,
        "profile_image_url": profile_image_url,
        "created_at": format_dt(created_at),
        "public_metrics": public_metrics,
        "pinned_tweet": pinned_tweet,
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }


def extract_account_file_metadata(path):
    metadata = {"path": path, "account_id": None, "handle": None, "x_user_id": None}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return metadata

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not metadata["account_id"] and line.startswith("アカウントID:"):
            value = line.split(":", 1)[1].strip()
            metadata["account_id"] = value.split("（", 1)[0].strip()
        elif not metadata["handle"] and line.startswith("ハンドル:"):
            metadata["handle"] = normalize_handle(line.split(":", 1)[1].strip())
        elif not metadata["x_user_id"] and line.startswith("X User ID:"):
            metadata["x_user_id"] = line.split(":", 1)[1].strip()
        if metadata["account_id"] and metadata["handle"] and metadata["x_user_id"]:
            break
    return metadata


def resolve_account_file(account_cfg, profile):
    # サブフォルダ（x_username小文字）が存在する場合はその中を優先パスとする
    sub_dir = ACCOUNT_DIR / profile['x_username'].lower()
    if sub_dir.exists() and sub_dir.is_dir():
        preferred_path = sub_dir / f"{profile['x_username']}.md"
    else:
        preferred_path = ACCOUNT_DIR / f"{profile['x_username']}.md"
    candidates = []

    # フラットな *.md とサブフォルダ内の *.md を両方検索する
    all_md = sorted(set(ACCOUNT_DIR.glob("*.md")) | set(ACCOUNT_DIR.glob("*/*.md")))
    for path in all_md:
        metadata = extract_account_file_metadata(path)
        score = 0
        if metadata["x_user_id"] and metadata["x_user_id"] == profile["x_user_id"]:
            score = 300
        elif metadata["handle"] and metadata["handle"] == normalize_handle(profile["x_username"]):
            score = 200
        elif metadata["account_id"] and metadata["account_id"] == account_cfg["id"]:
            score = 100

        if score:
            candidates.append((score, path, metadata))

    if candidates:
        candidates.sort(key=lambda item: (-item[0], str(item[1])))
        best_score = candidates[0][0]
        best = [item for item in candidates if item[0] == best_score]
        preferred = [item for item in best if item[1] == preferred_path]
        if len(preferred) == 1:
            return preferred[0][1], preferred[0][2], "matched"
        if len(best) > 1:
            names = ", ".join(item[1].name for item in best)
            print(
                f"❌ アカウント情報ファイル候補が複数あります: {names}. "
                f"X User ID またはハンドルを整理してください。",
                file=sys.stderr,
            )
            sys.exit(1)
        return candidates[0][1], candidates[0][2], "matched"

    if preferred_path.exists():
        return preferred_path, extract_account_file_metadata(preferred_path), "preferred-existing"

    return preferred_path, {"path": preferred_path, "account_id": None, "handle": None, "x_user_id": None}, "new"


def render_auto_block(profile):
    lines = [
        AUTO_BLOCK_START,
        "## 自動取得メタ情報",
        "",
        f"- 同期日時: {profile['synced_at']}",
        f"- X User ID: {profile['x_user_id']}",
        f"- 表示名: {profile['display_name']}",
        f"- ハンドル: @{profile['x_username']}",
        f"- プロフィール文: {profile['bio'] or 'なし'}",
        f"- プロフィール画像: {profile['profile_image_url'] or 'なし'}",
        f"- アカウント作成日時: {profile['created_at'] or 'なし'}",
        "",
        "## 固定投稿メモ（自動取得）",
        "",
    ]

    pinned = profile["pinned_tweet"]
    if pinned:
        lines.extend(
            [
                f"- tweet_id: {pinned['tweet_id']}",
                f"- URL: {pinned['url']}",
                f"- 投稿日時: {pinned['created_at'] or 'なし'}",
                "",
                "```",
                pinned["text"].strip() or "(本文なし)",
                "```",
            ]
        )
    else:
        lines.append("- 固定投稿は取得できませんでした")

    lines.append(AUTO_BLOCK_END)
    return "\n".join(lines)


def ensure_top_metadata_lines(text, account_cfg, profile):
    lines = text.rstrip("\n").splitlines()
    if not lines:
        lines = [f"# {profile['display_name']}"]

    insert_at = 1 if lines[0].startswith("# ") else 0

    if not any(line.startswith("アカウントID:") for line in lines[:10]):
        lines.insert(insert_at, f"アカウントID: {account_cfg['id']}")
        insert_at += 1

    handle_index = next((i for i, line in enumerate(lines[:12]) if line.startswith("ハンドル:")), None)
    if handle_index is None:
        lines.insert(insert_at, f"ハンドル: @{profile['x_username']}")
    else:
        lines[handle_index] = f"ハンドル: @{profile['x_username']}"

    return "\n".join(lines).rstrip() + "\n"


def upsert_auto_block(text, profile):
    auto_block = render_auto_block(profile)

    if AUTO_BLOCK_START in text and AUTO_BLOCK_END in text:
        before, _, rest = text.partition(AUTO_BLOCK_START)
        _, _, after = rest.partition(AUTO_BLOCK_END)
        updated = before.rstrip() + "\n\n" + auto_block + "\n\n" + after.lstrip("\n")
        return updated.rstrip() + "\n"

    lines = text.rstrip("\n").splitlines()
    insert_at = next((i + 1 for i, line in enumerate(lines) if line.startswith("ハンドル:")), None)
    if insert_at is None:
        insert_at = next((i for i, line in enumerate(lines) if line.strip() == "---"), None)
    if insert_at is None:
        insert_at = len(lines)

    block_lines = ["", auto_block, ""]
    lines[insert_at:insert_at] = block_lines
    return "\n".join(lines).rstrip() + "\n"


def build_new_account_file(account_cfg, profile):
    bio_hint = profile["bio"] or "未取得"
    pinned = profile["pinned_tweet"]
    pinned_hint = truncate_text(pinned["text"], 220) if pinned else "未取得"

    return (
        f"# {profile['display_name']}\n"
        f"アカウントID: {account_cfg['id']}\n"
        f"ハンドル: @{profile['x_username']}\n"
        "\n"
        f"{render_auto_block(profile)}\n"
        "\n"
        "---\n"
        "\n"
        "## コンセプト・目的\n"
        f"- プロフィール文の要点: {bio_hint}\n"
        f"- 固定投稿の主張: {pinned_hint}\n"
        "- 上の自動取得情報を叩き台にして、人が手で整える\n"
        "\n"
        "## ターゲット\n"
        "- [要編集]\n"
        "\n"
        "## トンマナ・口調\n"
        "- 固定投稿と普段の投稿を見て追記する\n"
        "\n"
        "## ビジュアル世界観\n"
        "\n"
        "### ブランドカラー\n"
        "- メイン: [未設定]\n"
        "- サブ: [未設定]\n"
        "- 背景: [未設定]\n"
        "\n"
        "### スタイルキーワード（英語プロンプトに使う）\n"
        "- [未設定]\n"
        "\n"
        "### 図解パターン\n"
        "- [未設定]\n"
        "\n"
        "### NGルール\n"
        "- [未設定]\n"
        "\n"
        "## 投稿の型・よく使う構成\n"
        "- 固定投稿と直近投稿を見て追記する\n"
        "\n"
        "## よく使うハッシュタグ\n"
        "- [要編集]\n"
        "\n"
        "## NG・禁止事項\n"
        "- [要編集]\n"
        "\n"
        "## 過去の人気投稿例\n"
        f"- 固定投稿候補: {pinned_hint}\n"
    )


def ensure_account_file(account_cfg, profile, refresh=False):
    ACCOUNT_DIR.mkdir(parents=True, exist_ok=True)
    path, metadata, resolution = resolve_account_file(account_cfg, profile)
    path.parent.mkdir(parents=True, exist_ok=True)  # サブフォルダも自動作成

    if path.exists():
        original = path.read_text(encoding="utf-8")
        needs_sync = any(
            [
                refresh,
                AUTO_BLOCK_START not in original,
                AUTO_BLOCK_END not in original,
                metadata["account_id"] != account_cfg["id"],
                metadata["handle"] != normalize_handle(profile["x_username"]),
                metadata["x_user_id"] != profile["x_user_id"],
            ]
        )
        if needs_sync:
            updated = ensure_top_metadata_lines(original, account_cfg, profile)
            updated = upsert_auto_block(updated, profile)
            path.write_text(updated, encoding="utf-8")
            status = "updated" if resolution != "new" else "created"
        else:
            status = "reused"
    else:
        path.write_text(build_new_account_file(account_cfg, profile), encoding="utf-8")
        status = "created"

    return {
        "status": status,
        "path": str(path),
        "resolution": resolution,
    }


_THREAD_FETCH_LIMIT = 5  # 1回の実行でスレッド取得を試みる最大ブックマーク数（レート制限対策）


def _fetch_thread_parts(client, conversation_id: str, author_id) -> list[dict]:
    """同じ conversation_id を持つ著者ツイートを取得してスレッド順に返す。"""
    try:
        resp = client.get_users_tweets(
            id=str(author_id),
            max_results=100,
            tweet_fields=["conversation_id", "text", "created_at"],
        )
    except Exception:
        return []
    if not resp.data:
        return []
    parts = []
    for t in resp.data:
        cid = str(getattr(t, "conversation_id", None) or "")
        if cid == conversation_id:
            parts.append({"tweet_id": str(t.id), "text": t.text})
    parts.sort(key=lambda x: int(x["tweet_id"]))  # 古い順（= スレッド上から）
    return parts


def _extract_media(tweet, media_map: dict) -> list[dict]:
    """ツイートに添付されたメディア情報を返す。"""
    attachments = getattr(tweet, "attachments", None) or {}
    media_keys  = (attachments.get("media_keys") if isinstance(attachments, dict)
                   else getattr(attachments, "media_keys", None)) or []
    result = []
    for key in media_keys:
        m = media_map.get(key)
        if not m:
            continue
        mtype = getattr(m, "type", None) or ""
        # 直接URL (photo)
        photo_url   = getattr(m, "url", None) or None
        # サムネイル (video / animated_gif)
        preview_url = getattr(m, "preview_image_url", None) or None
        # 動画URL: variants の中で最高ビットレートの mp4 を使う
        video_url = None
        variants = getattr(m, "variants", None) or []
        if variants:
            mp4s = [v for v in variants if getattr(v, "content_type", "") == "video/mp4"]
            if mp4s:
                best = max(mp4s, key=lambda v: getattr(v, "bitrate", 0) or 0)
                video_url = getattr(best, "url", None)
        result.append({
            "type":        mtype,
            "url":         photo_url or preview_url,   # preview 用: 常に画像URLが入るようにする
            "preview_url": preview_url,
            "video_url":   video_url,
        })
    return result


def fetch_bookmarks(client, count, account_cfg=None, user_id=None):
    try:
        response = client.get_bookmarks(
            max_results=min(count, 100),
            tweet_fields=["text", "created_at", "author_id", "public_metrics",
                          "conversation_id", "attachments"],
            expansions=["author_id", "attachments.media_keys"],
            user_fields=["username", "name"],
            media_fields=["url", "preview_image_url", "type", "variants"],
        )
    except (TypeError, tweepy.TweepyException) as exc:
        raise RuntimeError(f"Xブックマーク取得に失敗しました。 詳細: {exc}") from exc

    if not response.data:
        print("📭 ブックマークが見つかりませんでした")
        return []

    users_map = {}
    media_map: dict = {}
    if response.includes:
        for user in (response.includes.get("users") or []):
            users_map[user.id] = user
        for media in (response.includes.get("media") or []):
            media_map[media.media_key] = media

    bookmarks = []
    thread_fetch_count = 0
    for tweet in response.data:
        author      = users_map.get(tweet.author_id)
        username    = author.username if author else "unknown"
        author_name = author.name    if author else "unknown"
        conv_id     = str(getattr(tweet, "conversation_id", None) or tweet.id)
        metrics     = getattr(tweet, "public_metrics", None) or {}
        reply_count = metrics.get("reply_count", 0) if isinstance(metrics, dict) else getattr(metrics, "reply_count", 0)
        media       = _extract_media(tweet, media_map)

        # スレッド候補: このツイートが root かつ返信あり、または root でない（返信ツイート）
        is_root = conv_id == str(tweet.id)
        should_fetch_thread = (
            thread_fetch_count < _THREAD_FETCH_LIMIT
            and (not is_root or reply_count > 0)
        )
        thread_parts: list[dict] = []
        if should_fetch_thread:
            print(f"  🔗 スレッド取得: @{username} (conversation_id={conv_id})")
            thread_parts = _fetch_thread_parts(client, conv_id, tweet.author_id)
            thread_fetch_count += 1
            if len(thread_parts) > 1:
                print(f"     → {len(thread_parts)} パーツ取得")
            else:
                thread_parts = []

        if media:
            types = ", ".join(set(m["type"] for m in media))
            print(f"  📎 メディア取得: @{username} tweet={tweet.id} [{types}]")

        bookmarks.append({
            "tweet_id":        str(tweet.id),
            "text":            tweet.text,
            "author_username": username,
            "author_name":     author_name,
            "conversation_id": conv_id,
            "thread_parts":    thread_parts,
            "media":           media,
        })

    return bookmarks


def main():
    for key, value in load_dotenv().items():
        os.environ.setdefault(key, value)

    parser = argparse.ArgumentParser(description="Xブックマーク取得")
    parser.add_argument("--account", default="GUTARA")
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument(
        "--refresh-account-file",
        action="store_true",
        help="accounts/*.md の自動取得セクションを再同期する",
    )
    args = parser.parse_args()

    config = load_config()
    account_cfg = next((a for a in config["accounts"] if a["id"] == args.account), None)
    if not account_cfg:
        print(f"❌ アカウント '{args.account}' が見つかりません", file=sys.stderr)
        sys.exit(1)

    bookmarks_client = build_bookmarks_client(account_cfg)
    try:
        profile = fetch_account_profile(bookmarks_client, account_cfg, user_auth=False)
    except tweepy.Unauthorized:
        # アクセストークンが期限切れの場合、リフレッシュして1回だけ再試行する
        new_token, err = refresh_oauth2_token(account_cfg)
        if err:
            print(
                f"❌ トークンの自動更新に失敗しました: {err}\n"
                f"   以下を実行して再認証してください:\n"
                f"   python3 \"$X_POST_WRITER_ROOT/scripts/oauth2_setup.py\" --account {args.account}",
                file=sys.stderr,
            )
            sys.exit(1)
        bookmarks_client = build_bookmarks_client(account_cfg, access_token=new_token)
        try:
            profile = fetch_account_profile(bookmarks_client, account_cfg, user_auth=False)
        except tweepy.Unauthorized:
            print_oauth2_reauth_message(
                args.account,
                "アクセストークン更新後も /2/users/me が401を返しました",
            )
            sys.exit(1)
    print(f"👤 @{profile['x_username']} (ID: {profile['x_user_id']})")

    account_file = ensure_account_file(
        account_cfg,
        profile,
        refresh=args.refresh_account_file,
    )
    status_label = {
        "created": "🆕 初回作成",
        "updated": "♻️ 自動取得情報を更新",
        "reused": "📄 既存を再利用",
    }.get(account_file["status"], account_file["status"])
    print(f"{status_label}: {account_file['path']}")

    print(f"📚 ブックマーク取得中... (最大{args.count}件)")
    try:
        bookmarks = fetch_bookmarks(bookmarks_client, args.count, account_cfg, profile["x_user_id"])
    except tweepy.Unauthorized:
        print_oauth2_reauth_message(args.account, "ブックマーク取得APIが401を返しました")
        sys.exit(1)
    except RuntimeError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(1)

    if not bookmarks:
        return

    output = {
        "account": args.account,
        "x_username": profile["x_username"],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "account_profile": {
            "account_id": account_cfg["id"],
            "x_user_id": profile["x_user_id"],
            "display_name": profile["display_name"],
            "x_username": profile["x_username"],
            "bio": profile["bio"],
            "profile_image_url": profile["profile_image_url"],
            "pinned_tweet": profile["pinned_tweet"],
            "account_file_path": account_file["path"],
        },
        "bookmarks": bookmarks,
    }
    OUTPUT_FILE.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"✅ {len(bookmarks)}件を {OUTPUT_FILE} に保存しました\n")
    for i, bm in enumerate(bookmarks, 1):
        preview = bm["text"][:80]
        ellipsis = "…" if len(bm["text"]) > 80 else ""
        print(f"  [{i}] @{bm['author_username']}: {preview}{ellipsis}")

    print("\n👉 次のステップ: Claude Code / Codex に「ブックマークから下書きを生成して」と依頼してください")


if __name__ == "__main__":
    main()
