#!/usr/bin/env python3
"""
config/market.yaml に列挙した指数・為替を取得し、digest.json の market 型セクションを返す。

使い方:
    python3 -m collectors.market                             # 実取得してセクション JSON を stdout へ
    python3 -m collectors.market --dry-run                   # 通信ゼロ。YAML の検証と骨格の確認だけ
    python3 -m collectors.market --fixtures tests/fixtures   # 保存済み JSON で通しの動作確認
    python3 -m collectors.market --check                     # 銘柄ごとの取得可否を表で出す
    python3 -m collectors.market --check --save-fixtures tests/fixtures

yfinance を使わない理由:
    必要なのは 1 日 1 回・数銘柄の終値と前日比だけで、yfinance が引き込む
    pandas / numpy は Raspberry Pi には重すぎる。Python 3.14 向けの ARM 用
    パッケージが揃っていない問題もある。Yahoo Finance の公開 JSON を
    requests で直接読めば、追加の依存なしで同じ数字が得られる。

壊れたときの振る舞い:
    - 1 銘柄が落ちても（記号違い・休場・通信断）握りつぶして warning を残し、
      他の銘柄は返る。
    - 全銘柄が落ちた場合は items が空のセクションを返す。配信側は空セクションを
      スキップするので、マーケット欄だけが消える。
    - market.yaml 自体が壊れている場合だけ ConfigError で落とす。
      collect() はこれも捕まえて空のセクションを返す。
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import requests
import yaml

LOG = logging.getLogger("collectors.market")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "market.yaml"

# 日足と meta 情報が 1 リクエストで取れる公開エンドポイント。
# 認証は不要だが、User-Agent を付けないと弾かれることがある
CHART_ENDPOINT = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

# 三菱UFJアセットマネジメントが公開している投信情報 API。
# 登録も認証キーも不要で JSON が返る。国内の投資信託は Yahoo Finance では
# 取れないので、運用会社が出しているこれを使う。
# fund_cd はファンドページの URL に入っている番号（例: /fund/253425.html → 253425）
FUND_ENDPOINT = "https://developer.am.mufg.jp/fund_information_latest/fund_cd/{fund_cd}"

FALLBACK_DEFAULTS: dict[str, Any] = {
    "timeout": 15,
    "host_delay": 1.0,
    "range": "5d",
    "decimals": 2,
    "user_agent": "daily-digest/0.1",
}
FALLBACK_SECTION: dict[str, str] = {"id": "market", "title": "マーケット", "emoji": "📈"}

SLUG_RE = re.compile(r"[^\w.\-]+", re.UNICODE)

# 変化率がこの値未満なら「変わらず」とみなし、符号を付けない。
# publishers/discord.py は先頭の +/- で騰落マークを決めるため、
# 符号なしにすると ▪️ が出る
FLAT_THRESHOLD = 0.005


class ConfigError(RuntimeError):
    """market.yaml が読めない・構造が想定と違う。"""


class QuoteError(RuntimeError):
    """1 銘柄の取得に失敗した。他の銘柄には波及させない。"""


@dataclass(frozen=True)
class SymbolConfig:
    """1 銘柄の設定。

    source が取得方法を決める:
      yahoo : Yahoo Finance の公開 JSON。symbol に記号を書く（指数・為替・株式）
      fund  : 三菱UFJの投信情報 API。fund_cd を書く（同社の投資信託）
      csv   : 運用会社が公開している基準価額 CSV。url に場所を書く（その他の投資信託）
    """

    name: str
    source: str
    decimals: int
    timeout: int
    symbol: str = ""
    range: str = "5d"
    url: str = ""
    fund_cd: str = ""
    encoding: str = "auto"
    value_column: str = "基準価額"

    @property
    def slug(self) -> str:
        key = self.symbol if self.source == "yahoo" else (self.fund_cd or self.name)
        return SLUG_RE.sub("_", key).strip("_") or "unnamed"

    @property
    def fixture_name(self) -> str:
        """--fixtures / --save-fixtures で使うファイル名。設定から一意に決まる。"""
        suffix = "csv" if self.source == "csv" else "json"
        return f"market__{self.slug}.{suffix}"

    @property
    def location(self) -> str:
        """ログや --check の表に出す、取得元を表す文字列。"""
        if self.source == "yahoo":
            return self.symbol
        if self.source == "fund":
            return f"fund_cd={self.fund_cd}"
        return self.url or "(url 未設定)"


@dataclass
class MarketConfig:
    section: dict[str, str]
    host_delay: float
    user_agent: str
    symbols: list[SymbolConfig] = field(default_factory=list)


def _as_number(value: Any, fallback: float, label: str, minimum: float = None) -> float:
    """数値として読む。minimum を下回る値は fallback に落とす。

    minimum の既定は「正の数」。ただし decimals は 0 が正当な指定なので、
    そこだけ minimum=0 を渡す。
    """
    threshold = minimum if minimum is not None else 1e-9
    try:
        number = float(value)
    except (TypeError, ValueError):
        LOG.warning("%s の値 %r を数値として解釈できません。%r を使います", label, value, fallback)
        return fallback
    if number < threshold:
        LOG.warning("%s は %g 以上である必要があります (%r)。%r を使います", label, threshold, value, fallback)
        return fallback
    return number


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> MarketConfig:
    """market.yaml を読み、検証済みの MarketConfig を返す。

    ファイル単位の問題は ConfigError。銘柄単位の問題はその 1 件だけ捨てて警告する。
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
    section = {**FALLBACK_SECTION, **(raw.get("section") or {})}

    raw_symbols = raw.get("symbols")
    if not isinstance(raw_symbols, list) or not raw_symbols:
        raise ConfigError(f"{path} に symbols が定義されていません")

    default_decimals = int(_as_number(defaults["decimals"], 2, "defaults.decimals", minimum=0))
    default_timeout = int(_as_number(defaults["timeout"], 15, "defaults.timeout"))
    default_range = str(defaults["range"])

    symbols: list[SymbolConfig] = []
    for index, raw_symbol in enumerate(raw_symbols):
        if not isinstance(raw_symbol, dict):
            LOG.warning("symbols[%d] がマッピングではありません。読み飛ばします", index)
            continue

        name = str(raw_symbol.get("name") or "").strip()
        source = str(raw_symbol.get("source") or "yahoo").strip().lower()
        ticker = str(raw_symbol.get("symbol") or "").strip()
        url = str(raw_symbol.get("url") or "").strip()
        fund_cd = str(raw_symbol.get("fund_cd") or "").strip()

        if source not in ("yahoo", "fund", "csv"):
            LOG.warning("symbols[%d] (%s) の source %r は不明です。読み飛ばします", index, name or "?", source)
            continue
        if source == "yahoo" and not ticker:
            LOG.warning("symbols[%d] (%s) に symbol がありません。読み飛ばします", index, name or "?")
            continue
        if source == "fund" and not fund_cd:
            LOG.warning(
                "%s は source: fund ですが fund_cd が未設定です。読み飛ばします "
                "（ファンドページの URL に入っている番号です）",
                name or f"symbols[{index}]",
            )
            continue
        if source == "csv" and not url:
            LOG.warning(
                "%s は source: csv ですが url が未設定です。読み飛ばします "
                "（運用会社のファンドページにある基準価額 CSV の URL を入れてください）",
                name or f"symbols[{index}]",
            )
            continue
        if not name:
            name = ticker or fund_cd or url

        if raw_symbol.get("enabled", True) is False:
            LOG.info("%s は enabled: false のため対象外です", name)
            continue

        symbols.append(
            SymbolConfig(
                name=name,
                source=source,
                symbol=ticker,
                url=url,
                fund_cd=fund_cd,
                encoding=str(raw_symbol.get("encoding", "auto")),
                value_column=str(raw_symbol.get("value_column", "基準価額")),
                decimals=int(_as_number(raw_symbol.get("decimals", default_decimals), default_decimals, f"{name}.decimals", minimum=0)),
                range=str(raw_symbol.get("range", default_range)),
                timeout=int(_as_number(raw_symbol.get("timeout", default_timeout), default_timeout, f"{name}.timeout")),
            )
        )

    if not symbols:
        raise ConfigError(f"{path} に有効な銘柄が 1 つもありません")

    return MarketConfig(
        section=section,
        host_delay=_as_number(defaults["host_delay"], 1.0, "defaults.host_delay"),
        user_agent=str(defaults["user_agent"]),
        symbols=symbols,
    )


