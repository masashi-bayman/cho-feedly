#!/usr/bin/env python3
"""
ローカル LLM（Ollama）で digest のセクションを選別し、英語の見出しを和訳する。

使い方:
    python3 -m curator.llm --check                     # Ollama とモデルの疎通確認
    python3 -m curator.llm data/digest.json --dry-run  # LLM を呼ばずに送る内容を見る
    python3 -m curator.llm data/digest.json            # 選別して結果を stdout へ

設計方針:
    **この工程は「セクションを受け取ってセクションを返す」だけの変換である。**
    だから失敗したときは入力をそのまま返せばよく、配信は今までどおり続く。
    Ollama が入っていない、落ちている、変な答えを返す、遅すぎる、のいずれでも
    digest は壊れない。curate() は例外を投げない。

    キーワードによる絞り込み（feeds.yaml の include / exclude）とは役割が違う。
    キーワードは「確実に落とせるもの」を落とす。ここは「意味で判断する」部分を担う。
    キーワードで先に減らしてから渡すので、LLM に見せる件数は少なくて済む。

小さいモデルを前提にした作り:
    - 出力させるのは番号だけ。本文を書かせない（遅いうえに壊れやすい）
    - 一度に見せる件数を絞る（chunk_size）
    - format=json で構文を強制し、それでも壊れたら数字を拾い直す
    - 答えが解釈できなければ「全部残す」に倒す。黙って全部消えるより安全
    - 和訳は日本語でない見出しにだけ行う。1 件ずつ、短く
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import requests  # noqa: E402
import yaml  # noqa: E402

from envfile import load_dotenv  # noqa: E402

LOG = logging.getLogger("curator.llm")

DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "curator.yaml"
DEFAULT_ENV_PATH = REPO_ROOT / ".env"

FALLBACK_DEFAULTS: dict[str, Any] = {
    "base_url": "http://localhost:11434",
    "model": "qwen2.5:1.5b",
    "timeout": 180,
    "total_budget": 900,
    "chunk_size": 12,
    "num_ctx": 4096,
    "temperature": 0,
}

# 日本語（ひらがな・カタカナ・漢字）が含まれるか。和訳の要否判定に使う
JAPANESE_RE = re.compile(r"[぀-ゟ゠-ヿ一-鿿]")
INTEGER_RE = re.compile(r"\d+")


class CuratorError(RuntimeError):
    """1 回の問い合わせが失敗した。呼び出し側で握りつぶす。"""


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
@dataclass
class SectionRule:
    keep: int = 0
    criteria: str = ""
    translate: bool = False

    @property
    def selects(self) -> bool:
        return self.keep > 0


@dataclass
class CuratorConfig:
    base_url: str
    model: str
    timeout: int
    total_budget: float
    chunk_size: int
    num_ctx: int
    temperature: float
    rules: dict[str, SectionRule] = field(default_factory=dict)


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> CuratorConfig:
    """curator.yaml を読む。読めなければ既定値だけの設定を返す（選別は行われない）。"""
    raw: dict[str, Any] = {}
    path = Path(path)
    try:
        with path.open(encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except OSError:
        LOG.info("%s がありません。選別は行いません", path)
    except yaml.YAMLError as exc:
        LOG.warning("%s の YAML が壊れています: %s。選別は行いません", path, exc)

    if not isinstance(raw, dict):
        raw = {}

    values = {**FALLBACK_DEFAULTS, **(raw.get("defaults") or {})}

    # .env / 環境変数が YAML より優先。同居から別機への移設を 1 行で済ませるため
    base_url = os.environ.get("CURATOR_URL", "").strip() or str(values["base_url"])
    model = os.environ.get("CURATOR_MODEL", "").strip() or str(values["model"])

    rules: dict[str, SectionRule] = {}
    for section_id, raw_rule in (raw.get("sections") or {}).items():
        if not isinstance(raw_rule, dict):
            continue
        select = raw_rule.get("select") or {}
        if not isinstance(select, dict):
            select = {}
        rules[str(section_id)] = SectionRule(
            keep=int(select.get("keep") or 0),
            criteria=str(select.get("criteria") or "").strip(),
            translate=bool(raw_rule.get("translate", False)),
        )

    return CuratorConfig(
        base_url=base_url.rstrip("/"),
        model=model,
        timeout=int(values["timeout"]),
        total_budget=float(values["total_budget"]),
        chunk_size=max(1, int(values["chunk_size"])),
        num_ctx=int(values["num_ctx"]),
        temperature=float(values["temperature"]),
        rules=rules,
    )


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------
class OllamaClient:
    """Ollama の生成 API を叩くだけの薄い覆い。"""

    def __init__(self, config: CuratorConfig) -> None:
        self.config = config
        self.session = requests.Session()

    def generate(self, prompt: str, json_mode: bool, max_tokens: int) -> str:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": self.config.temperature,
                "num_ctx": self.config.num_ctx,
                "num_predict": max_tokens,
            },
        }
        if json_mode:
            payload["format"] = "json"

        try:
            response = self.session.post(
                f"{self.config.base_url}/api/generate",
                json=payload,
                timeout=self.config.timeout,
            )
            response.raise_for_status()
            return str(response.json().get("response") or "")
        except requests.exceptions.Timeout as exc:
            raise CuratorError(f"タイムアウト ({self.config.timeout} 秒)") from exc
        except requests.exceptions.RequestException as exc:
            raise CuratorError(f"通信に失敗しました ({type(exc).__name__})") from exc
        except ValueError as exc:
            raise CuratorError("応答が JSON として読めません") from exc

    def available_models(self) -> list[str]:
        response = self.session.get(f"{self.config.base_url}/api/tags", timeout=10)
        response.raise_for_status()
        return [str(m.get("name") or "") for m in response.json().get("models") or []]


# ---------------------------------------------------------------------------
# 選別
# ---------------------------------------------------------------------------
def build_select_prompt(titles: list[str], keep: int, criteria: str) -> str:
    listing = "\n".join(f"{i}. {t}" for i, t in enumerate(titles, start=1))
    return (
        "次はニュース記事の見出しの一覧です。\n"
        "この中から、条件に合うものだけを選んでください。\n\n"
        f"【条件】\n{criteria}\n\n"
        f"【見出し】\n{listing}\n\n"
        f"条件に合うものを最大 {keep} 件選び、その番号だけを JSON で答えてください。\n"
        '形式: {"keep": [1, 3, 5]}\n'
        "条件に合うものが無ければ {\"keep\": []} と答えてください。\n"
        "説明や理由は書かないでください。"
    )


def parse_selection(text: str, count: int, keep: int) -> list[int] | None:
    """モデルの答えから採用する番号（0 始まり）を取り出す。

    解釈できなければ None を返す。呼び出し側はそのとき「全部残す」に倒す。
    """
    numbers: list[int] = []
    # JSON として「番号の一覧」が返ってきたか。空配列でも一覧は一覧なので、
    # 「該当なし」と「読み取れなかった」を区別するために覚えておく
    explicit = False
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            raw = parsed.get("keep")
        elif isinstance(parsed, list):
            raw = parsed
        else:
            raw = None
        if isinstance(raw, list):
            explicit = True
            numbers = [int(n) for n in raw if isinstance(n, (int, float, str)) and str(n).strip().lstrip("-").isdigit()]
    except (ValueError, TypeError):
        numbers = []

    if not explicit and not numbers:
        # format=json でも壊れることがある。数字だけ拾い直す
        numbers = [int(m) for m in INTEGER_RE.findall(text)]

    if explicit and not numbers:
        return []  # {"keep": []} は「条件に合うものが無い」という正当な答え
    if not numbers:
        return None  # 数字が 1 つも見当たらない。答えとして読めていない

    seen: set[int] = set()
    picked: list[int] = []
    for n in numbers:
        index = n - 1  # プロンプトは 1 始まり
        if 0 <= index < count and index not in seen:
            seen.add(index)
            picked.append(index)
        if len(picked) >= keep:
            break

    if not picked:
        # 番号は返ってきたが 1 つも範囲内に無い。壊れた答えとみなして選別を諦める。
        # ここで [] を返すと記事が全部消えるので、それだけは避ける
        return None
    return picked


def select_items(
    client: OllamaClient,
    items: list[dict[str, Any]],
    rule: SectionRule,
    chunk_size: int,
    deadline: float,
) -> list[dict[str, Any]]:
    """条件に合う記事だけを残す。元の並び順は保つ。"""
    if len(items) <= rule.keep:
        LOG.debug("元から %d 件なので選別しません", len(items))
        return items

    kept: list[dict[str, Any]] = []
    chunks = [items[i : i + chunk_size] for i in range(0, len(items), chunk_size)]
    # 分割したぶん 1 回あたりの取り分を減らす。合計が keep を大きく超えないように
    per_chunk = max(1, -(-rule.keep // len(chunks)))

    for number, chunk in enumerate(chunks, start=1):
        if time.monotonic() > deadline:
            LOG.warning("時間切れのため残り %d 件は選別せずそのまま通します", len(items) - len(kept))
            kept.extend(chunk)
            continue

        titles = [str(item.get("title", "")) for item in chunk]
        prompt = build_select_prompt(titles, per_chunk, rule.criteria)
        try:
            answer = client.generate(prompt, json_mode=True, max_tokens=128)
        except CuratorError as exc:
            LOG.warning("選別に失敗しました (%s)。この %d 件はそのまま通します", exc, len(chunk))
            kept.extend(chunk)
            continue

        picked = parse_selection(answer, len(chunk), per_chunk)
        if picked is None:
            LOG.warning("答えを解釈できません (%r)。この %d 件はそのまま通します", answer[:80], len(chunk))
            kept.extend(chunk)
            continue

        LOG.debug("%d/%d 個目: %d 件中 %d 件を採用", number, len(chunks), len(chunk), len(picked))
        kept.extend(chunk[i] for i in sorted(picked))

    return kept


# ---------------------------------------------------------------------------
# 和訳
# ---------------------------------------------------------------------------
def looks_japanese(text: str) -> bool:
    return JAPANESE_RE.search(str(text)) is not None


def build_translate_prompt(title: str) -> str:
    return (
        "次の見出しを自然な日本語に訳してください。\n"
        "訳文だけを 1 行で答えてください。説明や原文は書かないでください。\n\n"
        f"{title}"
    )


def translate_titles(
    client: OllamaClient,
    items: list[dict[str, Any]],
    deadline: float,
) -> list[dict[str, Any]]:
    """日本語でない見出しを訳す。訳せなければ原文のまま残す。"""
    translated = 0
    for item in items:
        title = str(item.get("title", "")).strip()
        if not title or looks_japanese(title):
            continue
        if time.monotonic() > deadline:
            LOG.warning("時間切れのため残りの和訳は行いません")
            break

        try:
            answer = client.generate(build_translate_prompt(title), json_mode=False, max_tokens=128)
        except CuratorError as exc:
            LOG.warning("和訳に失敗しました (%s)。原文のまま残します", exc)
            continue

        candidate = unicodedata.normalize("NFKC", answer).strip().splitlines()
        candidate = candidate[0].strip() if candidate else ""
        # 訳せていない・暴走した・原文をそのまま返した場合は採用しない
        if not candidate or not looks_japanese(candidate) or len(candidate) > len(title) * 3 + 40:
            LOG.debug("和訳を採用しませんでした: %r", candidate[:60])
            continue

        item["title_original"] = title
        item["title"] = candidate
        translated += 1

    if translated:
        LOG.info("見出しを %d 件和訳しました", translated)
    return items


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def curate(
    sections: list[dict[str, Any]],
    config_path: str | Path = DEFAULT_CONFIG_PATH,
) -> list[dict[str, Any]]:
    """セクション配列を選別して返す。**決して例外を投げない。**

    Ollama が無い・落ちている・遅い・変な答えを返す、のいずれでも
    入力をそのまま返す。配信は今までどおり続く。
    """
    try:
        return _curate(sections, load_config(config_path))
    except Exception:
        LOG.exception("選別で予期しない例外が出ました。収集したままの結果で続けます")
        return sections


def _curate(sections: list[dict[str, Any]], config: CuratorConfig) -> list[dict[str, Any]]:
    if not config.rules:
        LOG.info("選別の設定がありません。そのまま通します")
        return sections

    client = OllamaClient(config)
    try:
        models = client.available_models()
    except Exception as exc:
        LOG.info("Ollama に繋がりません (%s: %s)。選別せずそのまま通します",
                 config.base_url, type(exc).__name__)
        return sections

    if models and not any(m == config.model or m.startswith(config.model + ":") for m in models):
        LOG.warning("モデル %s が見つかりません（利用可能: %s）。選別せずそのまま通します",
                    config.model, ", ".join(models[:5]) or "なし")
        return sections

    deadline = time.monotonic() + config.total_budget
    LOG.info("選別を開始します (%s / %s)", config.base_url, config.model)

    for section in sections:
        rule = config.rules.get(str(section.get("id")))
        items = section.get("items") or []
        if rule is None or not items:
            continue

        before = len(items)
        if rule.selects:
            items = select_items(client, items, rule, config.chunk_size, deadline)
        if rule.translate:
            items = translate_titles(client, items, deadline)

        section["items"] = items
        if before != len(items):
            LOG.info("[%s] %d 件 → %d 件", section.get("id"), before, len(items))

    remaining = deadline - time.monotonic()
    LOG.info("選別が終わりました (残り時間 %.0f 秒)", max(0.0, remaining))
    return sections


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ローカル LLM で digest を選別します")
    parser.add_argument("digest", nargs="?", help="digest.json のパス")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="curator.yaml のパス")
    parser.add_argument("--env", default=str(DEFAULT_ENV_PATH), help=".env のパス")
    parser.add_argument("--check", action="store_true", help="Ollama とモデルの疎通を確認する")
    parser.add_argument("--dry-run", action="store_true", help="LLM を呼ばず、送る内容を表示する")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug ログまで出す")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    load_dotenv(args.env)
    config = load_config(args.config)

    if args.check:
        print(f"接続先: {config.base_url}")
        print(f"モデル: {config.model}")
        client = OllamaClient(config)
        try:
            models = client.available_models()
        except Exception as exc:
            print(f"\n[NG] Ollama に繋がりません ({type(exc).__name__})", file=sys.stderr)
            print("     systemctl status ollama で動いているか確認してください", file=sys.stderr)
            return 1
        print(f"利用可能なモデル: {', '.join(models) or 'なし'}")
        if not any(m == config.model or m.startswith(config.model + ":") for m in models):
            print(f"\n[NG] {config.model} がありません。ollama pull {config.model} を実行してください", file=sys.stderr)
            return 1

        started = time.monotonic()
        try:
            answer = client.generate('次の質問に JSON で答えてください: {"ok": true} と返してください', True, 32)
        except CuratorError as exc:
            print(f"\n[NG] 応答がありません: {exc}", file=sys.stderr)
            return 1
        print(f"\n[OK] 応答を確認しました ({time.monotonic() - started:.1f} 秒): {answer.strip()[:60]}")
        print(f"     選別対象のセクション: {', '.join(config.rules) or 'なし'}")
        return 0

    if not args.digest:
        parser.error("digest.json のパスを指定してください（--check 以外の場合）")

    try:
        with open(args.digest, encoding="utf-8") as f:
            digest = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        LOG.error("digest を読み込めません: %s", exc)
        return 1

    sections = digest.get("sections") if isinstance(digest, dict) else digest
    if not isinstance(sections, list):
        LOG.error("%s が digest.json の形をしていません", args.digest)
        return 1

    if args.dry_run:
        for section in sections:
            rule = config.rules.get(str(section.get("id")))
            items = section.get("items") or []
            if rule is None or not items:
                print(f"[{section.get('id')}] {len(items)} 件 — 選別しません", file=sys.stderr)
                continue
            print(f"\n[{section.get('id')}] {len(items)} 件 → 最大 {rule.keep} 件 "
                  f"(和訳: {'あり' if rule.translate else 'なし'})", file=sys.stderr)
            if rule.selects:
                titles = [str(i.get("title", "")) for i in items[: config.chunk_size]]
                print(build_select_prompt(titles, rule.keep, rule.criteria), file=sys.stderr)
        print("\n[dry-run] LLM は呼んでいません", file=sys.stderr)
        return 0

    before = {str(s.get("id")): len(s.get("items") or []) for s in sections}
    curated = curate(sections, args.config)
    print(json.dumps(curated, ensure_ascii=False, indent=2))

    print("", file=sys.stderr)
    for section in curated:
        section_id = str(section.get("id"))
        after = len(section.get("items") or [])
        mark = "→" if before.get(section_id) == after else "⇒"
        print(f"  {section_id:9} {before.get(section_id, 0):3} 件 {mark} {after:3} 件", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
