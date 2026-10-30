"""運用者ローカルの上書き層 (``config/local/``、gitignore 済み、2026-10-30)。

公開リポには汎用の規則と例 (``*.example`` の placeholder) だけを置き、運用者固有の
一覧 (収集先・敵対的と分類する登録ドメイン・per-source の信頼度上書き等) は
``config/local/`` に置く。``CLAUDE.local.md`` / ``prompts/_persona_local.j2`` と同じ型で、
**ファイルがあれば読み、無ければ汎用の規則だけで動く**。

- 置き場所の既定は ``<repo>/config/local``。コンテナでは ``./config:/app/config`` の
  bind mount にそのまま乗る (image には焼かない — ``.dockerignore`` で除外)。
- ``KUEBIKO_LOCAL_CONFIG_DIR`` で置き場所を差し替えられる (テストは conftest が
  存在しない一時ディレクトリへ向け、開発機の実ファイルに左右されないようにする)。
- 読み手 (``hostile_hosts`` / ``source_basis``) は、どちらを使ったかを件数だけログに出す。
  **一覧の中身 (ドメイン名等) はログに出さない**。

詳細と復元手順は docs/local_overlay.md。
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_LOCAL_CONFIG_DIR = "KUEBIKO_LOCAL_CONFIG_DIR"

_DEFAULT_DIR = Path(__file__).resolve().parent.parent / "config" / "local"


def local_config_dir() -> Path:
    """ローカル上書き層のディレクトリ (呼ぶたびに env を見る)。"""
    raw = (os.environ.get(ENV_LOCAL_CONFIG_DIR) or "").strip()
    return Path(raw) if raw else _DEFAULT_DIR


def local_path(name: str) -> Path:
    """ローカル上書き層のファイルの path (存在するとは限らない)。

    ``name`` は ``config/local/`` からの相対名 (例: ``hostile_domains.yaml``)。
    外へ出る名前 (絶対 path・``..``) は拒否する。
    """
    rel = Path(name)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise ValueError(f"local overlay の名前が不正です: {name!r}")
    return local_config_dir() / rel
