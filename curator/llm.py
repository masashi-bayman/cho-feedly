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
import math
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
    "num_ctx": 2048,
    "temperature": 0,
    "keep_alive": "5m",
    "judge_style": "keep_true",
}

# 日本語（ひらがな・カタカナ・漢字）が含まれるか。和訳の要否判定に使う
JAPANESE_RE = re.compile(r"[぀-ゟ゠-ヿ一-鿿]")
INTEGER_RE = re.compile(r"\d+")


class CuratorError(RuntimeError):
    """1 回の問い合わせが失敗した。呼び出し側で握りつぶす。"""


# ---------------------------------------------------------------------------
# メモリの見張り
#
# 入りきらないモデルを読み込ませると、Pi は「遅くなる」ではなく「固まる」。
# 実際に qwen2.5:3b (1.9GB) を空き 2.0GB の Pi で動かして SSH ごと落ちた。
# OOM killer が間に合わず、電源を抜くしか戻す手が無くなる。
#
# 毎朝 6 時に無人で動くものが Pi を落とすのは許容できないので、
# 読み込ませる前にこちらで断る。断ったときは選別せずそのまま通すだけで、
# 配信は普段どおり続く。
# ---------------------------------------------------------------------------
MEMORY_MARGIN_BYTES = 400 * 1024 * 1024  # 実行時の作業領域として見込む分
MEMORY_SAFETY_RATIO = 1.15               # 重みの展開などで実測は容量より膨らむ


def available_memory() -> int | None:
    """すぐ使えるメモリ(バイト)。Linux 以外や読めない環境では None。

    MemFree ではなく MemAvailable を見る。キャッシュとして使われている分は
    必要になれば解放されるので、MemFree は実際より小さく出る。
    """
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def gib(value: float) -> str:
    return f"{value / (1024 ** 3):.1f}GB"


def top_memory_users(limit: int = 4) -> list[tuple[str, int]]:
    """メモリを食っている順に (名前, バイト)。読めなければ空。

    断ったときに「では何を止めればいいのか」が分からないと詰むので、
    犯人を名指しできるようにしておく。ps を呼ばず /proc から直接読む。
    """
    found: list[tuple[str, int]] = []
    try:
        pids = [d for d in os.listdir("/proc") if d.isdigit()]
    except OSError:
        return []
    for pid in pids:
        try:
            with open(f"/proc/{pid}/status", encoding="ascii", errors="replace") as f:
                name = ""
                rss = 0
                for line in f:
                    if line.startswith("Name:"):
                        name = line.split(maxsplit=1)[1].strip()
                    elif line.startswith("VmRSS:"):
                        rss = int(line.split()[1]) * 1024
                        break
            if not rss:
                continue
            # node や python は名前だけでは分からないので、コマンド行から手掛かりを拾う
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
            for hint in ("vscode-server", "ollama", "nginx", "php-fpm", "tailscaled", "node"):
                if hint in cmdline:
                    name = f"{name} ({hint})"
                    break
        except (OSError, ValueError, IndexError):
            continue
        found.append((name, rss))
    found.sort(key=lambda x: -x[1])
    return found[:limit]


def print_memory_advice(model_name: str, stream: Any) -> None:
    """空きが足りないときに、何を止めればよいかを出す。"""
    print("\n     メモリを使っているもの:", file=stream)
    for name, rss in top_memory_users():
        print(f"       {gib(rss):>7}  {name}", file=stream)
    print("\n     空けてからもう一度試してください:", file=stream)
    print("       ollama ps            # モデルが載ったままなら ollama stop <名前>", file=stream)
    print("       VS Code の接続を切る  # Remote-SSH は 1GB 前後を使います", file=stream)
    print(f"\n     {model_name} より小さいモデルは実用的なものがありません。", file=stream)
    print("     メモリを空けるのが唯一の道です。", file=stream)


def memory_verdict(model_bytes: int | None) -> tuple[bool, str]:
    """このモデルを読み込ませてよいか。(可否, 説明) を返す。

    大きさか空きが分からないときは通す。分からないことを理由に
    毎朝の選別を止める方が困るし、その場合は今までと同じ挙動になるだけ。
    """
    free = available_memory()
    if model_bytes is None or free is None:
        return True, "メモリを確認できませんでした（そのまま続けます）"
    needed = int(model_bytes * MEMORY_SAFETY_RATIO) + MEMORY_MARGIN_BYTES
    summary = f"モデル {gib(model_bytes)} / 必要 {gib(needed)} / 空き {gib(free)}"
    return needed <= free, summary


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
@dataclass
class SectionRule:
    keep: int = 0
    # 判定で減りすぎたときに戻す最低件数。0 なら戻さない。
    # 小さいモデルは「いいえ」に倒れやすく、放っておくと欄ごと空になる
    min_keep: int = 0
    criteria: str = ""
    translate: bool = False
    # individual: 1 件ずつ「残す/捨てる」を判定する（既定）
    # batch:      まとめて見せて番号を選ばせる
    mode: str = "individual"
    # (見出し, 残すか) の正解例。fewshot 系の聞き方が使う。
    # criteria という抽象的な指示より、こちらの方がはるかによく効く
    examples: list[tuple[str, bool]] = field(default_factory=list)
    # category の聞き方が使う分野名。読みたい分野と、そうでない分野。
    # 二択より選択肢が多い方が、片側に倒れにくい
    keep_categories: list[str] = field(default_factory=list)
    drop_categories: list[str] = field(default_factory=list)

    @property
    def categories(self) -> list[str]:
        """モデルに見せる分野の一覧。読みたい方に偏らないよう交互に並べる。"""
        mixed: list[str] = []
        for i in range(max(len(self.keep_categories), len(self.drop_categories))):
            if i < len(self.keep_categories):
                mixed.append(self.keep_categories[i])
            if i < len(self.drop_categories):
                mixed.append(self.drop_categories[i])
        return mixed

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
    keep_alive: str
    # 1 件ずつ判定するときの聞き方。JUDGE_STYLES のキー。--selftest で選ぶ
    judge_style: str = "keep_true"
    rules: dict[str, SectionRule] = field(default_factory=dict)


