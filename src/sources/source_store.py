"""ソース config (feeds / watchers / scrapers) の SSoT store。

routing / PIR / source_quality と同じく **config_store (DB, app_config_versions) を
runtime SSoT** にする。yaml は初回 seed 専用 (git 追跡 = ツール同梱の既定購読)。
購読の live 状態は DB に版保存され (履歴 + revert + pg_dump backup 対象)、git には入らない。

transport ↔ config_store key ↔ yaml file の対応と **flag 分岐をこの 1 箇所に集約** する
(loaders / writers に散らさない = DRY)。

- ``SOURCES_CONFIG_DB`` (既定 ``1``=ON) で挙動を切替: OFF にすると loaders / writers が
  完全に旧来の yaml 直読み/直書きに戻る (即時 rollback、 redeploy 不要)。
- DB-first 読みは key 未投入 / DB 読み失敗時に seed yaml へ fail-safe fallback する。
- ``path`` 明示時 (テスト / seed) は常に yaml を直読みする (旧挙動を保存)。

将来の多人数化 seam: 現在は単一 key (feeds/watchers/scrapers)。owner 別にするなら
key を ``feeds:{owner}`` に namespacing するか owner 列を足す additive 変更で済む
(本 module はそこへ負債を残さないための分離であり、multi-tenancy 自体は作らない)。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml

from src.logging_config import get_logger

_log = get_logger(__name__)

TransportT = Literal["rss", "sitemap", "html_scraper"]

# transport → (config_store key, yaml path, root list key)。
# yaml path は各 loader / writer の定数と同値 (config/ 直下の安定 literal)。
_SPEC: dict[str, tuple[str, Path, str]] = {
    "rss": ("feeds", Path("config/feeds.yaml"), "feeds"),
    "sitemap": ("watchers", Path("config/watchers.yaml"), "watchers"),
    "html_scraper": ("scrapers", Path("config/scrapers.yaml"), "scrapers"),
}

_ENV_FLAG = "SOURCES_CONFIG_DB"


def is_db_mode() -> bool:
    """DB を SSoT にするか (既定 ON)。``SOURCES_CONFIG_DB=0`` で yaml 旧挙動へ rollback。"""
    return os.environ.get(_ENV_FLAG, "1") != "0"


def config_key_for(transport: TransportT) -> str:
    return _SPEC[transport][0]


def yaml_path_for(transport: TransportT) -> Path:
    return _SPEC[transport][1]


def root_key_for(transport: TransportT) -> str:
    return _SPEC[transport][2]


def _read_yaml_entries(path: Path, root_key: str) -> list[dict[str, Any]]:
    """yaml file の root_key list を dict 要素のみ返す (存在/壊れは空 list、fail-safe)。"""
    if not path.exists():
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        _log.warning("source_store_yaml_parse_failed", path=str(path), error=str(e))
        return []
    items = raw.get(root_key, []) if isinstance(raw, dict) else []
    return [e for e in (items or []) if isinstance(e, dict)]


def _seed_entries_from_yaml(transport: TransportT) -> list[dict[str, Any]]:
    _, path, root_key = _SPEC[transport]
    return _read_yaml_entries(path, root_key)


def load_entries(transport: TransportT, *, path: Path | None = None) -> list[dict[str, Any]]:
    """transport の entry list を返す (list of dict)。

    - ``path`` 明示 → その yaml を直読み (テスト / seed loader 用、旧挙動)。
    - flag ON → config_store の現在値。未投入/読み失敗なら seed yaml に fallback。
    - flag OFF → seed yaml (= 既定 file) を直読み (旧挙動)。
    """
    if path is not None:
        return _read_yaml_entries(path, root_key_for(transport))
    if is_db_mode():
        val: Any = None
        try:
            from src.storage.config_store import get_config

            val = get_config(config_key_for(transport))
        except Exception as e:  # noqa: BLE001
            _log.warning("source_store_db_read_failed", transport=transport, error=str(e))
            val = None
        if isinstance(val, list):
            return [e for e in val if isinstance(e, dict)]
        return _seed_entries_from_yaml(transport)
    return _seed_entries_from_yaml(transport)


def _atomic_write_yaml(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    tmp.replace(path)


def save_entries(transport: TransportT, entries: list[dict[str, Any]], *, note: str) -> int | None:
    """transport の entry list 全体を保存。

    - flag ON → config_store に新 version として保存 (履歴) + registry cache invalidate。
      返り値 = 新 version 番号。
    - flag OFF → 既定 yaml file を atomic 上書き (旧挙動)。返り値 None。
    """
    if is_db_mode():
        from src.storage.config_store import save_config

        version = save_config(config_key_for(transport), entries, note=note)
        invalidate_caches(transport)
        return version
    _, path, root_key = _SPEC[transport]
    _atomic_write_yaml(path, {root_key: entries})
    invalidate_caches(transport)
    return None


def invalidate_caches(transport: TransportT) -> None:
    """watchers / scrapers の registry module cache を reset (次 build で再読込)。"""
    try:
        if transport == "html_scraper":
            from src.watchers.scrapers_registry import reset_cache

            reset_cache()
        elif transport == "sitemap":
            from src.watchers.yaml_registry import reset_cache

            reset_cache()
    except Exception:  # noqa: BLE001
        pass


def seed_all_if_absent() -> dict[str, bool]:
    """未投入の 3 transport を seed yaml から config_store に取り込む (起動時、full instance)。

    Returns: {config_key: seeded(bool)}。
    """
    out: dict[str, bool] = {}
    try:
        from src.storage.config_store import seed_config_if_absent
    except Exception as e:  # noqa: BLE001
        _log.warning("source_store_seed_import_failed", error=str(e))
        return {key: False for key, _, _ in _SPEC.values()}
    for _transport, (key, path, root_key) in _SPEC.items():
        try:
            seeded = seed_config_if_absent(
                key,
                _read_yaml_entries(path, root_key),
                note=f"初期 seed (config/{path.name})",
            )
            out[key] = seeded
        except Exception as e:  # noqa: BLE001
            _log.warning("source_store_seed_failed", key=key, error=str(e))
            out[key] = False
    return out
