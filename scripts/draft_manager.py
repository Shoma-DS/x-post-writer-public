# Neon DBに下書きを保存・一覧表示・削除し、画像URLも更新できるスクリプト。
# 単発投稿・ツリー（スレッド）形式に対応。投稿は手動で行う運用。
# 使い方: python draft_manager.py [save|list|show|delete|update-image] [オプション]

import argparse
import os
import sys
import json
from datetime import datetime, timezone
from pathlib import Path
import psycopg2
from psycopg2.extras import RealDictCursor

SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "accounts_config.json"


def get_db_conn():
    url = os.environ.get("NEON_DATABASE_URL")
    if not url:
        raise RuntimeError("NEON_DATABASE_URL が設定されていません")
    return psycopg2.connect(url)


def load_config():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        raw = "".join(l for l in f if not l.strip().startswith("//"))
        return json.loads(raw)


def get_account_id(cur, x_username):
    cur.execute("SELECT account_id FROM accounts WHERE x_username = %s", (x_username,))
    row = cur.fetchone()
    if not row:
        raise ValueError(
            f"アカウント '{x_username}' がDBに見つかりません。先に x_metrics_collector.py を実行してください。"
        )
    return row["account_id"]


def get_account_cfg(account_id):
    config = load_config()
    account_cfg = next((a for a in config["accounts"] if a["id"] == account_id), None)
    if not account_cfg:
        ids = [a["id"] for a in config["accounts"]]
        raise ValueError(f"アカウントID '{account_id}' が見つかりません。使えるID: {ids}")
    return account_cfg


def save_draft(account_id, parts_text, memo=None):
    account_cfg = get_account_cfg(account_id)
    normalized_parts = [p.strip() for p in parts_text if p and p.strip()]
    if not normalized_parts:
        raise ValueError("投稿文が空です")

    conn = get_db_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            db_account_id = get_account_id(cur, account_cfg["x_username"])

            cur.execute(
                "INSERT INTO drafts (account_id, memo) VALUES (%s, %s) RETURNING draft_id",
                (db_account_id, memo or None),
            )
            draft_id = str(cur.fetchone()["draft_id"])

            for i, content in enumerate(normalized_parts, start=1):
                cur.execute(
                    "INSERT INTO draft_parts (draft_id, position, content) VALUES (%s, %s, %s)",
                    (draft_id, i, content),
                )

            conn.commit()
            return draft_id
    finally:
        conn.close()


def update_draft_image(draft_id, position, image_url):
    conn = get_db_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE draft_parts
                   SET image_url = %s
                 WHERE draft_id = %s AND position = %s
             RETURNING part_id
                """,
                (image_url, draft_id, position),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"draft_id '{draft_id}' の position={position} が見つかりません")
            conn.commit()
    finally:
        conn.close()


def cmd_save(args):
    """下書きを保存する"""
    # 投稿文を読み込む（ファイルまたは標準入力）
    if args.file:
        parts_text = Path(args.file).read_text(encoding="utf-8").strip().split("\n---\n")
    elif args.text:
        parts_text = args.text.split("\\n---\\n")
    else:
        print("投稿文を入力してください（ツリーは --- で区切る）。入力完了は Ctrl+D:")
        parts_text = sys.stdin.read().strip().split("\n---\n")

    parts_text = [p.strip() for p in parts_text if p.strip()]
    if not parts_text:
        print("❌ 投稿文が空です", file=sys.stderr)
        sys.exit(1)

    try:
        draft_id = save_draft(args.account, parts_text, memo=args.memo)
    except (RuntimeError, ValueError, psycopg2.Error) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(1)

    post_type = "ツリー" if len(parts_text) > 1 else "単発"
    print(f"✅ 下書きを保存しました（{post_type}・{len(parts_text)}パーツ）")
    print(f"   draft_id: {draft_id}")


def cmd_list(args):
    """下書き一覧を表示する"""
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT d.draft_id, a.x_username, d.status, d.memo, d.created_at,
                       COUNT(p.part_id) AS parts_count
                FROM drafts d
                JOIN accounts a ON d.account_id = a.account_id
                JOIN draft_parts p ON d.draft_id = p.draft_id
                WHERE d.status = 'draft'
                GROUP BY d.draft_id, a.x_username, d.status, d.memo, d.created_at
                ORDER BY d.created_at DESC
            """)
            rows = cur.fetchall()
        conn.close()
    except (RuntimeError, psycopg2.Error) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(1)

    if not rows:
        print("下書きはありません。")
        return

    print(f"\n{'='*60}")
    print(f"{'#':<4} {'アカウント':<20} {'パーツ':<6} {'作成日時':<20} メモ")
    print(f"{'='*60}")
    for i, row in enumerate(rows, 1):
        created = row["created_at"].strftime("%m/%d %H:%M")
        memo = (row["memo"] or "")[:20]
        parts = f"{row['parts_count']}件" + ("（ツリー）" if row["parts_count"] > 1 else "")
        print(f"{i:<4} @{row['x_username']:<19} {parts:<10} {created:<20} {memo}")
        print(f"     draft_id: {row['draft_id']}")
    print(f"{'='*60}")
    print(f"合計 {len(rows)} 件の下書き\n")


