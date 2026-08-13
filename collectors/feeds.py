#!/usr/bin/env python3
"""
config/feeds.yaml に列挙した RSS を取得し、digest.json の links 型セクションを組み立てる。

使い方:
    python3 -m collectors.feeds                              # 実取得してセクション JSON を stdout へ
    python3 -m collectors.feeds --dry-run                    # 通信ゼロ。YAML の検証と骨格の確認だけ
    python3 -m collectors.feeds --fixtures tests/fixtures    # 保存済み XML で通しの動作確認
    python3 -m collectors.feeds --check                      # 購読先の疎通診断（表で出る）
    python3 -m collectors.feeds --check --save-fixtures tests/fixtures

壊れたときの振る舞い:
    - フィード 1 つの 404 / タイムアウト / パース失敗は握りつぶし、warning を残して次へ進む。
      他のフィードの結果は必ず返る。
    - feeds.yaml 自体が壊れている場合だけは ConfigError で落とす（配備ミスであり、
      黙って空を返すと「今日はニュースが無い日」と区別が付かないため）。
      collect() はこれも捕まえて空リストを返すので、非ゼロ終了の判断は run_daily.py 側で行う。
    - フィード内の 1 エントリが壊れていても、そのエントリだけ捨てて残りは通す。
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import feedparser
import requests
import yaml

LOG = logging.getLogger("collectors.feeds")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "feeds.yaml"

# digest.json は JST で日時を持つ（data/sample_digest.json 参照）。
# 日本標準時は 1951 年以降 DST を持たないので固定オフセットで等価。
JST = timezone(timedelta(hours=9), "JST")

# defaults ブロックが無い場合に使う値
FALLBACK_DEFAULTS: dict[str, Any] = {
    "window_hours": 24,
    "timeout": 15,
    "per_feed_limit": 10,
    "host_delay": 1.0,
    "assume_timezone": "Asia/Tokyo",
    "user_agent": "daily-digest/0.1",
}

# 除去しても遷移先が変わらないと判断できる計測用パラメータだけを挙げる。
# ここに挙げないパラメータは触らない（リンクが開けないのは配信の失敗そのもの）。
TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "msclkid",
        "yclid",
    }
)

# 日時文字列がタイムゾーンを持つかの判定。末尾に固定して見る
TZ_SUFFIX_RE = re.compile(
    r"(?:Z|[+-]\d{2}:?\d{2}|\b(?:UT|UTC|GMT|JST|CET|CEST|EST|EDT|CST|CDT|MST|MDT|PST|PDT)\b)\s*$",
    re.IGNORECASE,
)
RAW_DATE_KEYS = ("published", "updated", "created", "issued", "date")
PARSED_DATE_KEYS = ("published_parsed", "updated_parsed", "created_parsed")

TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")
# タイトル末尾の " - 媒体名"。ハイフン・ダッシュ類を対象にする
TITLE_SUFFIX_RE = re.compile(r"\s+[-–—|]\s+[^-–—|]+$")
SLUG_RE = re.compile(r"[^\w.\-]+", re.UNICODE)


class ConfigError(RuntimeError):
    """feeds.yaml が読めない・構造が想定と違う。"""


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FeedConfig:
    name: str
    url: str
    section_id: str
    limit: int
    timeout: int
    assume_timezone: str
    strip_title_suffix: bool

    @property
    def fixture_name(self) -> str:
        """--fixtures / --save-fixtures で使うファイル名。設定から一意に決まる。"""
        return f"{slugify(self.section_id)}__{slugify(self.name)}.xml"


@dataclass
class SectionConfig:
    id: str
    title: str
    emoji: str
    feeds: list[FeedConfig] = field(default_factory=list)


@dataclass
class Config:
    window_hours: float
    host_delay: float
    user_agent: str
    sections: list[SectionConfig] = field(default_factory=list)

    def all_feeds(self) -> Iterator[FeedConfig]:
        for section in self.sections:
            yield from section.feeds


def slugify(text: str) -> str:
    """ファイル名に使える形へ。日本語はそのまま残す（読める方が運用しやすい）。"""
    return SLUG_RE.sub("_", str(text)).strip("_") or "unnamed"


def _as_number(value: Any, fallback: float, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        LOG.warning("%s の値 %r を数値として解釈できません。%r を使います", label, value, fallback)
        return fallback
    if number <= 0:
        LOG.warning("%s は正の数である必要があります (%r)。%r を使います", label, value, fallback)
        return fallback
    return number


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> Config:
    """feeds.yaml を読み、検証済みの Config を返す。

    ファイル単位の問題（読めない・sections が無い）は ConfigError。
    エントリ単位の問題（url が無いフィードなど）は、そのエントリだけ捨てて警告する。
    """
    path = Path(path)
    try:
        with path.open(encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except OSError as exc:
        raise ConfigError(f"{path} を読み込めません: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} の YAML が壊れています: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{path} のトップレベルはマッピングである必要があります")

    defaults = {**FALLBACK_DEFAULTS, **(raw.get("defaults") or {})}
    raw_sections = raw.get("sections")
    if not isinstance(raw_sections, list) or not raw_sections:
        raise ConfigError(f"{path} に sections が定義されていません")

    default_limit = int(_as_number(defaults["per_feed_limit"], 10, "defaults.per_feed_limit"))
    default_timeout = int(_as_number(defaults["timeout"], 15, "defaults.timeout"))
    default_tz = str(defaults["assume_timezone"])

    sections: list[SectionConfig] = []
    seen_ids: set[str] = set()

    for index, raw_section in enumerate(raw_sections):
        if not isinstance(raw_section, dict):
            LOG.warning("sections[%d] がマッピングではありません。読み飛ばします", index)
            continue

        section_id = str(raw_section.get("id") or "").strip()
        if not section_id:
            LOG.warning("sections[%d] に id がありません。読み飛ばします", index)
            continue
        if section_id in seen_ids:
            LOG.warning("セクション id %r が重複しています。後の方を読み飛ばします", section_id)
            continue
        seen_ids.add(section_id)

        section = SectionConfig(
            id=section_id,
            title=str(raw_section.get("title") or section_id),
            emoji=str(raw_section.get("emoji") or ""),
        )

        for feed_index, raw_feed in enumerate(raw_section.get("feeds") or []):
            if not isinstance(raw_feed, dict):
                LOG.warning("%s.feeds[%d] がマッピングではありません。読み飛ばします", section_id, feed_index)
                continue

            url = str(raw_feed.get("url") or "").strip()
            name = str(raw_feed.get("name") or "").strip()
            if not url:
                LOG.warning("%s.feeds[%d] (%s) に url がありません。読み飛ばします", section_id, feed_index, name or "?")
                continue
            if not name:
                name = urlsplit(url).netloc or url

            if raw_feed.get("enabled", True) is False:
                LOG.info("%s / %s は enabled: false のため対象外です", section_id, name)
                continue

            section.feeds.append(
                FeedConfig(
                    name=name,
                    url=url,
                    section_id=section_id,
                    limit=int(_as_number(raw_feed.get("limit", default_limit), default_limit, f"{name}.limit")),
                    timeout=int(_as_number(raw_feed.get("timeout", default_timeout), default_timeout, f"{name}.timeout")),
                    assume_timezone=str(raw_feed.get("assume_timezone", default_tz)),
                    strip_title_suffix=bool(raw_feed.get("strip_title_suffix", False)),
                )
            )

        if not section.feeds:
            LOG.warning("セクション %s に有効なフィードがありません", section_id)
        sections.append(section)

    if not sections:
        raise ConfigError(f"{path} に有効なセクションが 1 つもありません")

    return Config(
        window_hours=_as_number(defaults["window_hours"], 24, "defaults.window_hours"),
        host_delay=_as_number(defaults["host_delay"], 1.0, "defaults.host_delay"),
        user_agent=str(defaults["user_agent"]),
        sections=sections,
    )


# ---------------------------------------------------------------------------
# 日時
# ---------------------------------------------------------------------------
_TZ_CACHE: dict[str, tzinfo] = {}


def resolve_timezone(name: str) -> tzinfo:
    """タイムゾーン名を tzinfo にする。未知の名前でも落とさない。"""
    if name in _TZ_CACHE:
        return _TZ_CACHE[name]
    try:
        resolved: tzinfo = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        upper = name.strip().upper()
        if upper in {"JST", "ASIA/TOKYO"}:
            resolved = JST
        elif upper in {"UTC", "GMT", "Z"}:
            resolved = timezone.utc
        else:
            LOG.warning("タイムゾーン %r を解決できません。UTC として扱います", name)
            resolved = timezone.utc
    _TZ_CACHE[name] = resolved
    return resolved


def _parse_raw_date(raw: str) -> datetime | None:
    """feedparser が struct_time を作れなかった場合の保険。"""
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None


def entry_datetime(entry: Any, assume_tz: tzinfo) -> tuple[datetime | None, str]:
    """エントリの日時を aware datetime にする。

    返り値は (日時, 由来) で、由来は 'tz' / 'naive' / 'none'。--check の表で使う。

    feedparser の *_parsed は「タイムゾーンがあれば UTC へ正規化済み、無ければ UTC と
    みなして格納」という挙動なので、struct_time だけ見ても両者を区別できない。
    元の文字列に TZ 表記があるかで判定し、無い場合だけ assume_tz を当て直す。
    """
    raw = None
    for key in RAW_DATE_KEYS:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            raw = value.strip()
            break

    parsed = None
    for key in PARSED_DATE_KEYS:
        value = entry.get(key)
        if value:
            parsed = value
            break

    if parsed is not None:
        try:
            wall_clock = datetime(*parsed[:6])
        except (TypeError, ValueError):
            wall_clock = None
        if wall_clock is not None:
            if raw and TZ_SUFFIX_RE.search(raw):
                return wall_clock.replace(tzinfo=timezone.utc), "tz"
            return wall_clock.replace(tzinfo=assume_tz), "naive"

    if raw:
        parsed_raw = _parse_raw_date(raw)
        if parsed_raw is not None:
            if parsed_raw.tzinfo is not None:
                return parsed_raw, "tz"
            return parsed_raw.replace(tzinfo=assume_tz), "naive"

    return None, "none"


# ---------------------------------------------------------------------------
# 文字列 / URL
# ---------------------------------------------------------------------------
def clean_title(text: str) -> str:
    """HTML エンティティを戻し、混じったタグと余分な空白を落とす。"""
    without_tags = TAG_RE.sub(" ", str(text))
    # 二重エスケープ (&amp;quot;) を返すフィードがあるので 2 回通す
    unescaped = html.unescape(html.unescape(without_tags))
    return WHITESPACE_RE.sub(" ", unescaped).strip()


def absolutize(url: str, base: str) -> str:
    """相対 URL を絶対化し、計測用パラメータだけ落とす。"""
    absolute = urljoin(base, str(url).strip())
    split = urlsplit(absolute)
    if not split.scheme or not split.netloc:
        return ""
    if split.query:
        kept = [
            (k, v)
            for k, v in parse_qsl(split.query, keep_blank_values=True)
            if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_")
        ]
        split = split._replace(query=urlencode(kept))
    return urlunsplit(split)


def url_key(url: str) -> str:
    """重複判定用のキー。表示に使う URL とは別物。"""
    split = urlsplit(url)
    host = split.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    # 既定ポートは落とす
    if (split.scheme == "https" and host.endswith(":443")) or (split.scheme == "http" and host.endswith(":80")):
        host = host.rsplit(":", 1)[0]
    path = split.path.rstrip("/") or "/"
    query = urlencode(sorted(parse_qsl(split.query, keep_blank_values=True)))
    # scheme と fragment は無視する（http/https 違いの同一記事を拾うため）
    return urlunsplit(("", host, path, query, ""))


def title_key(title: str, strip_suffix: bool) -> str:
    """重複判定用のタイトルキー。全角半角と大小文字の揺れを潰す。"""
    normalized = unicodedata.normalize("NFKC", title).casefold()
    normalized = WHITESPACE_RE.sub(" ", normalized).strip()
    if strip_suffix:
        normalized = TITLE_SUFFIX_RE.sub("", normalized).strip() or normalized
    return normalized


# ---------------------------------------------------------------------------
# 取得
# ---------------------------------------------------------------------------
@dataclass
class FeedResult:
    """1 フィードの取得結果。失敗しても例外ではなくこれが返る。"""

    feed: FeedConfig
    ok: bool
    status: str
    items: list[dict[str, Any]] = field(default_factory=list)
    total_entries: int = 0
    # limit で切る前の、24 時間の窓に入っていた件数。
    # items は limit 適用後なので、両方持っていないと limit の妥当性が判断できない
    in_window: int = 0
    date_origins: dict[str, int] = field(default_factory=lambda: {"tz": 0, "naive": 0, "none": 0})
    raw: bytes | None = None
    note: str = ""


class NetworkSource:
    """実ネットワークからの取得。同一ホストへの連続アクセスに間隔を空ける。"""

    def __init__(self, user_agent: str, host_delay: float) -> None:
        self.host_delay = host_delay
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
            }
        )
        self._last_seen: dict[str, float] = {}

    def _wait_for_host(self, host: str) -> None:
        last = self._last_seen.get(host)
        if last is None:
            return
        remaining = self.host_delay - (time.monotonic() - last)
        if remaining > 0:
            LOG.debug("%s への間隔調整のため %.2f 秒待機します", host, remaining)
            time.sleep(remaining)

    def fetch(self, feed: FeedConfig) -> bytes:
        host = urlsplit(feed.url).netloc.lower()
        self._wait_for_host(host)
        try:
            response = self.session.get(feed.url, timeout=feed.timeout)
        finally:
            # 応答が返った（あるいは失敗した）時刻を基準にすることで、
            # 「前のレスポンスから次のリクエストまで host_delay 秒」を保証する
            self._last_seen[host] = time.monotonic()
        response.raise_for_status()
        return response.content


class FixtureSource:
    """保存済み XML からの取得。ネットワークを一切使わない。"""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def fetch(self, feed: FeedConfig) -> bytes:
        path = self.directory / feed.fixture_name
        if not path.exists():
            raise FileNotFoundError(f"fixture がありません: {path}")
        return path.read_bytes()


def collect_feed(
    feed: FeedConfig,
    source: NetworkSource | FixtureSource,
    window_start: datetime,
    fetched_at: datetime,
) -> FeedResult:
    """1 フィードを取得して items を組み立てる。例外はここで止める。"""
    try:
        raw = source.fetch(feed)
    except requests.exceptions.Timeout:
        LOG.warning("[%s] %s: タイムアウト (%d 秒)", feed.section_id, feed.name, feed.timeout)
        return FeedResult(feed, False, "TIMEOUT", note=f"{feed.timeout}s")
    except requests.exceptions.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else "?"
        LOG.warning("[%s] %s: HTTP %s", feed.section_id, feed.name, code)
        return FeedResult(feed, False, f"HTTP {code}", note=str(code))
    except FileNotFoundError as exc:
        LOG.warning("[%s] %s: %s", feed.section_id, feed.name, exc)
        return FeedResult(feed, False, "NO FIXTURE", note=Path(feed.fixture_name).name)
    except Exception as exc:  # 通信まわりは何が飛んでくるか読み切れない
        LOG.warning("[%s] %s: 取得に失敗しました (%s)", feed.section_id, feed.name, exc)
        return FeedResult(feed, False, "ERROR", note=type(exc).__name__)

    try:
        parsed = feedparser.parse(raw)
    except Exception as exc:
        LOG.warning("[%s] %s: パースに失敗しました (%s)", feed.section_id, feed.name, exc)
        return FeedResult(feed, False, "PARSE NG", raw=raw, note=type(exc).__name__)

    entries = parsed.get("entries") or []
    if not entries:
        # bozo でもエントリが取れていれば通す。取れていない時だけ失敗扱い
        reason = type(parsed.get("bozo_exception")).__name__ if parsed.get("bozo") else "エントリなし"
        LOG.warning("[%s] %s: 記事を 1 件も取り出せませんでした (%s)", feed.section_id, feed.name, reason)
        return FeedResult(feed, False, "EMPTY", raw=raw, note=reason)

    assume_tz = resolve_timezone(feed.assume_timezone)
    base_url = str((parsed.get("feed") or {}).get("link") or feed.url)

    result = FeedResult(feed, True, "OK", raw=raw, total_entries=len(entries))

    for entry in entries:
        try:
            title = clean_title(entry.get("title") or "")
            url = absolutize(entry.get("link") or "", base_url)
            if not title or not url:
                # publishers/discord.py 側でも捨てられるが、digest.json に残す意味がない
                continue

            published, origin = entry_datetime(entry, assume_tz)
            result.date_origins[origin] += 1

            # 日時が取れないエントリは取得時刻で代替して窓を通す（HANDOFF の方針）。
            # ただし published_at には書かない。取得時刻を書くと配信側の HH:MM 表示が
            # 全件 06:00 になり、実際の配信時刻と見分けが付かなくなる。
            effective = published or fetched_at
            if effective < window_start:
                continue

            result.items.append(
                {
                    "title": title,
                    "url": url,
                    "source": feed.name,
                    "summary": None,
                    "published_at": published.astimezone(JST).isoformat() if published else None,
                    # 以下は組み立て用。出力前に落とす
                    "_sort_key": published,
                    "_url_key": url_key(url),
                    "_title_key": title_key(title, feed.strip_title_suffix),
                }
            )
        except Exception as exc:
            LOG.warning("[%s] %s: エントリを 1 件読み飛ばしました (%s)", feed.section_id, feed.name, exc)
            continue

    # limit はフィード側の並び（＝多くのフィードで新しい順）の先頭から数える
    result.in_window = len(result.items)
    if len(result.items) > feed.limit:
        result.items = result.items[: feed.limit]

    LOG.info(
        "[%s] %s: 取得 %d 件 → 24h内 %d 件 → 採用 %d 件%s",
        feed.section_id,
        feed.name,
        result.total_entries,
        result.in_window,
        len(result.items),
        f" (limit={feed.limit} で {result.in_window - len(result.items)} 件切り捨て)"
        if result.in_window > len(result.items)
        else "",
    )
    return result


# ---------------------------------------------------------------------------
# 組み立て
# ---------------------------------------------------------------------------
def build_sections(
    config: Config,
    results_by_feed: dict[str, FeedResult],
) -> list[dict[str, Any]]:
    """フィードごとの結果を digest.json のセクション配列にする。

    重複排除は feeds.yaml の宣言順（セクション順 → フィード順 → エントリ順）で走査し、
    最初に現れたものを残す。並びがそのまま優先度になるので、一次情報を上に置けば
    一次情報側のリンクが残る。表示順への並べ替えは、重複を落とし切ってから行う。
    """
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    sections: list[dict[str, Any]] = []
    dropped = 0

    for section_config in config.sections:
        kept: list[dict[str, Any]] = []
        for feed_config in section_config.feeds:
            result = results_by_feed.get(feed_config.fixture_name)
            if result is None or not result.ok:
                continue
            for item in result.items:
                if item["_url_key"] in seen_urls or item["_title_key"] in seen_titles:
                    dropped += 1
                    continue
                seen_urls.add(item["_url_key"])
                seen_titles.add(item["_title_key"])
                kept.append(item)

        # 日時があるものを新しい順に。日時が無いものはその後ろへ、フィードの並び順のまま
        dated = [i for i in kept if i["_sort_key"] is not None]
        undated = [i for i in kept if i["_sort_key"] is None]
        dated.sort(key=lambda i: i["_sort_key"], reverse=True)

        items = [{k: v for k, v in item.items() if not k.startswith("_")} for item in dated + undated]

        sections.append(
            {
                "id": section_config.id,
                "title": section_config.title,
                "emoji": section_config.emoji,
                "type": "links",
                "items": items,
            }
        )

    if dropped:
        LOG.info("重複として %d 件を除外しました", dropped)
    return sections


def collect(
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    generated_at: datetime | None = None,
    fixtures_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """run_daily.py 向けの入口。links 型セクションの配列を返す。

    どのフィードが落ちても、取れた分だけが入った配列が返る。
    feeds.yaml 自体が壊れている場合は空配列を返す（呼び出し側が非ゼロ終了を判断する）。
    """
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        LOG.error("設定を読み込めません: %s", exc)
        return []
    return collect_with_config(config, generated_at=generated_at, fixtures_dir=fixtures_dir)


def collect_with_config(
    config: Config,
    generated_at: datetime | None = None,
    fixtures_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """読み込み済みの Config で収集する。設定を二重に読みたくない場合に使う。"""
    generated_at = generated_at or datetime.now(JST)
    window_start = generated_at - timedelta(hours=config.window_hours)

    source: NetworkSource | FixtureSource
    if fixtures_dir is not None:
        source = FixtureSource(fixtures_dir)
        # fixture は固定の内容なので、取得時刻も固定して結果を再現可能にする
        fetched_at = generated_at
    else:
        source = NetworkSource(config.user_agent, config.host_delay)
        fetched_at = datetime.now(JST)

    results: dict[str, FeedResult] = {}
    for feed in config.all_feeds():
        results[feed.fixture_name] = collect_feed(feed, source, window_start, fetched_at)

    ok_count = sum(1 for r in results.values() if r.ok)
    if results and ok_count == 0:
        LOG.error("すべてのフィードの取得に失敗しました (%d 件)", len(results))
    elif ok_count < len(results):
        LOG.warning("%d 件中 %d 件のフィードが失敗しました", len(results), len(results) - ok_count)

    return build_sections(config, results)


def skeleton_sections(config: Config) -> list[dict[str, Any]]:
    """通信せずに、出力されるセクションの形だけを組み立てる。"""
    return [
        {"id": s.id, "title": s.title, "emoji": s.emoji, "type": "links", "items": []}
        for s in config.sections
    ]


# ---------------------------------------------------------------------------
# --check の表
# ---------------------------------------------------------------------------
def display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def print_check_table(results: list[FeedResult], window_hours: float) -> None:
    headers = [
        "セクション",
        "フィード",
        "状態",
        "取得",
        f"{int(window_hours)}h内",
        "採用",
        "limit",
        "TZ有",
        "TZ無",
        "日時欠",
        "備考",
    ]
    rows: list[list[str]] = []
    for r in results:
        rows.append(
            [
                r.feed.section_id,
                r.feed.name,
                r.status,
                str(r.total_entries) if r.ok else "-",
                str(r.in_window) if r.ok else "-",
                str(len(r.items)) if r.ok else "-",
                str(r.feed.limit),
                str(r.date_origins["tz"]) if r.ok else "-",
                str(r.date_origins["naive"]) if r.ok else "-",
                str(r.date_origins["none"]) if r.ok else "-",
                r.note,
            ]
        )

    widths = [max(display_width(h), *(display_width(row[i]) for row in rows)) for i, h in enumerate(headers)] if rows else [display_width(h) for h in headers]

    print("  ".join(pad(h, w) for h, w in zip(headers, widths)), file=sys.stderr)
    print("  ".join("-" * w for w in widths), file=sys.stderr)
    for row in rows:
        print("  ".join(pad(c, w) for c, w in zip(row, widths)), file=sys.stderr)

    failed = [r for r in results if not r.ok]
    naive = [r for r in results if r.ok and r.date_origins["naive"]]
    undated = [r for r in results if r.ok and r.date_origins["none"]]
    truncated = [r for r in results if r.ok and r.in_window > len(r.items)]

    print("", file=sys.stderr)
    if truncated:
        print("[注意] limit で切り捨てが発生しています。増やすかどうかは好みで判断してください:", file=sys.stderr)
        for r in truncated:
            print(
                f"        {r.feed.name}: 24h内 {r.in_window} 件 → limit {r.feed.limit} 件",
                file=sys.stderr,
            )
    if naive:
        print(
            "[注意] タイムゾーンを持たない日時を返すフィードがあります: "
            + ", ".join(f"{r.feed.name} (assume_timezone: {r.feed.assume_timezone})" for r in naive),
            file=sys.stderr,
        )
    if undated:
        print(
            "[注意] 日時欄が無いエントリを含むフィードがあります: "
            + ", ".join(r.feed.name for r in undated)
            + " — published_at は null になり、セクションの末尾に並びます",
            file=sys.stderr,
        )
    if failed:
        print(
            f"[NG] {len(failed)} 件のフィードが失敗しました: "
            + ", ".join(f"{r.feed.name} ({r.status})" for r in failed),
            file=sys.stderr,
        )
    else:
        print(f"[OK] {len(results)} 件すべて取得できました", file=sys.stderr)


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RSS を取得して digest のセクション配列を作ります")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="feeds.yaml のパス")
    parser.add_argument("--dry-run", action="store_true", help="通信せず、YAML の検証とセクション骨格の確認だけ行う")
    parser.add_argument("--fixtures", metavar="DIR", help="保存済み XML から読み込む（通信しない）")
    parser.add_argument("--check", action="store_true", help="購読先の疎通を診断して表で出す")
    parser.add_argument("--save-fixtures", metavar="DIR", help="--check で取得した内容を fixture として保存する")
    parser.add_argument("--generated-at", metavar="ISO8601", help="24 時間窓の基準時刻（既定: 現在時刻）")
    parser.add_argument(
        "--digest",
        action="store_true",
        help="セクション配列ではなく digest.json 相当の完全な形で出力する"
        "（publishers/discord.py --dry-run に渡して確認するための補助。run_daily.py はこれを使わない）",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug ログまで出す")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    if args.generated_at:
        try:
            generated_at = datetime.fromisoformat(args.generated_at)
        except ValueError:
            print(f"[error] --generated-at を解釈できません: {args.generated_at}", file=sys.stderr)
            return 2
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=JST)
    else:
        generated_at = datetime.now(JST)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2

    # --- 通信ゼロ。YAML が読めるか、セクションの形が想定どおりかだけ見る
    if args.dry_run:
        feeds = list(config.all_feeds())
        print(json.dumps(skeleton_sections(config), ensure_ascii=False, indent=2))
        print(
            f"\n[dry-run] sections={len(config.sections)} feeds={len(feeds)} "
            f"window={config.window_hours:.0f}h host_delay={config.host_delay:.1f}s（通信していません）",
            file=sys.stderr,
        )
        for section in config.sections:
            names = ", ".join(f.name for f in section.feeds) or "(なし)"
            print(f"  {section.emoji} {section.id}: {names}", file=sys.stderr)
        return 0

    # --- 疎通診断
    if args.check:
        source = NetworkSource(config.user_agent, config.host_delay)
        window_start = generated_at - timedelta(hours=config.window_hours)
        results = [collect_feed(f, source, window_start, datetime.now(JST)) for f in config.all_feeds()]

        if args.save_fixtures:
            directory = Path(args.save_fixtures)
            directory.mkdir(parents=True, exist_ok=True)
            saved = 0
            for result in results:
                if result.raw:
                    (directory / result.feed.fixture_name).write_bytes(result.raw)
                    saved += 1
            print(f"[ok] fixture を {saved} 件 {directory} に保存しました", file=sys.stderr)

        print_check_table(results, config.window_hours)
        return 1 if any(not r.ok for r in results) else 0

    # --- 通常の収集（--fixtures 指定時はローカル XML から）
    sections = collect_with_config(config, generated_at=generated_at, fixtures_dir=args.fixtures)

    if args.digest:
        # 確認用の器。本番では run_daily.py が digest_url なども含めて組み立てる
        output: Any = {
            "generated_at": generated_at.isoformat(),
            "digest_url": None,
            "sections": sections,
        }
    else:
        output = sections
    print(json.dumps(output, ensure_ascii=False, indent=2))

    total = sum(len(s["items"]) for s in sections)
    print(
        f"\n[{'fixtures' if args.fixtures else 'collect'}] sections={len(sections)} items={total} "
        f"generated_at={generated_at.isoformat()}",
        file=sys.stderr,
    )
    return 0 if total else 1


if __name__ == "__main__":
    sys.exit(main())