# ---------------------------------------------------------------------------
# 取得
# ---------------------------------------------------------------------------
class NetworkSource:
    """Yahoo Finance の公開 JSON から取得する。同一ホストへの連続アクセスに間隔を空ける。

    collectors/feeds.py にも似た仕組みがあるが、あちらは feedparser 前提で
    引数の形が違う。収集モジュールは 1 ファイルで完結させたいので共有していない。
    """

    def __init__(self, user_agent: str, host_delay: float) -> None:
        self.host_delay = host_delay
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
        self._last_seen: dict[str, float] = {}

    def _wait_for_host(self, host: str) -> None:
        last = self._last_seen.get(host)
        if last is None:
            return
        remaining = self.host_delay - (time.monotonic() - last)
        if remaining > 0:
            LOG.debug("%s への間隔調整のため %.2f 秒待機します", host, remaining)
            time.sleep(remaining)

    def fetch(self, symbol: SymbolConfig) -> bytes:
        if symbol.source == "csv":
            url, params = symbol.url, None
        elif symbol.source == "fund":
            url, params = FUND_ENDPOINT.format(fund_cd=quote(symbol.fund_cd, safe="")), None
        else:
            # ^N225 や USDJPY=X の記号をそのまま URL に置くと壊れる環境があるので符号化する
            url = CHART_ENDPOINT.format(symbol=quote(symbol.symbol, safe=""))
            params = {"range": symbol.range, "interval": "1d"}

        host = urlsplit(url).netloc.lower()
        self._wait_for_host(host)
        try:
            response = self.session.get(url, params=params, timeout=symbol.timeout)
        finally:
            self._last_seen[host] = time.monotonic()
        response.raise_for_status()
        return response.content