def cmd_show(args):
    """下書きの内容を表示する"""
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT d.draft_id, a.x_username, d.status, d.memo, d.created_at
                FROM drafts d JOIN accounts a ON d.account_id = a.account_id
                WHERE d.draft_id = %s
            """, (args.draft_id,))
            draft = cur.fetchone()
            if not draft:
                print(f"❌ draft_id '{args.draft_id}' が見つかりません", file=sys.stderr)
                sys.exit(1)

            cur.execute(
                "SELECT position, content, image_url FROM draft_parts WHERE draft_id = %s ORDER BY position",
                (args.draft_id,)
            )
            parts = cur.fetchall()
        conn.close()
    except (RuntimeError, psycopg2.Error) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"アカウント : @{draft['x_username']}")
    print(f"作成日時   : {draft['created_at'].strftime('%Y-%m-%d %H:%M')}")
    print(f"メモ       : {draft['memo'] or 'なし'}")
    print(f"{'='*60}")
    for part in parts:
        if len(parts) > 1:
            print(f"\n[{part['position']}/{len(parts)}]")
        print(part["content"])
        if part["image_url"]:
            print(f"🖼 {part['image_url']}")
    print(f"\n{'='*60}\n")


def cmd_delete(args):
    """下書きを削除する"""
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM drafts WHERE draft_id = %s RETURNING draft_id", (args.draft_id,))
            if not cur.fetchone():
                print(f"❌ draft_id '{args.draft_id}' が見つかりません", file=sys.stderr)
                conn.close()
                sys.exit(1)
            conn.commit()
        conn.close()
    except (RuntimeError, psycopg2.Error) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"🗑  下書きを削除しました（draft_id: {args.draft_id}）")


def cmd_update_image(args):
    """下書きパーツの image_url を更新する"""
    image_url = None if args.clear else args.image_url

    try:
        update_draft_image(args.draft_id, args.position, image_url)
    except (RuntimeError, ValueError, psycopg2.Error) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(1)

    if image_url:
        print(f"🖼 image_url を更新しました（draft_id: {args.draft_id}, position: {args.position}）")
        print(f"   {image_url}")
    else:
        print(f"🧹 image_url をクリアしました（draft_id: {args.draft_id}, position: {args.position}）")


def main():
    parser = argparse.ArgumentParser(description="X 投稿下書き管理ツール")
    sub = parser.add_subparsers(dest="command")

    # save
    p_save = sub.add_parser("save", help="下書きを保存する")
    p_save.add_argument("--account", required=True, help="アカウントID（例: GUTARA）")
    p_save.add_argument("--text", help="投稿文（ツリーは \\n---\\n で区切る）")
    p_save.add_argument("--file", help="投稿文が書かれたテキストファイルのパス（--- で区切るとツリー）")
    p_save.add_argument("--memo", help="メモ（管理用）")

    # list
    sub.add_parser("list", help="下書き一覧を表示する")

    # show
    p_show = sub.add_parser("show", help="下書きの内容を表示する")
    p_show.add_argument("draft_id", help="draft_id")

    # delete
    p_del = sub.add_parser("delete", help="下書きを削除する")
    p_del.add_argument("draft_id", help="draft_id")

    # update-image
    p_img = sub.add_parser("update-image", help="下書きパーツの image_url を更新する")
    p_img.add_argument("draft_id", help="draft_id")
    p_img.add_argument("--position", type=int, default=1, help="対象パーツ番号（デフォルト: 1）")
    img_group = p_img.add_mutually_exclusive_group(required=True)
    img_group.add_argument("--image-url", help="保存する画像URLまたはローカル配信URL")
    img_group.add_argument("--clear", action="store_true", help="image_url を空にする")

    args = parser.parse_args()

    if args.command == "save":
        cmd_save(args)
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "show":
        cmd_show(args)
    elif args.command == "delete":
        cmd_delete(args)
    elif args.command == "update-image":
        cmd_update_image(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
