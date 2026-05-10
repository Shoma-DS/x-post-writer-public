# X投稿の過去3日分メトリクスをNeon DBから取得し、時系列分析・傾向抽出・Discord通知を行うスクリプト。
# 投稿時間帯・post_type別のエンゲージメント傾向を分析し、accounts/*.mdの「最近の傾向」セクションを自動更新する。
# 使い方: python daily_analysis.py [--account GUTARA]

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import psycopg2
import requests

SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "accounts_config.json"
ACCOUNTS_DIR = SCRIPT_DIR.parent / "accounts"
ANALYSIS_WINDOW_DAYS = 3
TRACKED_POST_TYPES = ("single", "long")


def load_settings_env() -> dict[str, str]:
    from with_local_env import load_dotenv, load_settings_env as _load_fallback
    env = _load_fallback()
    env.update(load_dotenv())
    return env


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        print(f"❌ 設定ファイルが見つかりません: {CONFIG_FILE}", file=sys.stderr)
        sys.exit(1)
    text = CONFIG_FILE.read_text(encoding="utf-8")
    json_text = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("//")
    )
    return json.loads(json_text)


def get_db_conn():
    url = os.environ.get("NEON_DATABASE_URL")
    if not url:
        print("❌ NEON_DATABASE_URL が設定されていません", file=sys.stderr)
        sys.exit(1)
    return psycopg2.connect(url)


def fetch_tweets_with_latest_metrics(cur, x_username: str, since: datetime) -> list[dict]:
    """過去3日間のメイン投稿と最新スナップショットを取得する。"""
    cur.execute(
        """
        SELECT
            t.tweet_id,
            t.content,
            t.posted_at,
            t.post_type,
            s.impression_count,
            s.like_count,
            s.retweet_count,
            s.bookmark_count,
            s.reply_count
        FROM tweets t
        JOIN accounts a ON t.account_id = a.account_id
        LEFT JOIN LATERAL (
            SELECT impression_count, like_count, retweet_count, bookmark_count, reply_count
            FROM tweet_metrics_snapshots
            WHERE tweet_id = t.tweet_id
            ORDER BY hours_after_post DESC
            LIMIT 1
        ) s ON TRUE
        WHERE a.x_username = %s
          AND t.posted_at >= %s
          AND t.post_type = ANY(%s)
        ORDER BY t.posted_at ASC
        """,
        (x_username, since, list(TRACKED_POST_TYPES)),
    )
    cols = ["tweet_id", "content", "posted_at", "post_type",
            "impression_count", "like_count", "retweet_count", "bookmark_count", "reply_count"]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def analyze(tweets: list[dict]) -> dict:
    """時系列・時間帯別・post_type別・上位投稿を集計して返す。"""
    if not tweets:
        return {
            "total": 0,
            "avg_like": 0,
            "avg_impression": 0,
            "avg_rt": 0,
            "total_bookmark": 0,
            "hourly": {},
            "by_type": {},
            "top3": [],
            "timeline": [],
        }

    def safe_int(v) -> int:
        return v if isinstance(v, int) else 0

    # 直近3日間サマリー
    total = len(tweets)
    avg_like = sum(safe_int(t["like_count"]) for t in tweets) / total
    avg_impression = sum(safe_int(t["impression_count"]) for t in tweets) / total
    avg_rt = sum(safe_int(t["retweet_count"]) for t in tweets) / total
    total_bookmark = sum(safe_int(t["bookmark_count"]) for t in tweets)

    # 時間帯別集計
    hourly_likes: dict[int, list[int]] = defaultdict(list)
    hourly_imp: dict[int, list[int]] = defaultdict(list)
    for t in tweets:
        if t["posted_at"]:
            h = t["posted_at"].astimezone(timezone(timedelta(hours=9))).hour
            hourly_likes[h].append(safe_int(t["like_count"]))
            hourly_imp[h].append(safe_int(t["impression_count"]))
    hourly = {
        h: {
            "avg_like": sum(hourly_likes[h]) / len(hourly_likes[h]),
            "avg_impression": sum(hourly_imp[h]) / len(hourly_imp[h]),
        }
        for h in hourly_likes
    }

    # post_type別集計
    type_data: dict[str, list[dict]] = defaultdict(list)
    for t in tweets:
        type_data[t["post_type"] or "single"].append(t)
    by_type = {
        pt: {
            "count": len(items),
            "avg_like": sum(safe_int(i["like_count"]) for i in items) / len(items),
            "avg_impression": sum(safe_int(i["impression_count"]) for i in items) / len(items),
        }
        for pt, items in type_data.items()
    }

    # 上位3件（like順）
    top3 = sorted(tweets, key=lambda t: safe_int(t["like_count"]), reverse=True)[:3]

    # 時系列一覧
    timeline = [
        {
            "posted_at": t["posted_at"],
            "content": t["content"],
            "like": safe_int(t["like_count"]),
            "impression": safe_int(t["impression_count"]),
            "rt": safe_int(t["retweet_count"]),
            "bookmark": safe_int(t["bookmark_count"]),
            "reply": safe_int(t["reply_count"]),
        }
        for t in tweets
    ]

    return {
        "total": total,
        "avg_like": avg_like,
        "avg_impression": avg_impression,
        "avg_rt": avg_rt,
        "total_bookmark": total_bookmark,
        "hourly": hourly,
        "by_type": by_type,
        "top3": top3,
        "timeline": timeline,
    }