def _load_examples(raw: Any) -> list[tuple[str, bool]]:
    """select.examples の keep / drop を (見出し, 残すか) の並びにする。

    keep と drop を交互に並べる。同じ答えが続くと、小さいモデルは
    中身を見ずにその答えを繰り返すようになるため。
    """
    if not isinstance(raw, dict):
        return []
    keep = [str(t) for t in (raw.get("keep") or []) if str(t).strip()]
    drop = [str(t) for t in (raw.get("drop") or []) if str(t).strip()]
    mixed: list[tuple[str, bool]] = []
    for i in range(max(len(keep), len(drop))):
        if i < len(keep):
            mixed.append((keep[i], True))
        if i < len(drop):
            mixed.append((drop[i], False))
    return mixed


def _string_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


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
            min_keep=int(select.get("min_keep") or 0),
            criteria=str(select.get("criteria") or "").strip(),
            translate=bool(raw_rule.get("translate", False)),
            mode=str(select.get("mode") or "individual").strip().lower(),
            examples=_load_examples(select.get("examples")),
            keep_categories=_string_list((select.get("categories") or {}).get("keep")),
            drop_categories=_string_list((select.get("categories") or {}).get("drop")),
        )

    return CuratorConfig(
        base_url=base_url.rstrip("/"),
        model=model,
        timeout=int(values["timeout"]),
        total_budget=float(values["total_budget"]),
        chunk_size=max(1, int(values["chunk_size"])),
        num_ctx=int(values["num_ctx"]),
        temperature=float(values["temperature"]),
        keep_alive=str(values["keep_alive"]),
        judge_style=str(values["judge_style"]).strip(),
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
            "keep_alive": self.config.keep_alive,
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

    def unload(self) -> None:
        """モデルをメモリから降ろす。

        Ollama は既定で 5 分ほどモデルを保持し続ける。1 日 1 回しか使わないので、
        終わったら明示的に解放する。Pi では 1〜2GB の差になる。
        失敗しても実害は無い（放っておいてもいずれ解放される）ので握りつぶす。
        """
        try:
            self.session.post(
                f"{self.config.base_url}/api/generate",
                json={"model": self.config.model, "keep_alive": 0},
                timeout=30,
            )
            LOG.info("モデルをメモリから解放しました")
        except Exception as exc:
            LOG.debug("モデルの解放に失敗しました (%s)", type(exc).__name__)

    def installed_models(self) -> list[dict[str, Any]]:
        response = self.session.get(f"{self.config.base_url}/api/tags", timeout=10)
        response.raise_for_status()
        models = response.json().get("models") or []
        return [m for m in models if isinstance(m, dict)]

    def available_models(self) -> list[str]:
        return [str(m.get("name") or "") for m in self.installed_models()]

    def model_size(self, name: str) -> int | None:
        """モデルの大きさ(バイト)。分からなければ None。"""
        for model in self.installed_models():
            found = str(model.get("name") or "")
            if found == name or found.startswith(name + ":"):
                size = model.get("size")
                if isinstance(size, (int, float)) and size > 0:
                    return int(size)
        return None


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


# 英語の聞き方で使う keep / discard もここに入れる。
# "discard" は "keep" を含まず、"keep" は "no" を含まないので取り違えない
TRUE_WORDS = ("true", "yes", "はい", "残す", "keep", "1")
FALSE_WORDS = ("false", "no", "いいえ", "捨てる", "discard", "drop", "0")


def parse_judgement(text: str) -> bool | None:
    """「残す/捨てる」の判定を取り出す。読めなければ None。"""
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            value = parsed.get("keep")
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return bool(value)
            if isinstance(value, str):
                low = value.strip().lower()
                if low in TRUE_WORDS:
                    return True
                if low in FALSE_WORDS:
                    return False
    except (ValueError, TypeError):
        pass

    low = text.strip().lower()
    has_true = any(w in low for w in TRUE_WORDS)
    has_false = any(w in low for w in FALSE_WORDS)
    if has_true and not has_false:
        return True
    if has_false and not has_true:
        return False
    return None


# ---------------------------------------------------------------------------
# 聞き方の候補
#
# 実測（qwen2.5:1.5b / 見出し 8 件）:
#   keep_true         8 件すべて「捨てる」
#   keep_false_first  8 件すべて「捨てる」（選択肢の順序は原因ではない）
#   score             8 件すべて 5 点以上＝「残す」
#
# 同じモデル・同じ見出しで、聞き方を変えただけで答えが丸ごと裏返っている。
# 読めていないのではなく、聞き方に引きずられて片側へ倒れている。
#
# 小さいモデルに「判断しろ」と抽象的に頼むのが無理なだけなので、
# 正解例を並べてから聞く（fewshot）。どれが効くかは --selftest で測る。
# ---------------------------------------------------------------------------
Example = tuple[str, bool]


def _format_examples(examples: list[Example], yes: str, no: str) -> str:
    return "".join(f"見出し: {t}\n答え: {yes if want else no}\n\n" for t, want in examples)


def _prompt_fewshot(title: str, rule: SectionRule) -> str:
    """正解例を見せてから同じ形式で答えさせる。

    小さいモデルは「条件を読んで判断する」より「並んでいる形を続ける」方が
    はるかに得意。例が無ければただの yes_no と同じになる。
    """
    return (
        "ニュースの見出しを仕分けます。\n"
        f"次のようなものを読みたい: {rule.criteria}\n"
        "それ以外は読みたくない。\n\n"
        f"{_format_examples(rule.examples, 'はい', 'いいえ')}"
        f"見出し: {title}\n答え:"
    )


def _prompt_fewshot_json(title: str, rule: SectionRule) -> str:
    """fewshot と同じ内容を JSON で答えさせる。

    fewshot が効いたのに JSON では効かないなら、format=json が原因だと分かる。
    """
    body = "".join(
        f'見出し: {t}\n答え: {{"keep": {"true" if want else "false"}}}\n\n'
        for t, want in rule.examples
    )
    return (
        "ニュースの見出しを仕分けます。\n"
        f"次のようなものを読みたい: {rule.criteria}\n"
        "それ以外は読みたくない。\n\n"
        f"{body}"
        f"見出し: {title}\n答え:"
    )


# ---------------------------------------------------------------------------
# 英語で指示する聞き方
#
# 小型モデルは英語の指示追従が日本語よりはるかに強い。学習データの量が
# 桁違いに違うため。見出しは日本語のまま、指示だけ英語にすると、
# 同じモデルでも結果が変わることがある。
# 日本語で全滅したら、モデルを替える前にこちらを試す価値がある。
# ---------------------------------------------------------------------------
def _prompt_english(title: str, rule: SectionRule) -> str:
    return (
        "You are filtering Japanese news headlines for a reader.\n"
        f"The reader wants: {rule.criteria}\n"
        "Everything else should be discarded.\n\n"
        f"Headline: {title}\n"
        'Answer with one word, "keep" or "discard".\nAnswer:'
    )


def _prompt_english_fewshot(title: str, rule: SectionRule) -> str:
    body = "".join(
        f"Headline: {t}\nAnswer: {'keep' if want else 'discard'}\n\n"
        for t, want in rule.examples
    )
    return (
        "You are filtering Japanese news headlines for a reader.\n"
        f"The reader wants: {rule.criteria}\n"
        "Everything else should be discarded.\n"
        'Answer each headline with one word, "keep" or "discard".\n\n'
        f"{body}"
        f"Headline: {title}\nAnswer:"
    )


# ---------------------------------------------------------------------------
# 分野を選ばせる聞き方
#
# 二択は片側に倒れやすい。実測では 20 件すべて同じ答えになった。
# 「どの分野か」を選ばせれば、答えの候補が増えて倒れにくくなるうえ、
# 見出しを読まないと選べない。読んでいるかどうかの見極めにもなる。
# ---------------------------------------------------------------------------
def _prompt_category(title: str, rule: SectionRule) -> str:
    labels = rule.categories or ["その他"]
    return (
        "次の見出しはどの分野ですか。下から 1 つだけ選んでください。\n\n"
        f"分野: {' / '.join(labels)}\n\n"
        f"見出し: {title}\n"
        "分野名だけを答えてください。説明は書かないでください。\n分野:"
    )


def _parse_category(text: str, rule: SectionRule) -> bool | None:
    """答えた分野が「読みたい方」に入っていれば残す。

    どちらにも無い言葉を返してきたら不明扱い。不明は捨てないので、
    分野名を思いつきで作られても記事が消えることはない。
    """
    answer = normalize_label(text)
    if not answer:
        return None
    for label in rule.keep_categories:
        if normalize_label(label) and normalize_label(label) in answer:
            return True
    for label in rule.drop_categories:
        if normalize_label(label) and normalize_label(label) in answer:
            return False
    return None


def normalize_label(text: str) -> str:
    return unicodedata.normalize("NFKC", text).strip().lower()


def _prompt_keep_true(title: str, rule: SectionRule) -> str:
    return (
        "次の見出しは、下の「読みたいもの」に当てはまりますか。\n\n"
        f"【読みたいもの】\n{rule.criteria}\n"
        f"【見出し】\n{title}\n\n"
        '当てはまるなら {"keep": true}、当てはまらないなら {"keep": false} '
        "と答えてください。\n迷ったら true にしてください。説明は書かないでください。"
    )


def _prompt_keep_false_first(title: str, rule: SectionRule) -> str:
    """keep_true と選択肢の順序だけを入れ替えたもの。

    これで答えが丸ごと反転するなら、モデルは見出しを読まずに
    「最後に書いてあった方」を返しているということ。位置バイアスの検出用。
    """
    return (
        "次の見出しは、下の「読みたいもの」に当てはまりますか。\n\n"
        f"【読みたいもの】\n{rule.criteria}\n"
        f"【見出し】\n{title}\n\n"
        '当てはまらないなら {"keep": false}、当てはまるなら {"keep": true} '
        "と答えてください。\n迷ったら true にしてください。説明は書かないでください。"
    )


def _prompt_yes_no(title: str, rule: SectionRule) -> str:
    """JSON をやめて 1 語で答えさせる。

    format=json は構文を強制する代わりに、小さいモデルほど中身を考えずに
    「JSON らしい何か」を出して終わりにしてしまう。
    """
    return (
        f"{rule.criteria}\n"
        "――この条件に当てはまるニュースだけを選んでいます。\n\n"
        f"見出し: {title}\n\n"
        "この見出しは条件に当てはまりますか。「はい」か「いいえ」だけ答えてください。"
    )


def _prompt_score(title: str, rule: SectionRule) -> str:
    """二択ではなく点数にする。"""
    return (
        "次の見出しが、下の【関心】にどれくらい近いか 0〜10 で採点してください。\n"
        "10 がぴったり、0 が全く無関係です。\n\n"
        f"【関心】\n{rule.criteria}\n"
        f"【見出し】\n{title}\n\n"
        '{"score": 7} の形の JSON だけを答えてください。説明は書かないでください。'
    )


SCORE_THRESHOLD = 5


def parse_score(text: str, rule: SectionRule) -> bool | None:
    """点数を読んで、しきい値以上なら残す。読めなければ None。"""
    value: Any = None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            value = parsed.get("score")
    except (ValueError, TypeError):
        pass
    if value is None:
        found = INTEGER_RE.search(text)
        value = found.group() if found else None
    if value is None:
        return None
    try:
        return float(value) >= SCORE_THRESHOLD
    except (TypeError, ValueError):
        return None


def _parse_yes_no(text: str, rule: SectionRule) -> bool | None:
    return parse_judgement(text)


@dataclass(frozen=True)
class JudgeStyle:
    label: str
    build: Any          # (title, rule) -> prompt
    parse: Any          # (text, rule) -> bool | None
    json_mode: bool
    max_tokens: int


JUDGE_STYLES: dict[str, JudgeStyle] = {
    # 日本語で例を見せる
    "fewshot": JudgeStyle("例あり・はい/いいえ", _prompt_fewshot, _parse_yes_no, False, 8),
    "fewshot_json": JudgeStyle("例あり・JSON", _prompt_fewshot_json, _parse_yes_no, True, 24),
    # 英語で指示する。小型モデルは英語の指示の方が通りやすい
    "english": JudgeStyle("英語の指示・例なし", _prompt_english, _parse_yes_no, False, 8),
    "english_fewshot": JudgeStyle("英語の指示・例あり", _prompt_english_fewshot, _parse_yes_no, False, 8),
    # 二択をやめる
    "category": JudgeStyle("分野を選ばせる", _prompt_category, _parse_category, False, 12),
    # 比較用。例も無く条件文だけで聞く
    "yes_no": JudgeStyle("例なし・はい/いいえ", _prompt_yes_no, _parse_yes_no, False, 8),
    "keep_true": JudgeStyle("例なし・JSON (true が先)", _prompt_keep_true, _parse_yes_no, True, 24),
    "keep_false_first": JudgeStyle("例なし・JSON (false が先)", _prompt_keep_false_first, _parse_yes_no, True, 24),
    "score": JudgeStyle(f"例なし・0〜10 の点数 ({SCORE_THRESHOLD} 以上を残す)", _prompt_score, parse_score, True, 16),
}

# --selftest で既定で試すもの。全部やると時間がかかるので、
# まだ試していない筋の良いものと、比べる意味のある対照だけに絞ってある。
# 日本語の二択（keep_true / score）は 20 件すべて同じ答えを返すことが
# 実測で分かっているので既定からは外した（--styles で指定はできる）
SELFTEST_STYLES = ["english_fewshot", "english", "category", "fewshot"]

def judge_style(name: str) -> JudgeStyle:
    style = JUDGE_STYLES.get(name)
    if style is None:
        LOG.warning("judge_style '%s' は知らない聞き方です。keep_true を使います", name)
        return JUDGE_STYLES["keep_true"]
    return style


def select_one_by_one(
    client: OllamaClient,
    items: list[dict[str, Any]],
    rule: SectionRule,
    deadline: float,
    explain: list[tuple[str, str]] | None = None,
    style: JudgeStyle | None = None,
) -> list[dict[str, Any]]:
    """1 件ずつ「残す/捨てる」を判定する。

    小さいモデルは「一覧から選べ」が苦手で、読まずに先頭から番号を返してくる。
    二択にすると実際に見出しを読んで判断するようになる。
    問い合わせ回数は増えるが、1 回あたりのプロンプトが短いので合計時間は変わらない。
    """
    kept: list[dict[str, Any]] = []
    undecided: list[dict[str, Any]] = []
    style = style or JUDGE_STYLES["keep_true"]

    for item in items:
        title = str(item.get("title", ""))
        if time.monotonic() > deadline:
            LOG.warning("時間切れ。残りは判定せずそのまま通します")
            undecided.extend(items[items.index(item):])
            break

        try:
            answer = client.generate(
                style.build(title, rule), style.json_mode, style.max_tokens
            )
        except CuratorError as exc:
            LOG.warning("判定に失敗しました (%s)。この記事は残します", exc)
            undecided.append(item)
            if explain is not None:
                explain.append((title, "エラー→残す"))
            continue

        verdict = style.parse(answer, rule)
        if verdict is None:
            undecided.append(item)
            if explain is not None:
                explain.append((title, f"不明→残す ({answer.strip()[:20]})"))
        elif verdict:
            kept.append(item)
            if explain is not None:
                explain.append((title, "残す"))
        elif explain is not None:
            explain.append((title, "捨てる"))

    # 判定できなかったものは捨てない
    result = kept + undecided

    # 減りすぎたときの安全弁。判定を信じきると欄ごと空になることがある
    if rule.min_keep and len(result) < rule.min_keep:
        chosen = {id(x) for x in result}
        restored: set[str] = set()
        for item in items:
            if id(item) not in chosen:
                result.append(item)
                chosen.add(id(item))
                restored.add(str(item.get("title", "")))
                if len(result) >= rule.min_keep:
                    break
        LOG.warning("判定で %d 件まで減ったので、新しい順に %d 件まで戻しました",
                    len(kept), len(result))
        if explain is not None:
            # 「捨てる」と出したのに残ったものは、そう見えるようにしておく
            for i, (title, verdict) in enumerate(explain):
                if verdict == "捨てる" and title in restored:
                    explain[i] = (title, "捨てる→戻した")

    # 元の並び（新しい順）に戻す
    order = {id(x): i for i, x in enumerate(items)}
    result.sort(key=lambda x: order.get(id(x), 0))

    if len(result) > rule.keep:
        result = result[: rule.keep]
    return result


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

    kept: list[dict[str, Any]] = []  # batch 方式
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
    explain: dict[str, list[tuple[str, str]]] | None = None,
) -> list[dict[str, Any]]:
    """セクション配列を選別して返す。**決して例外を投げない。**

    Ollama が無い・落ちている・遅い・変な答えを返す、のいずれでも
    入力をそのまま返す。配信は今までどおり続く。
    """
    try:
        return _curate(sections, load_config(config_path), explain)
    except Exception:
        LOG.exception("選別で予期しない例外が出ました。収集したままの結果で続けます")
        return sections