class FixtureSource:
    """保存済み JSON から取得する。ネットワークを一切使わない。"""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def fetch(self, symbol: SymbolConfig) -> bytes:
        path = self.directory / symbol.fixture_name
        if not path.exists():
            raise FileNotFoundError(f"fixture がありません: {path}")
        return path.read_bytes()


def extract_closes(result: dict[str, Any]) -> list[float]:
    """日足の終値を、欠損を除いて古い順に取り出す。

    休場日は null が入る。祝日や取得直後の未確定バーで null になるので、
    素朴に最後の 2 件を取ると None 同士の引き算で落ちる。
    """
    indicators = result.get("indicators") or {}
    quotes = indicators.get("quote") or []
    if not quotes:
        return []
    raw_closes = (quotes[0] or {}).get("close") or []
    closes: list[float] = []
    for value in raw_closes:
        if value is None:
            continue
        try:
            closes.append(float(value))
        except (TypeError, ValueError):
            continue
    return closes


def parse_quote(raw: bytes, symbol: SymbolConfig) -> tuple[float, float]:
    """(現在値, 前日終値) を返す。取り出せなければ QuoteError。"""
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise QuoteError("JSON として読めません") from exc

    chart = (payload or {}).get("chart") or {}

    error = chart.get("error")
    if error:
        description = ""
        if isinstance(error, dict):
            description = str(error.get("description") or error.get("code") or "")
        raise QuoteError(description or "エラー応答")

    results = chart.get("result") or []
    if not results:
        raise QuoteError("データがありません（記号が違う可能性）")

    result = results[0] or {}
    closes = extract_closes(result)
    meta = result.get("meta") or {}

    if len(closes) >= 2:
        current, previous = closes[-1], closes[-2]
    elif len(closes) == 1:
        # 期間内に 1 営業日分しか無い。前日終値は meta から補う
        current = closes[0]
        previous = meta.get("chartPreviousClose") or meta.get("previousClose")
        if previous is None:
            raise QuoteError("前日終値が取れません")
        previous = float(previous)
    else:
        # 終値の配列が空。長期休場などで meta だけが返ることがある
        market_price = meta.get("regularMarketPrice")
        previous_close = meta.get("chartPreviousClose") or meta.get("previousClose")
        if market_price is None or previous_close is None:
            raise QuoteError("終値が 1 件も取れません")
        current, previous = float(market_price), float(previous_close)

    if previous == 0:
        raise QuoteError("前日終値が 0 です")
    return current, previous


