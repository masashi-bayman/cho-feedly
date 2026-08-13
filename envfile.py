#!/usr/bin/env python3
"""
.env を読んで os.environ に入れる。

python-dotenv を使わないのは、この程度の処理のために Raspberry Pi へ
依存を増やしたくないため。run_daily.py と render_html.py の両方が使う。

すでに環境にある値は上書きしない。systemd の EnvironmentFile や
手動の export が .env より優先される。
値は絶対にログへ出さない（鍵そのものであるため）。
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

LOG = logging.getLogger("envfile")

# KEY=VALUE。先頭の export は許す
ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def load_dotenv(path: str | Path) -> int:
    """読み込んだ件数を返す。ファイルが無くても落とさない。"""
    path = Path(path)
    if not path.exists():
        LOG.info(".env がありません (%s)。環境変数をそのまま使います", path)
        return 0

    try:
        mode = path.stat().st_mode
        if mode & 0o077:
            LOG.warning(".env が他ユーザーから読める状態です。chmod 600 %s を推奨します", path)
    except OSError:
        pass

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        LOG.warning(".env を読めません: %s", exc)
        return 0

    loaded = 0
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        matched = ENV_LINE_RE.match(line)
        if not matched:
            continue

        key, value = matched.group(1), matched.group(2).strip()
        if value[:1] in ("'", '"'):
            quote_char = value[0]
            closing = value.find(quote_char, 1)
            value = value[1:closing] if closing > 0 else value[1:]
        else:
            # 引用されていない場合だけ行末コメントを落とす
            value = value.split(" #", 1)[0].strip()

        if key in os.environ:
            continue
        os.environ[key] = value
        loaded += 1

    LOG.info(".env から %d 件の設定を読みました", loaded)
    return loaded