def _curate(
    sections: list[dict[str, Any]],
    config: CuratorConfig,
    explain: dict[str, list[tuple[str, str]]] | None = None,
) -> list[dict[str, Any]]:
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

    # 入りきらないなら読み込ませない。無人で動くものが Pi を固めてはいけない
    fits, summary = memory_verdict(client.model_size(config.model))
    if not fits:
        LOG.warning("メモリが足りないので選別を飛ばします（%s）", summary)
        LOG.warning("小さいモデルに変えるか、curator.yaml の num_ctx を下げてください")
        return sections
    LOG.debug("メモリ確認: %s", summary)

    deadline = time.monotonic() + config.total_budget
    style = judge_style(config.judge_style)
    LOG.info("選別を開始します (%s / %s / 聞き方 %s)",
             config.base_url, config.model, config.judge_style)

    started = time.monotonic()
    try:
        for section in sections:
            rule = config.rules.get(str(section.get("id")))
            items = section.get("items") or []
            if rule is None or not items:
                continue

            before = len(items)
            section_started = time.monotonic()
            if rule.selects:
                if rule.mode == "batch":
                    items = select_items(client, items, rule, config.chunk_size, deadline)
                else:
                    log = [] if explain is not None else None
                    items = select_one_by_one(client, items, rule, deadline, log, style)
                    if explain is not None and log is not None:
                        explain[str(section.get("id"))] = log
            if rule.translate:
                items = translate_titles(client, items, deadline)

            section["items"] = items
            LOG.info("[%s] %d 件 → %d 件 (%.0f 秒)",
                     section.get("id"), before, len(items), time.monotonic() - section_started)
    finally:
        # 途中で何が起きても必ずメモリを返す
        client.unload()

    LOG.info("選別が終わりました (所要 %.0f 秒 / 上限 %.0f 秒)",
             time.monotonic() - started, config.total_budget)
    return sections


