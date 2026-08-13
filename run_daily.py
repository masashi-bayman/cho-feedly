#!/usr/bin/env python3
"""
毎朝の収集から配信までを通しで実行する。systemd timer から呼ばれる本体。

使い方:
    python3 run_daily.py                            # 収集して配信する（本番）
    python3 run_daily.py --dry-run                  # 収集するが送信しない
    python3 run_daily.py --fixtures tests/fixtures --dry-run   # 通信も送信もしない

処理の順序:
    1. .env を読む
    2. collectors/ で収集する
    3. data/digest.json と data/archive/YYYY-MM-DD.json に書く
    4. render_html.py で PWA サイトを生成する（DIGEST_OUTPUT_DIR がある場合）
    5. publishers/discord.py で配信する

4 を 5 より先に置いているのは、Discord のメッセージに載る digest_url を
タップした先が、届いた時点で既に存在しているようにするため。
3 を先に置いているのは、配信が失敗しても PWA 側が当日分を出せるようにするため。

終了コード:
    0  配信できた（一部の情報源が落ちていても、取れた分を配信できれば 0）
    1  収集が全滅した / 配信に失敗した
    2  設定が読めない

壊れたときの振る舞い:
    - 収集モジュールが 1 つ丸ごと例外を投げても、もう一方の結果で配信を続ける
    - 収集結果が 1 件も無い場合だけ、配信せずに非ゼロで終了する
      （「今日は記事が無い日」と「全部落ちた日」を Discord 上で区別できないため、
        空のメッセージを送るより落ちた方がよい）
    - Webhook URL はログに出さない。万一どこかで混入しても
      ログ出力の直前で伏せ字にする
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent
# 別のディレクトリから呼ばれても collectors / publishers を見つけられるようにする
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import render_html  # noqa: E402
from collectors import feeds, market  # noqa: E402
from envfile import load_dotenv  # noqa: E402
from publishers import discord as discord_publisher  # noqa: E402

LOG = logging.getLogger("run_daily")

JST = timezone(timedelta(hours=9), "JST")

DEFAULT_ENV_PATH = REPO_ROOT / ".env"
DIGEST_PATH = REPO_ROOT / "data" / "digest.json"
ARCHIVE_DIR = REPO_ROOT / "data" / "archive"

# ログに混入した Webhook URL を伏せるための保険
WEBHOOK_RE = re.compile(r"https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/\S+", re.IGNORECASE)
WEBHOOK_MASK = "https://discord.com/api/webhooks/***"


# ---------------------------------------------------------------------------
# 秘匿情報
# ---------------------------------------------------------------------------
class RedactSecrets(logging.Filter):
    """ログに Webhook URL が混ざっていたら伏せ字にする。

    そもそも出力しない作りにしているが、journalctl は残り続けるので
    最後の砦として全レコードを通す。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        if WEBHOOK_RE.search(message):
            record.msg = WEBHOOK_RE.sub(WEBHOOK_MASK, message)
            record.args = ()
        return True


# ---------------------------------------------------------------------------
# 収集
# ---------------------------------------------------------------------------
def _safely(label: str, collect_fn: Callable[[], Any], fallback: Any) -> Any:
    """収集モジュールが丸ごと落ちても、もう一方を巻き込まないようにする。"""
    try:
        return collect_fn()
    except Exception:
        LOG.exception("%s の収集で予期しない例外が出ました。この情報源は諦めます", label)
        return fallback


def build_digest(
    generated_at: datetime,
    fixtures_dir: str | Path | None = None,
) -> dict[str, Any]:
    """digest.json の中身を組み立てる。セクションの並びがそのまま配信順になる。"""
    empty_market = {"id": "market", "title": "マーケット", "emoji": "📈", "type": "market", "items": []}

    market_section = _safely(
        "マーケット",
        lambda: market.collect(fixtures_dir=fixtures_dir),
        empty_market,
    )
    feed_sections = _safely(
        "RSS",
        lambda: feeds.collect(generated_at=generated_at, fixtures_dir=fixtures_dir),
        [],
    )

    base_url = os.environ.get("DIGEST_BASE_URL", "").strip().rstrip("/")
    digest_url = f"{base_url}/{generated_at:%Y-%m-%d}/" if base_url else None

    return {
        "generated_at": generated_at.isoformat(),
        "digest_url": digest_url,
        "sections": [market_section, *feed_sections],
    }


def count_items(digest: dict[str, Any]) -> int:
    return sum(len(section.get("items") or []) for section in digest.get("sections", []))


