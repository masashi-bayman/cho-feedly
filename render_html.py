#!/usr/bin/env python3
"""
digest.json から静的 HTML を生成し、ポータルサイトの公開ディレクトリへ出力する。

使い方:
    python3 render_html.py                                  # data/digest.json → DIGEST_OUTPUT_DIR
    python3 render_html.py data/sample_digest.json --output ./site
    python3 render_html.py --dry-run                        # 書かずに、書く予定の一覧を出す

出力される構成:
    <出力先>/
      index.html                 過去分へのリンク一覧
      2026-08-13/index.html      当日ページ
      2026-08-13/meta.json       index を組み立てるための件数情報
      sw.js                      Service Worker
      manifest.webmanifest
      icon.svg

設計方針:
    - **CSS は各ページに埋め込む。** 外部ファイルにすると、そのリクエストだけ
      キャッシュを外したときに素の HTML が表示される。ページ 1 枚で完結させれば
      その経路が消える。通勤電車内で開けないのは失敗とみなす、という要件が最優先。
    - Service Worker のキャッシュ名は日付で変えない。日付で変えると、
      昨日まで読めていたページが今日のキャッシュ更新で消える。
    - JavaScript はページ本体には使わない。Service Worker の登録だけ。
      表示に JS が要ると、JS が落ちた瞬間に何も読めなくなる。

注意:
    Service Worker は HTTPS（または localhost）でのみ動作する。
    Cloudflare Tunnel 経由の公開 URL では動くが、Pi のローカル IP に
    http で直接アクセスした場合は登録されない。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from envfile import load_dotenv  # noqa: E402

LOG = logging.getLogger("render_html")

DEFAULT_DIGEST_PATH = REPO_ROOT / "data" / "digest.json"
DEFAULT_ENV_PATH = REPO_ROOT / ".env"

DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

SITE_TITLE = "デイリーダイジェスト"

# セクション id ごとの色。publishers/discord.py の SECTION_COLORS と揃えている
SECTION_ACCENTS: dict[str, str] = {
    "market": "#2e8b57",
    "news": "#4169e1",
    "game": "#d2691e",
    "ai": "#8a2be2",
}
DEFAULT_ACCENT = "#5865f2"


# ---------------------------------------------------------------------------
# スタイル
# ---------------------------------------------------------------------------
STYLESHEET = """
*, *::before, *::after { box-sizing: border-box; }
:root {
  --bg: #f6f7f9;
  --surface: #ffffff;
  --border: #e2e5ea;
  --text: #1a1c1f;
  --muted: #6b7280;
  --up: #128a4a;
  --down: #c0392b;
  color-scheme: light dark;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14161a;
    --surface: #1c1f24;
    --border: #2b2f36;
    --text: #e8eaed;
    --muted: #9aa1ab;
    --up: #4ade80;
    --down: #f87171;
  }
}
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Hiragino Sans", "Noto Sans JP",
               "Yu Gothic UI", sans-serif;
  font-size: 16px;
  line-height: 1.6;
  overflow-wrap: anywhere;
}
.wrap { max-width: 720px; margin: 0 auto; padding: 0 12px 64px; }

/* 日付とセクション見出しへのジャンプ。1 画面でセクションを俯瞰させる */
.top {
  position: sticky; top: 0; z-index: 10;
  background: var(--bg);
  padding: 12px 0 8px;
  border-bottom: 1px solid var(--border);
}
.date { font-size: 1.05rem; font-weight: 700; margin: 0 0 8px; }
.date small { font-weight: 400; color: var(--muted); margin-left: 6px; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 0; padding: 0; list-style: none; }
.chip {
  display: inline-flex; align-items: center; gap: 4px;
  /* 揺れる電車内で押せるよう、タップ領域は 44px 以上にする */
  min-height: 44px; padding: 0 12px;
  border: 1px solid var(--border); border-radius: 999px;
  background: var(--surface); color: var(--text);
  text-decoration: none; font-size: 0.85rem; white-space: nowrap;
}
.chip b { font-weight: 700; }
.chip .n { color: var(--muted); font-variant-numeric: tabular-nums; }