# ---------------------------------------------------------------------------
# 聞き方の採点
#
# 本番を 1 回回すと 15 分かかるうえ、答え合わせができない。
# 「良くなったのか」が分からないまま設定をいじることになるので、
# 答えの決まっている見出しで測れるようにする。
#
# 件数について:
#   8 件では何も分からない。コイン投げでも 8 件中 5 件は 36% の確率で当たる。
#   20 件にして、偶然にそうなる確率を毎回一緒に出す。
#
# 見出しについて:
#   実際の収集結果から採ったものと、判定がぶれないよう補ったものが混じっている。
#   境目のもの（盆栽の盗難は「社会の出来事」か？）はわざと入れていない。
#   ここで測りたいのは「そもそも見分けられるのか」であって、
#   境目をどちらに倒すかは criteria を書き換えて調整する話だから。
# ---------------------------------------------------------------------------
SELFTEST_CRITERIA = "政治、経済、災害、国際情勢、社会の大きな出来事。"

# fewshot 系に見せる正解例。下の採点用の見出しとは重ねないこと
# （見せた答えをそのまま返すだけでも満点になってしまう）
SELFTEST_EXAMPLES: list[tuple[str, bool]] = [
    ("日銀 政策金利の引き上げを決定 市場の反応は", True),
    ("夏バテに効く簡単レシピ5選", False),
    ("台風10号が九州に上陸 5万世帯が停電", True),
    ("人気俳優の結婚が発表される", False),
]