# ---------------------------------------------------------------------------
# 投資信託の基準価額 CSV
# ---------------------------------------------------------------------------
# 国内の運用会社が出す CSV は Shift_JIS のことが多い。BOM 付き UTF-8 も混ざる。
# 上から順に試して、日本語のヘッダが化けずに読めたものを採用する
CSV_ENCODINGS = ("utf-8-sig", "cp932", "utf-8", "euc_jp")

# CSV 内で日付を表す列。ヘッダにこの語が含まれる列を探す
DATE_COLUMN_HINTS = ("年月日", "日付", "基準日", "date")


# 投信情報 API のレスポンスから基準価額を取り出す。
#
# 手元から developer.am.mufg.jp に出られなかったため、実際の JSON を見ずに
# 書いている。入れ子の深さや配列かどうかが違っても読めるよう、キー名で
# 探す作りにしてある。読めなければ --check が生の JSON の先頭を出すので、
# そこを見て NAV_KEYS / PREV_KEYS を実物に合わせること。
NAV_KEYS = ("nav", "基準価額", "standard_price", "base_price")
PREV_KEYS = ("cmp_prev_day", "前日比", "change", "diff")


def _to_number(value: Any) -> float | None:
    """API の数値を float にする。"12,345" や "+221" のような文字列も通す。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    cleaned = unicodedata.normalize("NFKC", value).replace(",", "").replace("円", "").strip()
    if cleaned.startswith("+"):
        cleaned = cleaned[1:]
    try:
        return float(cleaned)
    except ValueError:
        return None


def _find_number(payload: Any, keys: tuple[str, ...]) -> float | None:
    """入れ子のどこかにある keys のいずれかを探して数値で返す。

    配列で包まれていても、1 段深くても拾えるようにしておく。
    同じキーが複数あったら最初に見つかったものを使う。
    """
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).strip().lower() in keys:
                number = _to_number(value)
                if number is not None:
                    return number
        for value in payload.values():
            found = _find_number(value, keys)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _find_number(value, keys)
            if found is not None:
                return found
    return None


def parse_fund_api(raw: bytes, symbol: SymbolConfig) -> tuple[float, float]:
    """(基準価額, 前日の基準価額) を返す。

    API は前日比を持っているので、前日の値は引き算で出す。
    CSV のように 2 日分を並べて読む必要がない。
    """
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, TypeError) as exc:
        raise QuoteError("JSON として読めません") from exc

    nav = _find_number(payload, NAV_KEYS)
    if nav is None:
        raise QuoteError(
            f"基準価額が見つかりません（探したキー: {', '.join(NAV_KEYS)}）"
        )

    change = _find_number(payload, PREV_KEYS)
    if change is None:
        # 前日比が無くても現在値だけは出す。前日比は「±0」表示になる
        LOG.warning("%s: 前日比が見つかりません。前日比なしで表示します", symbol.name)
        return nav, nav
    return nav, nav - change


def decode_csv(raw: bytes, encoding: str) -> str:
    """CSV のバイト列を文字列にする。encoding が auto なら順に試す。"""
    if encoding and encoding.lower() != "auto":
        return raw.decode(encoding, errors="replace")

    for candidate in CSV_ENCODINGS:
        try:
            text = raw.decode(candidate)
        except (UnicodeDecodeError, LookupError):
            continue
        # 化けた場合は置換文字が入る。それが無ければ採用する
        if "�" not in text:
            LOG.debug("CSV の文字コードを %s と判定しました", candidate)
            return text
    LOG.warning("CSV の文字コードを判定できません。cp932 として読みます")
    return raw.decode("cp932", errors="replace")


def _to_number(text: str) -> float | None:
    """"27,880" や "27,880 円" のような値を数値にする。"""
    cleaned = re.sub(r"[^\d.\-]", "", str(text))
    if not cleaned or cleaned in ("-", "."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_fund_csv(raw: bytes, symbol: SymbolConfig) -> tuple[float, float]:
    """基準価額 CSV から (最新, 前営業日) を返す。取り出せなければ QuoteError。"""
    text = decode_csv(raw, symbol.encoding)
    rows = list(csv.reader(io.StringIO(text)))
    rows = [r for r in rows if any(str(c).strip() for c in r)]
    if len(rows) < 2:
        raise QuoteError("CSV に行がありません")

    header = [str(c).strip() for c in rows[0]]

    value_index = next(
        (i for i, h in enumerate(header) if symbol.value_column in h),
        None,
    )
    if value_index is None:
        raise QuoteError(f"列 {symbol.value_column!r} が見つかりません（ヘッダ: {'/'.join(header[:6])}）")

    date_index = next(
        (i for i, h in enumerate(header) if any(hint in h.lower() for hint in DATE_COLUMN_HINTS)),
        0,
    )

    # (日付文字列, 値) を集める。値が読めない行は捨てる
    records: list[tuple[str, float]] = []
    for row in rows[1:]:
        if value_index >= len(row):
            continue
        value = _to_number(row[value_index])
        if value is None:
            continue
        date_text = str(row[date_index]).strip() if date_index < len(row) else ""
        records.append((date_text, value))

    if len(records) < 2:
        raise QuoteError(f"基準価額の行が {len(records)} 件しかありません")

    # 新しい順に並ぶ CSV と古い順に並ぶ CSV の両方がある。日付で判定する
    if records[0][0] > records[-1][0]:
        records.reverse()

    current, previous = records[-1][1], records[-2][1]
    if previous == 0:
        raise QuoteError("前営業日の基準価額が 0 です")
    return current, previous


def format_value(value: float, decimals: int) -> str:
    """桁区切り済みの文字列にする。digest.json では数値ではなく文字列で持つ。"""
    return f"{value:,.{decimals}f}"


def format_change(current: float, previous: float) -> str:
    """符号付きのパーセント文字列にする。配信側は先頭の符号で騰落マークを決める。"""
    percent = (current - previous) / previous * 100
    if abs(percent) < FLAT_THRESHOLD:
        return "0.00%"
    return f"{percent:+.2f}%"


@dataclass
class QuoteResult:
    """1 銘柄の取得結果。失敗しても例外ではなくこれが返る。"""

    symbol: SymbolConfig
    ok: bool
    status: str
    item: dict[str, str] | None = None
    raw: bytes | None = None
    note: str = ""


def collect_symbol(symbol: SymbolConfig, source: NetworkSource | FixtureSource) -> QuoteResult:
    """1 銘柄を取得して item を組み立てる。例外はここで止める。"""
    try:
        raw = source.fetch(symbol)
    except requests.exceptions.Timeout:
        LOG.warning("%s (%s): タイムアウト (%d 秒)", symbol.name, symbol.location, symbol.timeout)
        return QuoteResult(symbol, False, "TIMEOUT", note=f"{symbol.timeout}s")
    except requests.exceptions.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else "?"
        LOG.warning("%s (%s): HTTP %s", symbol.name, symbol.location, code)
        return QuoteResult(symbol, False, f"HTTP {code}", note="記号違いの可能性" if code == 404 else "")
    except FileNotFoundError as exc:
        LOG.warning("%s (%s): %s", symbol.name, symbol.location, exc)
        return QuoteResult(symbol, False, "NO FIXTURE", note=symbol.fixture_name)
    except Exception as exc:
        LOG.warning("%s (%s): 取得に失敗しました (%s)", symbol.name, symbol.location, exc)
        return QuoteResult(symbol, False, "ERROR", note=type(exc).__name__)

    try:
        if symbol.source == "csv":
            current, previous = parse_fund_csv(raw, symbol)
        elif symbol.source == "fund":
            current, previous = parse_fund_api(raw, symbol)
        else:
            current, previous = parse_quote(raw, symbol)
    except QuoteError as exc:
        LOG.warning("%s (%s): %s", symbol.name, symbol.location, exc)
        return QuoteResult(symbol, False, "NO DATA", raw=raw, note=str(exc))
    except Exception as exc:
        LOG.warning("%s (%s): 解釈に失敗しました (%s)", symbol.name, symbol.location, exc)
        return QuoteResult(symbol, False, "PARSE NG", raw=raw, note=type(exc).__name__)

    item = {
        "name": symbol.name,
        "value": format_value(current, symbol.decimals),
        "change": format_change(current, previous),
    }
    LOG.info("%s (%s): %s %s", symbol.name, symbol.location, item["value"], item["change"])
    return QuoteResult(symbol, True, "OK", item=item, raw=raw)


# ---------------------------------------------------------------------------
# 組み立て
# ---------------------------------------------------------------------------
def build_section(config: MarketConfig, results: list[QuoteResult]) -> dict[str, Any]:
    """取得結果を digest.json の market 型セクションにする。"""
    items = [r.item for r in results if r.ok and r.item]
    return {
        "id": config.section.get("id", "market"),
        "title": config.section.get("title", "マーケット"),
        "emoji": config.section.get("emoji", ""),
        "type": "market",
        "items": items,
    }


def collect(
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    fixtures_dir: str | Path | None = None,
) -> dict[str, Any]:
    """run_daily.py 向けの入口。market 型セクションを 1 つ返す。

    どの銘柄が落ちても、取れた分だけが入ったセクションが返る。
    market.yaml 自体が壊れている場合は items が空のセクションを返す。
    """
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        LOG.error("設定を読み込めません: %s", exc)
        return {**FALLBACK_SECTION, "type": "market", "items": []}
    return collect_with_config(config, fixtures_dir=fixtures_dir)


def collect_with_config(
    config: MarketConfig,
    fixtures_dir: str | Path | None = None,
) -> dict[str, Any]:
    """読み込み済みの MarketConfig で収集する。設定を二重に読みたくない場合に使う。"""
    source: NetworkSource | FixtureSource
    if fixtures_dir is not None:
        source = FixtureSource(fixtures_dir)
    else:
        source = NetworkSource(config.user_agent, config.host_delay)

    results = [collect_symbol(s, source) for s in config.symbols]

    ok_count = sum(1 for r in results if r.ok)
    if results and ok_count == 0:
        LOG.error("すべての銘柄の取得に失敗しました (%d 件)", len(results))
    elif ok_count < len(results):
        LOG.warning("%d 件中 %d 件の銘柄が失敗しました", len(results), len(results) - ok_count)

    return build_section(config, results)


def skeleton_section(config: MarketConfig) -> dict[str, Any]:
    """通信せずに、出力されるセクションの形だけを組み立てる。"""
    return build_section(config, [])


# ---------------------------------------------------------------------------
# --check の表
# ---------------------------------------------------------------------------
def display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def print_check_table(results: list[QuoteResult]) -> None:
    headers = ["銘柄", "取得元", "状態", "値", "前日比", "備考"]
    rows = [
        [
            r.symbol.name,
            r.symbol.location,
            r.status,
            (r.item or {}).get("value", "-"),
            (r.item or {}).get("change", "-"),
            r.note,
        ]
        for r in results
    ]

    widths = (
        [max(display_width(h), *(display_width(row[i]) for row in rows)) for i, h in enumerate(headers)]
        if rows
        else [display_width(h) for h in headers]
    )

    print("  ".join(pad(h, w) for h, w in zip(headers, widths)), file=sys.stderr)
    print("  ".join("-" * w for w in widths), file=sys.stderr)
    for row in rows:
        print("  ".join(pad(c, w) for c, w in zip(row, widths)), file=sys.stderr)

    failed = [r for r in results if not r.ok]
    print("", file=sys.stderr)
    if failed:
        print(f"[NG] {len(failed)} 件の銘柄が失敗しました:", file=sys.stderr)
        for r in failed:
            print(f"        {r.symbol.name} ({r.symbol.location}): {r.status} {r.note}".rstrip(), file=sys.stderr)
            # 取れてはいるが読めなかった場合は中身を見せる。
            # 想定と違うキー名で返ってきたとき、これが無いと直しようがない
            if r.raw and r.status in ("NO DATA", "PARSE NG"):
                head = r.raw[:300].decode("utf-8", "replace").replace("\n", " ")
                print(f"          受け取った中身: {head}", file=sys.stderr)
        print("     記号が違う場合は config/market.yaml の symbol を直してください。", file=sys.stderr)
    else:
        print(f"[OK] {len(results)} 件すべて取得できました", file=sys.stderr)


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="指数・為替を取得して digest のセクションを作ります")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="market.yaml のパス")
    parser.add_argument("--dry-run", action="store_true", help="通信せず、YAML の検証とセクション骨格の確認だけ行う")
    parser.add_argument("--fixtures", metavar="DIR", help="保存済み JSON から読み込む（通信しない）")
    parser.add_argument("--check", action="store_true", help="銘柄ごとの取得可否を表で出す")
    parser.add_argument("--save-fixtures", metavar="DIR", help="--check で取得した内容を fixture として保存する")
    parser.add_argument(
        "--digest",
        action="store_true",
        help="セクション単体ではなく digest.json 相当の完全な形で出力する"
        "（publishers/discord.py --dry-run に渡して確認するための補助）",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug ログまで出す")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(json.dumps(skeleton_section(config), ensure_ascii=False, indent=2))
        print(
            f"\n[dry-run] symbols={len(config.symbols)} host_delay={config.host_delay:.1f}s（通信していません）",
            file=sys.stderr,
        )
        for symbol in config.symbols:
            print(f"  {symbol.name} ({symbol.location})", file=sys.stderr)
        return 0

    if args.check:
        source = NetworkSource(config.user_agent, config.host_delay)
        results = [collect_symbol(s, source) for s in config.symbols]

        if args.save_fixtures:
            directory = Path(args.save_fixtures)
            directory.mkdir(parents=True, exist_ok=True)
            saved = 0
            for result in results:
                if result.raw:
                    (directory / result.symbol.fixture_name).write_bytes(result.raw)
                    saved += 1
            print(f"[ok] fixture を {saved} 件 {directory} に保存しました", file=sys.stderr)

        print_check_table(results)
        return 1 if any(not r.ok for r in results) else 0

    section = collect_with_config(config, fixtures_dir=args.fixtures)

    if args.digest:
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone(timedelta(hours=9), "JST"))
        output: Any = {"generated_at": now.isoformat(), "digest_url": None, "sections": [section]}
    else:
        output = section
    print(json.dumps(output, ensure_ascii=False, indent=2))

    print(
        f"\n[{'fixtures' if args.fixtures else 'collect'}] items={len(section['items'])}/{len(config.symbols)}",
        file=sys.stderr,
    )
    return 0 if section["items"] else 1


if __name__ == "__main__":
    sys.exit(main())