def format_report(x_username: str, result: dict, date_str: str) -> str:
    if result["total"] == 0:
        return f"📊 @{x_username} 3日間X分析レポート ({date_str})\n\nデータなし（直近3日間にメイン投稿が記録されていません）"

    lines = [f"📊 @{x_username} 3日間X分析レポート ({date_str})", ""]

    # サマリー
    lines.append("【直近3日間のサマリー】")
    lines.append(
        f"投稿数: {result['total']}件 | 平均いいね: {result['avg_like']:.1f} | "
        f"平均インプレ: {result['avg_impression']:.0f} | 平均RT: {result['avg_rt']:.1f} | "
        f"合計ブックマーク: {result['total_bookmark']}"
    )
    lines.append("")

    # 時系列一覧
    lines.append("【時系列一覧】")
    jst = timezone(timedelta(hours=9))
    for t in result["timeline"]:
        ts = t["posted_at"].astimezone(jst).strftime("%m/%d %H:%M") if t["posted_at"] else "不明"
        snippet = (t["content"] or "")[:40].replace("\n", " ")
        lines.append(f"{ts} | 👍{t['like']} 👁{t['impression']} 🔁{t['rt']} | {snippet}...")
    lines.append("")

    # 時間帯ベスト3
    hourly = result["hourly"]
    if hourly:
        sorted_hours = sorted(hourly.items(), key=lambda x: x[1]["avg_like"], reverse=True)[:3]
        lines.append("【投稿時間帯ベスト3（いいね平均順）】")
        for h, v in sorted_hours:
            lines.append(f"{h}時台: 平均いいね {v['avg_like']:.1f}（平均インプレ {v['avg_impression']:.0f}）")
        lines.append("")

    # post_type別
    by_type = result["by_type"]
    if by_type:
        lines.append("【post_type別傾向】")
        for pt, v in sorted(by_type.items()):
            lines.append(
                f"{pt}: {v['count']}件 | 平均いいね {v['avg_like']:.1f} | 平均インプレ {v['avg_impression']:.0f}"
            )
        lines.append("")

    # 上位3件
    if result["top3"]:
        lines.append("【直近3日間のトップ投稿】")
        for i, t in enumerate(result["top3"], 1):
            snippet = (t["content"] or "")[:50].replace("\n", " ")
            lines.append(
                f"{i}位: 👍{t['like_count'] or 0} 👁{t['impression_count'] or 0} | {snippet}..."
            )

    return "\n".join(lines)


def send_discord(webhook_url: str, message: str) -> None:
    """2000文字超の場合は分割して送信する。"""
    chunks: list[str] = []
    while len(message) > 2000:
        split_at = message.rfind("\n", 0, 2000)
        if split_at == -1:
            split_at = 2000
        chunks.append(message[:split_at])
        message = message[split_at:].lstrip("\n")
    chunks.append(message)

    for chunk in chunks:
        try:
            resp = requests.post(webhook_url, json={"content": chunk}, timeout=10)
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"⚠️  Discord通知失敗: {exc}", file=sys.stderr)