# category の聞き方が使う分野名
SELFTEST_KEEP_CATEGORIES = ["政治", "経済", "災害", "国際", "事件事故"]
SELFTEST_DROP_CATEGORIES = ["生活", "グルメ", "芸能", "スポーツ", "雑学"]


def selftest_rule() -> SectionRule:
    """採点で使う条件。本番の SectionRule と同じ形なので聞き方をそのまま試せる。"""
    return SectionRule(
        keep=len(SELFTEST_CASES),
        criteria=SELFTEST_CRITERIA,
        examples=SELFTEST_EXAMPLES,
        keep_categories=SELFTEST_KEEP_CATEGORIES,
        drop_categories=SELFTEST_DROP_CATEGORIES,
    )

SELFTEST_CASES: list[tuple[str, bool]] = [
    # 残すべき ─ 政治・経済・災害・国際・社会の大きな出来事
    ("政府、半導体分野への追加投資を決定", True),
    ("千葉豪雨で冠水の国道アンダーパス 停電で排水できず 運転手重体", True),
    ("アフガニスタン タリバン復権から5年 人道状況の悪化懸念", True),
    ("米 7月の小売業の売上高 前月比0.6％減 FRB利上げ観測やや後退", True),
    ("きょう終戦81年 全国戦没者追悼式に遺族ら 戦後生まれは過去最高", True),
    ("熊本地震 被災の福祉施設へ介護職員派遣 人件費は公費負担に", True),
    ("千葉県 明け方まで激しい雷雨恐れ", True),
    ("エルニーニョ 非常に強くなる恐れ", True),
    ("英国 右派政党の党首 辞職後の補欠選挙で再選も疑惑の調査続く", True),
    ("内閣支持率が急落 与党内から解散論も", True),
    # 捨てるべき ─ 生活の豆知識・グルメ・芸能・スポーツ・雑学
    ("スマホ水没・水ぬれ時 NGな行為", False),
    ("松屋の新メニューを実食レビュー 想像以上のボリューム", False),
    ("今年の夏に読みたいおすすめ小説10選", False),
    ("100均グッズで作る夏の収納アイデア", False),
    ("大リーグ村上宗隆選手が熊本支援のチャリティープロジェクト", False),
    ("人気アイドルグループ 新メンバーの加入を発表", False),
    ("プロ野球 阪神が3連勝 首位との差を2に縮める", False),
    ("話題のスイーツ店に3時間の行列 SNSで火が付く", False),
    ("猫が段ボールを好む理由を専門家が解説", False),
    ("寝つきをよくする5つの習慣 専門医が解説", False),
]