def summarize(digest: dict[str, Any]) -> str:
    parts = [
        f"{section.get('id')} {len(section.get('items') or [])}件"
        for section in digest.get("sections", [])
    ]
    return " / ".join(parts) if parts else "(セクションなし)"


# ---------------------------------------------------------------------------
# 出力
# ---------------------------------------------------------------------------
def render_site(digest: dict[str, Any], dry_run: bool) -> bool:
    """PWA サイトを生成する。失敗しても Discord への配信は続ける。"""
    output_dir = render_html.resolve_output_dir(None)
    if output_dir is None:
        LOG.info("DIGEST_OUTPUT_DIR が未設定のため、PWA サイトは生成しません")
        return False
    try:
        page = render_html.render_site(digest, output_dir, dry_run=dry_run)
    except Exception:
        LOG.exception("PWA サイトの生成に失敗しました。Discord への配信は続けます")
        return False
    LOG.info("PWA サイトを生成しました: %s", page)
    return True


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    """途中で落ちても壊れたファイルを残さないよう、書いてから差し替える。

    PWA 側が同じファイルを読むため、半端な JSON を見せない。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    temporary.replace(path)


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="収集から Discord 配信までを通しで実行します")
    parser.add_argument("--dry-run", action="store_true", help="収集はするが送信しない。ファイルも書かない")
    parser.add_argument("--fixtures", metavar="DIR", help="保存済みの応答から収集する（通信しない）")
    parser.add_argument("--generated-at", metavar="ISO8601", help="基準時刻（既定: 現在時刻）")
    parser.add_argument("--env", default=str(DEFAULT_ENV_PATH), help=".env のパス")
    parser.add_argument(
        "--max-items",
        type=int,
        default=discord_publisher.DEFAULT_MAX_ITEMS,
        help=f"Discord のセクションごとの表示件数 (既定: {discord_publisher.DEFAULT_MAX_ITEMS})",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug ログまで出す")
    args = parser.parse_args(argv)

    # journald が時刻を付けるので、こちらでは付けない
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactSecrets())

    if args.generated_at:
        try:
            generated_at = datetime.fromisoformat(args.generated_at)
        except ValueError:
            LOG.error("--generated-at を解釈できません: %s", args.generated_at)
            return 2
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=JST)
    else:
        generated_at = datetime.now(JST)

    load_dotenv(Path(args.env))

    mode = "fixtures" if args.fixtures else "network"
    LOG.info("収集を開始します (基準時刻 %s / %s)", generated_at.isoformat(), mode)

    digest = build_digest(generated_at, fixtures_dir=args.fixtures)
    total = count_items(digest)
    LOG.info("収集結果: %s (計 %d件)", summarize(digest), total)

    # 「今日は記事が無い日」と「全部落ちた日」は Discord 上で区別が付かない。
    # 空のメッセージを送るより、落ちて journalctl に残す方がよい
    if total == 0:
        LOG.error("収集結果が 1 件もありません。配信を中止します")
        return 1

    if not args.dry_run:
        write_json_atomic(DIGEST_PATH, digest)
        archive_path = ARCHIVE_DIR / f"{generated_at:%Y-%m-%d}.json"
        write_json_atomic(archive_path, digest)
        LOG.info("%s と %s に書き出しました", DIGEST_PATH, archive_path)

    # PWA サイトの生成。Discord のメッセージに載る digest_url をタップした先が、
    # 届いた時点で既に存在しているよう配信より前に行う。
    # ここが失敗しても Discord への配信は続ける（片方だけでも届いた方がよい）
    render_site(digest, dry_run=args.dry_run)

    options = discord_publisher.Options(max_items=args.max_items, dry_run=args.dry_run)
    payload = discord_publisher.build_payload(digest, options)

    if not payload["embeds"]:
        LOG.error("配信できる Embed がありません。配信を中止します")
        return 1

    used = sum(discord_publisher.embed_char_count(e) for e in payload["embeds"])
    LOG.info(
        "Embed %d 個 / %d文字 (上限 %d)",
        len(payload["embeds"]),
        used,
        discord_publisher.MAX_TOTAL_EMBED_CHARS,
    )

    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        LOG.info("--dry-run のため送信しませんでした")
        return 0

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        LOG.error("DISCORD_WEBHOOK_URL が未設定です。.env を確認してください")
        return 1

    try:
        discord_publisher.post_to_discord(webhook_url, payload, options)
    except discord_publisher.PublishError as exc:
        # PublishError に URL は含めない設計だが、念のためここでも伏せる
        LOG.error("配信に失敗しました: %s", WEBHOOK_RE.sub(WEBHOOK_MASK, str(exc)))
        return 1

    LOG.info("配信しました: %s", summarize(digest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