section { margin: 22px 0 0; scroll-margin-top: 92px; }
h2 {
  font-size: 1rem; margin: 0 0 10px;
  padding-left: 9px; border-left: 4px solid var(--accent, #5865f2);
}
h2 .n { color: var(--muted); font-weight: 400; font-size: 0.85rem; margin-left: 6px; }

/* マーケット: 数字なので一覧性優先で並べる */
.tiles { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 8px; }
.tile { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px; }
.tile .name { font-size: 0.78rem; color: var(--muted); }
.tile .value { font-size: 1.15rem; font-weight: 700; font-variant-numeric: tabular-nums; }
.tile .chg { font-size: 0.85rem; font-variant-numeric: tabular-nums; }
.up { color: var(--up); }
.down { color: var(--down); }
.flat { color: var(--muted); }

/* 記事一覧 */
ol.items { list-style: none; margin: 0; padding: 0; }
ol.items li { border-bottom: 1px solid var(--border); }
ol.items li:last-child { border-bottom: 0; }
ol.items a {
  display: block; padding: 11px 2px; color: inherit; text-decoration: none;
}
ol.items a:active { background: var(--surface); }
.title { display: block; }
.meta { display: block; font-size: 0.75rem; color: var(--muted); margin-top: 3px; }
.summary { display: block; font-size: 0.85rem; color: var(--muted); margin-top: 4px; }
.empty { color: var(--muted); font-size: 0.9rem; margin: 0; }

/* index */
ul.days { list-style: none; margin: 0; padding: 0; }
ul.days li { border-bottom: 1px solid var(--border); }
ul.days a { display: flex; justify-content: space-between; gap: 12px;
            padding: 13px 2px; color: inherit; text-decoration: none; }
ul.days .n { color: var(--muted); font-size: 0.8rem; white-space: nowrap; }

footer { margin-top: 32px; color: var(--muted); font-size: 0.75rem; }
footer a { color: inherit; }
.back { display: inline-block; margin-top: 20px; font-size: 0.85rem; color: var(--muted); }
"""


def _minify_css(css: str) -> str:
    """コメントと余分な空白を落とす。ページごとに埋め込むので少しでも軽くする。"""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    css = re.sub(r"\s+", " ", css)
    css = re.sub(r"\s*([{}:;,>])\s*", r"\1", css)
    return css.strip()


# ---------------------------------------------------------------------------
# 部品
# ---------------------------------------------------------------------------
def change_class(change: str) -> str:
    change = (change or "").strip()
    if change.startswith("+"):
        return "up"
    if change.startswith("-"):
        return "down"
    return "flat"


def change_mark(change: str) -> str:
    change = (change or "").strip()
    if change.startswith("+"):
        return "▲"
    if change.startswith("-"):
        return "▼"
    return "―"


def format_time(iso_string: str | None) -> str:
    """published_at を HH:MM にする。無ければ空文字（時刻欄ごと出さない）。"""
    if not iso_string:
        return ""
    try:
        return datetime.fromisoformat(iso_string).strftime("%H:%M")
    except (TypeError, ValueError):
        return ""


def format_date_heading(iso_string: str | None) -> tuple[str, str]:
    """(見出し用の日付, 生成時刻) を返す。"""
    weekdays = "月火水木金土日"
    try:
        stamp = datetime.fromisoformat(str(iso_string))
    except (TypeError, ValueError):
        return str(iso_string or ""), ""
    return f"{stamp:%Y/%m/%d}（{weekdays[stamp.weekday()]}）", f"{stamp:%H:%M} 更新"


def digest_date(digest: dict[str, Any]) -> str:
    """YYYY-MM-DD。ディレクトリ名になる。"""
    try:
        return f"{datetime.fromisoformat(str(digest.get('generated_at'))):%Y-%m-%d}"
    except (TypeError, ValueError):
        return datetime.now().strftime("%Y-%m-%d")


def render_market_section(section: dict[str, Any]) -> str:
    tiles = []
    for item in section.get("items") or []:
        name = escape(str(item.get("name", "")))
        value = escape(str(item.get("value", "-")))
        change = str(item.get("change", "") or "")
        if change:
            chg = (
                f'<div class="chg {change_class(change)}">'
                f"{change_mark(change)} {escape(change)}</div>"
            )
        else:
            chg = ""
        tiles.append(f'<div class="tile"><div class="name">{name}</div>'
                     f'<div class="value">{value}</div>{chg}</div>')
    return f'<div class="tiles">{"".join(tiles)}</div>'


def render_links_section(section: dict[str, Any]) -> str:
    rows = []
    for item in section.get("items") or []:
        title = str(item.get("title", "")).strip()
        url = str(item.get("url", "")).strip()
        if not title or not url:
            continue

        meta_parts = [p for p in (str(item.get("source") or ""), format_time(item.get("published_at"))) if p]
        meta = f'<span class="meta">{escape(" ・ ".join(meta_parts))}</span>' if meta_parts else ""

        summary = str(item.get("summary") or "").strip()
        summary_html = f'<span class="summary">{escape(summary)}</span>' if summary else ""

        rows.append(
            f'<li><a href="{escape(url, quote=True)}" rel="noopener">'
            f'<span class="title">{escape(title)}</span>{meta}{summary_html}</a></li>'
        )
    if not rows:
        return '<p class="empty">この時間帯の更新はありませんでした。</p>'
    return f'<ol class="items">{"".join(rows)}</ol>'


def render_page_shell(title: str, body: str, sw_path: str) -> str:
    """全ページ共通の外枠。CSS は埋め込む。"""
    return (
        "<!DOCTYPE html>\n"
        '<html lang="ja">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="color-scheme" content="light dark">\n'
        f"<title>{escape(title)}</title>\n"
        f'<link rel="manifest" href="{escape(sw_path, quote=True)}manifest.webmanifest">\n'
        f'<link rel="icon" href="{escape(sw_path, quote=True)}icon.svg" type="image/svg+xml">\n'
        f"<style>{_minify_css(STYLESHEET)}</style>\n"
        "</head>\n<body>\n"
        f'<div class="wrap">{body}</div>\n'
        # 表示は JS に依存させない。ここは Service Worker の登録だけ
        "<script>"
        'if("serviceWorker" in navigator){'
        'window.addEventListener("load",function(){'
        f'navigator.serviceWorker.register("{sw_path}sw.js").catch(function(){{}});'
        "});}"
        "</script>\n"
        "</body>\n</html>\n"
    )


def render_day_page(digest: dict[str, Any]) -> str:
    sections = [s for s in digest.get("sections", []) if s.get("items")]
    heading, updated = format_date_heading(digest.get("generated_at"))

    chips = []
    for section in sections:
        emoji = escape(str(section.get("emoji") or ""))
        title = escape(str(section.get("title") or section.get("id") or ""))
        count = len(section.get("items") or [])
        chips.append(
            f'<li><a class="chip" href="#s-{escape(str(section.get("id")), quote=True)}">'
            f'{emoji}<b>{title}</b><span class="n">{count}</span></a></li>'
        )

    blocks = []
    for section in sections:
        section_id = escape(str(section.get("id", "")), quote=True)
        accent = SECTION_ACCENTS.get(str(section.get("id")), DEFAULT_ACCENT)
        emoji = escape(str(section.get("emoji") or ""))
        title = escape(str(section.get("title") or section.get("id") or ""))
        count = len(section.get("items") or [])
        inner = (
            render_market_section(section)
            if section.get("type") == "market"
            else render_links_section(section)
        )
        blocks.append(
            f'<section id="s-{section_id}" style="--accent:{accent}">'
            f'<h2>{emoji} {title}<span class="n">{count}</span></h2>{inner}</section>'
        )

    if not blocks:
        blocks.append('<p class="empty">この日の記録はありません。</p>')

    body = (
        '<div class="top">'
        f'<h1 class="date">{escape(heading)}<small>{escape(updated)}</small></h1>'
        f'<ul class="chips">{"".join(chips)}</ul>'
        "</div>"
        + "".join(blocks)
        + '<a class="back" href="../">← 過去分の一覧</a>'
        f"<footer>{escape(SITE_TITLE)}</footer>"
    )
    return render_page_shell(f"{heading} — {SITE_TITLE}", body, "../")


def render_index_page(days: list[dict[str, Any]]) -> str:
    rows = []
    for day in days:
        date = escape(str(day["date"]), quote=True)
        total = day.get("total", 0)
        heading, _ = format_date_heading(day.get("generated_at"))
        rows.append(
            f'<li><a href="{date}/"><span>{escape(heading or date)}</span>'
            f'<span class="n">{total} 件</span></a></li>'
        )

    if rows:
        listing = f'<ul class="days">{"".join(rows)}</ul>'
    else:
        listing = '<p class="empty">まだ記録がありません。</p>'

    body = (
        '<div class="top"><h1 class="date">' + escape(SITE_TITLE) + "</h1></div>"
        f"<section>{listing}</section>"
        f"<footer>毎朝 06:00 に更新されます</footer>"
    )
    return render_page_shell(SITE_TITLE, body, "./")


def render_service_worker(precache: list[str]) -> str:
    """Service Worker。

    キャッシュ名は日付で変えない。日付で変えて古いキャッシュを消すと、
    昨日まで読めていたページが今朝の更新で消えてしまう。過去分もオフラインで
    読めることが要件なので、1 つのキャッシュに積み上げていく。
    """
    urls = json.dumps(precache, ensure_ascii=False)
    return f"""// daily-digest service worker
const CACHE = "daily-digest-v1";
const PRECACHE = {urls};

self.addEventListener("install", (event) => {{
  event.waitUntil((async () => {{
    const cache = await caches.open(CACHE);
    // 1 つ失敗しても他は入れる。addAll だと 1 つの 404 で全滅する
    await Promise.all(PRECACHE.map((url) => cache.add(url).catch(() => {{}})));
    await self.skipWaiting();
  }})());
}});

self.addEventListener("activate", (event) => {{
  event.waitUntil((async () => {{
    const names = await caches.keys();
    // 名前が変わったときだけ古いものを片付ける。通常の更新では消さない
    await Promise.all(names.filter((n) => n !== CACHE).map((n) => caches.delete(n)));
    await self.clients.claim();
  }})());
}});

self.addEventListener("fetch", (event) => {{
  const request = event.request;
  if (request.method !== "GET") return;
  if (new URL(request.url).origin !== self.location.origin) return;

  const isPage = request.mode === "navigate" ||
    (request.headers.get("accept") || "").includes("text/html");

  if (isPage) {{
    // ページは新しい方を優先し、取れなければキャッシュを出す
    event.respondWith((async () => {{
      try {{
        const response = await fetch(request);
        if (response && response.ok) {{
          const cache = await caches.open(CACHE);
          cache.put(request, response.clone());
        }}
        return response;
      }} catch (e) {{
        const cached = await caches.match(request, {{ ignoreSearch: true }});
        if (cached) return cached;
        return new Response(
          "<!DOCTYPE html><meta charset=utf-8><title>オフライン</title>" +
          "<p style='font-family:sans-serif;padding:24px'>" +
          "このページはまだ保存されていません。<br>電波が届く場所で開くと、次からは圏外でも読めます。</p>",
          {{ headers: {{ "Content-Type": "text/html; charset=utf-8" }} }}
        );
      }}
    }})());
    return;
  }}

  // それ以外は保存済みを優先。取得できたら保存しておく
  event.respondWith((async () => {{
    const cached = await caches.match(request);
    if (cached) return cached;
    const response = await fetch(request);
    if (response && response.ok) {{
      const cache = await caches.open(CACHE);
      cache.put(request, response.clone());
    }}
    return response;
  }})());
}});
"""


def render_manifest() -> str:
    return json.dumps(
        {
            "name": SITE_TITLE,
            "short_name": "ダイジェスト",
            "start_url": "./",
            "scope": "./",
            "display": "standalone",
            "background_color": "#14161a",
            "theme_color": "#14161a",
            "icons": [
                {"src": "./icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}
            ],
        },
        ensure_ascii=False,
        indent=2,
    )


def render_icon() -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
        '<rect width="512" height="512" rx="96" fill="#5865f2"/>'
        '<rect x="120" y="146" width="272" height="34" rx="17" fill="#ffffff"/>'
        '<rect x="120" y="239" width="272" height="34" rx="17" fill="#ffffff" opacity=".8"/>'
        '<rect x="120" y="332" width="176" height="34" rx="17" fill="#ffffff" opacity=".6"/>'
        "</svg>"
    )


# ---------------------------------------------------------------------------
# 出力
# ---------------------------------------------------------------------------
def write_text(path: Path, content: str, dry_run: bool) -> None:
    if dry_run:
        LOG.info("[dry-run] %s (%d bytes)", path, len(content.encode("utf-8")))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def collect_days(output_dir: Path) -> list[dict[str, Any]]:
    """出力先を走査して、過去分の一覧を新しい順に組み立てる。"""
    days: list[dict[str, Any]] = []
    if not output_dir.exists():
        return days
    for child in output_dir.iterdir():
        if not child.is_dir() or not DATE_DIR_RE.match(child.name):
            continue
        entry: dict[str, Any] = {"date": child.name, "total": 0, "generated_at": None}
        meta_path = child / "meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                entry["total"] = int(meta.get("total", 0))
                entry["generated_at"] = meta.get("generated_at")
            except (OSError, ValueError, TypeError):
                LOG.warning("%s を読めません。日付だけで一覧に載せます", meta_path)
        days.append(entry)
    days.sort(key=lambda d: str(d["date"]), reverse=True)
    return days


def render_site(digest: dict[str, Any], output_dir: Path, dry_run: bool = False) -> Path:
    """当日ページと index、Service Worker 一式を書き出す。当日ページのパスを返す。"""
    date = digest_date(digest)
    day_dir = output_dir / date

    write_text(day_dir / "index.html", render_day_page(digest), dry_run)

    meta = {
        "date": date,
        "generated_at": digest.get("generated_at"),
        "total": sum(len(s.get("items") or []) for s in digest.get("sections", [])),
        "sections": [
            {"id": s.get("id"), "title": s.get("title"), "count": len(s.get("items") or [])}
            for s in digest.get("sections", [])
        ],
    }
    write_text(day_dir / "meta.json", json.dumps(meta, ensure_ascii=False, indent=2), dry_run)

    # index は当日分を含めて組み直す。dry-run では当日分がまだ無いので足しておく
    days = collect_days(output_dir)
    if dry_run and not any(d["date"] == date for d in days):
        days.insert(0, {"date": date, "total": meta["total"], "generated_at": meta["generated_at"]})
        days.sort(key=lambda d: str(d["date"]), reverse=True)

    write_text(output_dir / "index.html", render_index_page(days), dry_run)
    write_text(output_dir / "sw.js", render_service_worker(["./", f"./{date}/", "./icon.svg"]), dry_run)
    write_text(output_dir / "manifest.webmanifest", render_manifest(), dry_run)
    write_text(output_dir / "icon.svg", render_icon(), dry_run)

    return day_dir / "index.html"


def resolve_output_dir(explicit: str | None) -> Path | None:
    if explicit:
        return Path(explicit).expanduser()
    from_env = os.environ.get("DIGEST_OUTPUT_DIR", "").strip()
    return Path(from_env).expanduser() if from_env else None


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="digest.json から静的 HTML を生成します")
    parser.add_argument("digest", nargs="?", default=str(DEFAULT_DIGEST_PATH), help="digest.json のパス")
    parser.add_argument("--output", help="出力先（既定: .env か環境変数の DIGEST_OUTPUT_DIR）")
    parser.add_argument("--env", default=str(DEFAULT_ENV_PATH), help=".env のパス")
    parser.add_argument("--dry-run", action="store_true", help="書き込まず、書く予定の一覧だけ出す")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug ログまで出す")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    # --output があれば .env は要らないが、無い場合はここから DIGEST_OUTPUT_DIR を得る
    if not args.output:
        load_dotenv(args.env)

    output_dir = resolve_output_dir(args.output)
    if output_dir is None:
        LOG.error("出力先が決まりません。次のどちらかをしてください:")
        LOG.error("  1) --output で直接指定する   例: --output ~/digest-site")
        LOG.error("  2) %s に DIGEST_OUTPUT_DIR=... を書く", args.env)
        return 2

    try:
        with open(args.digest, encoding="utf-8") as f:
            digest = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        LOG.error("digest を読み込めません: %s", exc)
        return 1

    if not isinstance(digest, dict) or "sections" not in digest:
        LOG.error("%s が digest.json の形をしていません", args.digest)
        return 1

    page = render_site(digest, output_dir, dry_run=args.dry_run)
    total = sum(len(s.get("items") or []) for s in digest.get("sections", []))
    LOG.info("%s を生成しました (%d 件)%s", page, total, "（dry-run のため書いていません）" if args.dry_run else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