def coin_flip_probability(correct: int, total: int) -> float:
    """当てずっぽうで correct 件以上あたる確率。

    これを出さずに「5/8 だから一番」と言ってはいけない。
    コイン投げでも 8 件中 5 件は 36% の確率で起きる。
    """
    ways = sum(math.comb(total, k) for k in range(correct, total + 1))
    return ways / (2 ** total)


@dataclass
class StyleScore:
    name: str
    label: str
    kept_right: int = 0     # 残すべきを残せた数
    dropped_right: int = 0  # 捨てるべきを捨てられた数
    said_keep: int = 0      # 「残す」と答えた数（正誤を問わない）
    said_drop: int = 0      # 「捨てる」と答えた数（正誤を問わない）
    unknown: int = 0        # 答えを読み取れなかった数
    seconds: float = 0.0

    @property
    def correct(self) -> int:
        return self.kept_right + self.dropped_right

    @property
    def discriminates(self) -> bool:
        """両方の答えを実際に使い分けているか。

        全部「残す」でも全部「捨てる」でも正解率は 50% 付近になる。
        正解率だけ見ていると、何も判断していないものを見逃す。
        実際 keep_true は 8/8「捨てる」、score は 8/8「残す」で、
        どちらも 4/8 という「まとも」に見える数字を出していた。
        """
        return self.said_keep > 0 and self.said_drop > 0