def find_account_md(x_username: str) -> Path | None:
    """アカウントの .md ファイルを探す（大文字小文字を問わない）。"""
    candidates = [
        ACCOUNTS_DIR / f"{x_username.lower()}.md",
        ACCOUNTS_DIR / f"{x_username}.md",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def update_account_md(x_username: str, result: dict, date_str: str) -> None:
    """accounts/*.md の「## 最近の傾向（自動分析）」セクションを更新する。"""
    md_path = find_account_md(x_username)
    if md_path is None:
        print(f"⚠️  {x_username}: accounts/*.md が見つかりません。スキップします。", file=sys.stderr)
        return

    content = md_path.read_text(encoding="utf-8")

    jst = timezone(timedelta(hours=9))
    hourly = result["hourly"]
    by_type = result["by_type"]
    top3 = result["top3"]

    # 時間帯ベスト2
    best_hours = sorted(hourly.items(), key=lambda x: x[1]["avg_like"], reverse=True)[:2]
    hour_lines = ""
    for i, (h, v) in enumerate(best_hours):
        label = "ベスト" if i == 0 else "次点"
        hour_lines += f"\n- {label}: {h}時台（平均いいね {v['avg_like']:.1f}）"

    # post_type別
    type_lines = ""
    for pt, v in sorted(by_type.items()):
        type_lines += f"\n- {pt}: 平均いいね {v['avg_like']:.1f}、平均インプレ {v['avg_impression']:.0f}"

    # ハイライト
    highlight = ""
    if top3:
        t = top3[0]
        snippet = (t["content"] or "")[:40].replace("\n", " ")
        highlight = f"\n- 最高パフォーマンス: 「{snippet}...」(👍{t['like_count'] or 0} 👁{t['impression_count'] or 0})"

    new_section = f"""## 最近の傾向（自動分析）
最終更新: {date_str}

### 直近3日間サマリー
- 投稿数: {result['total']}件
- 平均いいね: {result['avg_like']:.1f}
- 平均インプレ: {result['avg_impression']:.0f}
- 平均RT: {result['avg_rt']:.1f}
- 合計ブックマーク: {result['total_bookmark']}

### 効果的な投稿時間帯{hour_lines if hour_lines else chr(10) + "- データなし"}

### post_type別傾向{type_lines if type_lines else chr(10) + "- データなし"}

### 直近ハイライト{highlight if highlight else chr(10) + "- データなし"}"""

    # 既存セクションを置換 or 末尾に追加
    pattern = r"## 最近の傾向（自動分析）.*?(?=\n## |\Z)"
    if re.search(pattern, content, flags=re.DOTALL):
        content = re.sub(pattern, new_section, content, flags=re.DOTALL)
    else:
        content = content.rstrip("\n") + "\n\n" + new_section + "\n"

    md_path.write_text(content, encoding="utf-8")
    print(f"✅ {md_path.name} を更新しました")


def run_account(account_cfg: dict, conn) -> None:
    x_username = account_cfg["x_username"]
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=ANALYSIS_WINDOW_DAYS)
    date_str = now.astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")

    print(f"\n▶ @{x_username} を分析中...")

    with conn.cursor() as cur:
        tweets = fetch_tweets_with_latest_metrics(cur, x_username, since)

    print(f"  取得件数: {len(tweets)}件")
    result = analyze(tweets)

    report = format_report(x_username, result, date_str)
    print(report)

    webhook = os.environ.get("DISCORD_WEBHOOK_X_DRAFT")
    if webhook:
        send_discord(webhook, report)
        print("  Discord通知を送信しました")
    else:
        print("  ⚠️  DISCORD_WEBHOOK_X_DRAFT 未設定 — Discord通知スキップ", file=sys.stderr)

    update_account_md(x_username, result, date_str)


def main() -> None:
    # repo直下の .env から環境変数を読み込む
    sys.path.insert(0, str(SCRIPT_DIR))
    env = load_settings_env()
    for k, v in env.items():
        os.environ.setdefault(k, v)

    parser = argparse.ArgumentParser(description="X投稿 日次分析")
    parser.add_argument("--account", help="対象アカウントID（例: GUTARA）。省略時は全アカウント")
    args = parser.parse_args()

    config = load_config()
    accounts = config["accounts"]
    if args.account:
        accounts = [a for a in accounts if a["id"] == args.account]
        if not accounts:
            print(f"❌ アカウントID '{args.account}' が見つかりません", file=sys.stderr)
            sys.exit(1)

    conn = get_db_conn()
    try:
        for account in accounts:
            run_account(account, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
