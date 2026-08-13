#!/usr/bin/env python3
"""
digest.json を Discord Webhook へ配信する。

使い方:
    export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
    python discord_publisher.py data/sample_digest.json
    python discord_publisher.py data/sample_digest.json --dry-run

設計方針:
    - 1 日分を「1 メッセージ」に収める。Discord モバイルは受信済みメッセージを
      ローカルにキャッシュするため、圏外でも読み返せる可能性が高くなる。
    - セクション = Embed。1 メッセージあたり Embed は最大 10 個まで。
    - 収集側とは digest.json のスキーマだけで結合する（配信側は取得方法を知らない）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# ---------------------------------------------------------------------------
# Discord API の上限値（超えると 400 が返る）
# ---------------------------------------------------------------------------
MAX_EMBEDS_PER_MESSAGE = 10
MAX_EMBED_DESCRIPTION = 4096
MAX_EMBED_FIELD_VALUE = 1024
MAX_EMBED_FIELDS = 25
MAX_CONTENT = 2000
MAX_TOTAL_EMBED_CHARS = 6000

# セクション ID ごとの色（未定義は既定色）
SECTION_COLORS: dict[str, int] = {
    "market": 0x2E8B57,
    "news": 0x4169E1,
    "game": 0xD2691E,
    "ai": 0x8A2BE2,
}
DEFAULT_COLOR = 0x5865F2

DEFAULT_MAX_ITEMS = 6
USER_AGENT = "daily-digest-publisher/0.1"


class PublishError(RuntimeError):
    """配信に失敗したことを表す。"""


@dataclass
class Options:
    max_items: int = DEFAULT_MAX_ITEMS
    dry_run: bool = False
    timeout: int = 20
    max_retries: int = 3


# ---------------------------------------------------------------------------
# 文字列ユーティリティ
# ---------------------------------------------------------------------------
def truncate(text: str, limit: int) -> str:
    """limit を超える場合は末尾を … に置き換えて丸める。"""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def escape_markdown_link_text(text: str) -> str:
    """リンクラベル内で表示が壊れる文字を無害化する。"""
    return text.replace("[", "［").replace("]", "］").replace("\n", " ").strip()


def format_jst(iso_string: str | None) -> str:
    """ISO 8601 文字列を HH:MM に整形する。失敗したら空文字。"""
    if not iso_string:
        return ""
    try:
        return datetime.fromisoformat(iso_string).strftime("%H:%M")
    except ValueError:
        return ""


# ---------------------------------------------------------------------------
# Embed 組み立て
# ---------------------------------------------------------------------------
def build_links_embed(section: dict[str, Any], opts: Options) -> dict[str, Any] | None:
    """記事リンク型セクションを 1 つの Embed にする。"""
    items = section.get("items") or []
    if not items:
        return None

    lines: list[str] = []
    for item in items[: opts.max_items]:
        title = escape_markdown_link_text(str(item.get("title", "")).strip())
        url = str(item.get("url", "")).strip()
        if not title or not url:
            continue

        # タイトルが長すぎるとモバイルで折り返しが増えるので抑える
        label = truncate(title, 90)
        line = f"・[{label}]({url})"

        meta_parts = [p for p in (item.get("source"), format_jst(item.get("published_at"))) if p]
        if meta_parts:
            line += f"\n　　-# {' | '.join(str(p) for p in meta_parts)}"

        # 要約は将来の拡張。存在すればリンクの下にぶら下げる
        summary = item.get("summary")
        if summary:
            line += f"\n　　{truncate(str(summary).strip(), 160)}"

        lines.append(line)

    if not lines:
        return None

    description = truncate("\n".join(lines), MAX_EMBED_DESCRIPTION)
    emoji = section.get("emoji", "")
    title = f"{emoji} {section.get('title', section.get('id', ''))}".strip()

    embed: dict[str, Any] = {
        "title": truncate(title, 256),
        "description": description,
        "color": SECTION_COLORS.get(str(section.get("id")), DEFAULT_COLOR),
    }

    omitted = len(items) - min(len(items), opts.max_items)
    if omitted > 0:
        embed["footer"] = {"text": f"ほか {omitted} 件"}
    return embed


def build_market_embed(section: dict[str, Any], opts: Options) -> dict[str, Any] | None:
    """価格型セクションを Embed の inline フィールドとして並べる。"""
    items = section.get("items") or []
    if not items:
        return None

    fields: list[dict[str, Any]] = []
    for item in items[:MAX_EMBED_FIELDS]:
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        value = str(item.get("value", "-"))
        change = str(item.get("change", "")).strip()

        if change.startswith("+"):
            mark = "🔺"
        elif change.startswith("-"):
            mark = "🔻"
        else:
            mark = "▪️"

        body = f"**{value}**"
        if change:
            body += f"\n{mark} {change}"

        fields.append(
            {
                "name": truncate(name, 256),
                "value": truncate(body, MAX_EMBED_FIELD_VALUE),
                "inline": True,
            }
        )

    if not fields:
        return None

    emoji = section.get("emoji", "")
    return {
        "title": truncate(f"{emoji} {section.get('title', '')}".strip(), 256),
        "fields": fields,
        "color": SECTION_COLORS.get(str(section.get("id")), DEFAULT_COLOR),
    }


def embed_char_count(embed: dict[str, Any]) -> int:
    """Discord が数える Embed の合計文字数を概算する。"""
    total = len(embed.get("title", "")) + len(embed.get("description", ""))
    total += len(embed.get("footer", {}).get("text", ""))
    for field in embed.get("fields", []):
        total += len(field.get("name", "")) + len(field.get("value", ""))
    return total


def build_payload(digest: dict[str, Any], opts: Options) -> dict[str, Any]:
    """digest.json から Webhook のリクエストボディを組み立てる。"""
    generated_at = digest.get("generated_at")
    try:
        header_date = datetime.fromisoformat(generated_at).strftime("%Y/%m/%d (%a) %H:%M")
    except (TypeError, ValueError):
        header_date = str(generated_at or "")

    content_lines = [f"## 📬 デイリーダイジェスト  {header_date}"]
    if digest.get("digest_url"):
        content_lines.append(f"全文はこちら → {digest['digest_url']}")
    content = truncate("\n".join(content_lines), MAX_CONTENT)

    embeds: list[dict[str, Any]] = []
    running_total = 0

    for section in digest.get("sections", []):
        if len(embeds) >= MAX_EMBEDS_PER_MESSAGE:
            break

        if section.get("type") == "market":
            embed = build_market_embed(section, opts)
        else:
            embed = build_links_embed(section, opts)

        if embed is None:
            continue

        size = embed_char_count(embed)
        if running_total + size > MAX_TOTAL_EMBED_CHARS:
            # 上限を超えるセクションは切り捨てる（400 で全滅させない）
            break
        embeds.append(embed)
        running_total += size

    return {
        "content": content,
        "embeds": embeds,
        "allowed_mentions": {"parse": []},
    }


# ---------------------------------------------------------------------------
# 送信
# ---------------------------------------------------------------------------
def post_to_discord(webhook_url: str, payload: dict[str, Any], opts: Options) -> None:
    """429 の Retry-After を尊重しつつ Webhook へ POST する。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    for attempt in range(1, opts.max_retries + 1):
        request = urllib.request.Request(
            webhook_url,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=opts.timeout) as response:
                if 200 <= response.status < 300:
                    return
                raise PublishError(f"想定外のステータス: {response.status}")

        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")

            if exc.code == 429:
                wait = 5.0
                try:
                    wait = float(json.loads(detail).get("retry_after", wait))
                except (ValueError, AttributeError):
                    pass
                print(f"[warn] レート制限。{wait:.1f} 秒待機します", file=sys.stderr)
                time.sleep(wait + 0.5)
                continue

            if 500 <= exc.code < 600 and attempt < opts.max_retries:
                wait = 2**attempt
                print(f"[warn] {exc.code} を受信。{wait} 秒後に再試行します", file=sys.stderr)
                time.sleep(wait)
                continue

            raise PublishError(f"HTTP {exc.code}: {truncate(detail, 500)}") from exc

        except urllib.error.URLError as exc:
            if attempt < opts.max_retries:
                wait = 2**attempt
                print(f"[warn] 通信失敗 ({exc.reason})。{wait} 秒後に再試行します", file=sys.stderr)
                time.sleep(wait)
                continue
            raise PublishError(f"通信失敗: {exc.reason}") from exc

    raise PublishError("再試行の上限に達しました")


# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="digest.json を Discord へ配信します")
    parser.add_argument("digest", help="digest.json のパス")
    parser.add_argument("--webhook-url", default=os.environ.get("DISCORD_WEBHOOK_URL"))
    parser.add_argument("--max-items", type=int, default=DEFAULT_MAX_ITEMS,
                        help=f"セクションごとの最大件数 (既定: {DEFAULT_MAX_ITEMS})")
    parser.add_argument("--dry-run", action="store_true", help="送信せずペイロードを表示")
    args = parser.parse_args()

    opts = Options(max_items=args.max_items, dry_run=args.dry_run)

    try:
        with open(args.digest, encoding="utf-8") as f:
            digest = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[error] digest の読み込みに失敗しました: {exc}", file=sys.stderr)
        return 1

    payload = build_payload(digest, opts)

    if not payload["embeds"]:
        print("[warn] 配信対象がありません。送信を中止します", file=sys.stderr)
        return 0

    if opts.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print(
            f"\n[dry-run] embeds={len(payload['embeds'])} "
            f"chars={sum(embed_char_count(e) for e in payload['embeds'])}/{MAX_TOTAL_EMBED_CHARS}",
            file=sys.stderr,
        )
        return 0

    if not args.webhook_url:
        print("[error] DISCORD_WEBHOOK_URL が未設定です", file=sys.stderr)
        return 1

    try:
        post_to_discord(args.webhook_url, payload, opts)
    except PublishError as exc:
        print(f"[error] 配信に失敗しました: {exc}", file=sys.stderr)
        return 1

    print(f"[ok] {len(payload['embeds'])} セクションを配信しました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