SELFTEST_HALF = sum(1 for _, want in SELFTEST_CASES if want)


def run_selftest(config: CuratorConfig, style_names: list[str], verbose: bool) -> int:
    """聞き方ごとに同じ見出しを採点する。勝敗は正解率と偶然の確率の両方で決める。"""
    client = OllamaClient(config)
    try:
        client.available_models()
    except Exception as exc:
        print(f"[NG] Ollama に繋がりません ({type(exc).__name__})", file=sys.stderr)
        return 1

    # 入りきらないモデルを読み込ませると Pi ごと固まる。試す前に断る
    fits, summary = memory_verdict(client.model_size(config.model))
    print(f"メモリ: {summary}")
    if not fits:
        print(f"\n[NG] 今は空きが足りません。読み込ませると固まります ({config.model})",
              file=sys.stderr)
        print_memory_advice(config.model, sys.stderr)
        return 1

    total = len(SELFTEST_CASES)
    print(f"モデル: {config.model}")
    print(f"見出し {total} 件（残すべき {SELFTEST_HALF} / 捨てるべき {total - SELFTEST_HALF}）を "
          f"{len(style_names)} 通りの聞き方で採点します")
    print(f"問い合わせ {total * len(style_names)} 回。1 件 10 秒として "
          f"{total * len(style_names) * 10 // 60} 分ほどかかります\n")

    rule = selftest_rule()
    scores: list[StyleScore] = []
    try:
        for name in style_names:
            style = judge_style(name)
            print(f"── {name}: {style.label}")
            score = StyleScore(name, style.label)
            started = time.monotonic()
            for title, want in SELFTEST_CASES:
                try:
                    raw = client.generate(
                        style.build(title, rule), style.json_mode, style.max_tokens
                    )
                    got = style.parse(raw, rule)
                except CuratorError as exc:
                    print(f"   [NG] 問い合わせに失敗しました ({exc})")
                    got, raw = None, ""
                if got is None:
                    score.unknown += 1
                else:
                    if got:
                        score.said_keep += 1
                    else:
                        score.said_drop += 1
                    if got == want:
                        if want:
                            score.kept_right += 1
                        else:
                            score.dropped_right += 1
                if verbose:
                    shown = {True: "残す", False: "捨てる", None: f"不明({raw.strip()[:12]})"}[got]
                    mark = "○" if got == want else "×"
                    print(f"   {mark} 期待 {'残す' if want else '捨てる'} / 実際 {shown}  {title[:32]}")

            score.seconds = time.monotonic() - started
            scores.append(score)
            print(f"   残すべき {SELFTEST_HALF} 件 → 残せた {score.kept_right}")
            print(f"   捨てるべき {total - SELFTEST_HALF} 件 → 捨てられた {score.dropped_right}")
            if score.unknown:
                print(f"   答えを読み取れなかった {score.unknown} 件")
            p = coin_flip_probability(score.correct, total)
            if not score.discriminates:
                verdict = "← 片方の答えしか返していません。判断していない"
            elif p >= 0.05:
                verdict = f"← 当てずっぽうと区別が付きません (偶然にこうなる確率 {p:.0%})"
            else:
                verdict = f"← 見分けられています (偶然にこうなる確率 {p:.1%})"
            print(f"   正解 {score.correct}/{total}  {score.seconds / total:.1f} 秒/件  {verdict}\n")
    finally:
        client.unload()

    print("=" * 66)
    for score in sorted(scores, key=lambda s: -s.correct):
        p = coin_flip_probability(score.correct, total)
        state = "判断していない" if not score.discriminates else (
            "偶然と区別できない" if p >= 0.05 else "使える")
        print(f"  {score.name:14} {score.correct:2}/{total}  "
              f"{score.seconds / total:5.1f} 秒/件  {state}")

    usable = [s for s in scores
              if s.discriminates and coin_flip_probability(s.correct, total) < 0.05]
    if not usable:
        print("\nどの聞き方も当てずっぽうと区別が付きませんでした。")
        print("この見出しは人間なら迷わず分けられるものばかりなので、")
        print(f"{config.model} にこの仕事は無理だという結論になります。")
        print("\n次の手は 2 つです:")
        print("  1. 大きいモデルを試す。今のモデルは消さなくてよい（同時には載らない）")
        print("       ollama pull qwen2.5:3b")
        print("       python3 -m curator.llm --selftest --model qwen2.5:3b")
        print("     メモリが足りずに落ちるなら、一回り小さい gemma2:2b も日本語は強い")
        print("  2. LLM をやめる。curator.yaml の sections を消せば選別工程は素通しになる")
        return 1

    best = max(usable, key=lambda s: (s.correct, -s.seconds))
    print(f"\n{best.name} が使えます（{best.correct}/{total}、{best.seconds / total:.1f} 秒/件）。")
    print("curator.yaml の defaults を書き換えてください:")
    print(f"\n  judge_style: {best.name}\n")
    if best.name.startswith("fewshot"):
        print("この聞き方は各セクションの select.examples を使います。")
        print("選別の精度は criteria より examples で決まるので、")
        print("外した記事を見つけたら examples に足していくのが一番効きます。")
    return 0


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ローカル LLM で digest を選別します")
    parser.add_argument("digest", nargs="?", help="digest.json のパス")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="curator.yaml のパス")
    parser.add_argument("--env", default=str(DEFAULT_ENV_PATH), help=".env のパス")
    parser.add_argument("--check", action="store_true", help="Ollama とモデルの疎通を確認する")
    parser.add_argument("--dry-run", action="store_true", help="LLM を呼ばず、送る内容を表示する")
    parser.add_argument("--explain", action="store_true",
                        help="1 件ずつの採否を表示する（判定が効いているかの確認用）")
    parser.add_argument("--selftest", action="store_true",
                        help="答えの分かっている見出しで聞き方を採点する（digest 不要）")
    parser.add_argument("--model", help="このモデルで動かす（curator.yaml より優先）")
    parser.add_argument("--styles", help=f"--selftest で試す聞き方をカンマ区切りで指定 "
                                         f"(既定: {','.join(SELFTEST_STYLES)} / "
                                         f"すべて: {','.join(JUDGE_STYLES)})")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug ログまで出す")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    load_dotenv(args.env)
    config = load_config(args.config)
    if args.model:
        config.model = args.model.strip()

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

        fits, summary = memory_verdict(client.model_size(config.model))
        print(f"メモリ: {summary}")
        if not fits:
            print("\n[NG] 今は空きが足りません。読み込ませると固まります", file=sys.stderr)
            print("     毎朝の実行では自動的に選別を飛ばすので、配信は止まりません",
                  file=sys.stderr)
            print_memory_advice(config.model, sys.stderr)
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

    if args.selftest:
        names = [n.strip() for n in (args.styles or "").split(",") if n.strip()] or SELFTEST_STYLES
        unknown = [n for n in names if n not in JUDGE_STYLES]
        if unknown:
            parser.error(f"知らない聞き方です: {', '.join(unknown)}\n"
                         f"使えるのは: {', '.join(JUDGE_STYLES)}")
        return run_selftest(config, names, args.verbose)

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
                  f"(下限 {rule.min_keep} 件 / 方式 {rule.mode} / 聞き方 {config.judge_style} / "
                  f"和訳: {'あり' if rule.translate else 'なし'})", file=sys.stderr)
            if not rule.selects:
                continue
            # 実際に投げるものと同じプロンプトを出す
            if rule.mode == "batch":
                titles = [str(i.get("title", "")) for i in items[: config.chunk_size]]
                print(build_select_prompt(titles, rule.keep, rule.criteria), file=sys.stderr)
            else:
                style = judge_style(config.judge_style)
                title = str(items[0].get("title", ""))
                print(f"（1 件目の例。実際は {len(items)} 件に同じ形で聞きます）", file=sys.stderr)
                print(style.build(title, rule), file=sys.stderr)
        print("\n[dry-run] LLM は呼んでいません", file=sys.stderr)
        return 0

    before = {str(s.get("id")): len(s.get("items") or []) for s in sections}
    explain: dict[str, list[tuple[str, str]]] | None = {} if args.explain else None
    curated = curate(sections, args.config, explain)

    if explain is None:
        print(json.dumps(curated, ensure_ascii=False, indent=2))
    else:
        marks = {"残す": "○", "捨てる": "×", "捨てる→戻した": "△"}
        for section_id, judgements in explain.items():
            print(f"\n=== {section_id} ===", file=sys.stderr)
            for title, verdict in judgements:
                print(f"  {marks.get(verdict, '?')} [{verdict}] {title[:60]}", file=sys.stderr)
        verdicts = [v for j in explain.values() for _, v in j]
        kept = verdicts.count("残す")
        dropped = verdicts.count("捨てる")
        restored = verdicts.count("捨てる→戻した")
        unknown = len(verdicts) - kept - dropped - restored
        print(f"\n判定: 残す {kept} / 捨てる {dropped} / 判定できず {unknown}", file=sys.stderr)
        if restored:
            print(f"      うち {restored} 件は min_keep の安全弁で戻しました（△）", file=sys.stderr)
        if dropped + restored == 0:
            print("[警告] 1 件も捨てていません。モデルが条件を読めていない可能性があります",
                  file=sys.stderr)

    print("", file=sys.stderr)
    for section in curated:
        section_id = str(section.get("id"))
        after = len(section.get("items") or [])
        mark = "→" if before.get(section_id) == after else "⇒"
        print(f"  {section_id:9} {before.get(section_id, 0):3} 件 {mark} {after:3} 件", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
