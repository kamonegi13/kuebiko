"""PIR を triage / synthesis に注入する hook layer。

各 layer は config/pir.yaml が空 / 不在の場合は **既存挙動を維持** する (no-op default)。
これにより migration 前後で挙動が同じことを保証する。

routing への直接注入 (R0 = target_channel override) は 2026-06-13 に撤去。
チャンネル決定権は routing に一本化し、PIR の優先度は triage の importance
評価のみを経由して配信に届く (PIR=関心の定義と評価 / routing=配信)。
"""

from __future__ import annotations

import os

from src.logging_config import get_logger
from src.pir.loader import load_pir_config
from src.pir.models import Pir, PirConfig

_log = get_logger(__name__)

# 環境変数で PIR-driven triage を無効化可能 (緊急 rollback 用)
_PIR_DRIVEN_TRIAGE_ENV = "PIR_DRIVEN_TRIAGE"

# 運用 config として DB を正とする (案 B 拡張)。config/pir.yaml は初回 seed 専用。
PIR_CONFIG_KEY = "pir"

# Cache (in-process、container 再起動で reload)
_cached_config: PirConfig | None = None


def load_current_pir_config() -> PirConfig:
    """DB (config_store) 優先で PIR config をロード (fresh, 非 cache)。

    未保存/障害/破損時は config/pir.yaml (seed) に fallback。read-modify-write する
    UI endpoint と get_pir_config の loader が使う。
    """
    raw: object = None
    try:
        from src.storage.config_store import get_config

        raw = get_config(PIR_CONFIG_KEY)
    except Exception as e:  # noqa: BLE001 — DB 障害で PIR を壊さない
        _log.warning("pir_db_read_failed", error=str(e))
        raw = None
    if isinstance(raw, dict):
        try:
            from src.pir.loader import strip_legacy_pir_keys

            return PirConfig.model_validate(strip_legacy_pir_keys(raw))
        except Exception as e:  # noqa: BLE001 — 破損 DB 値は seed に degrade
            _log.warning("pir_db_parse_failed", error=str(e))
    return load_pir_config()


def get_pir_config(force_reload: bool = False) -> PirConfig:
    """PIR config を取得 (cache あり、DB 正)。"""
    global _cached_config
    if _cached_config is None or force_reload:
        _cached_config = load_current_pir_config()
    return _cached_config


def persist_pir_config(config: PirConfig) -> int:
    """PIR config を DB (config_store) に版保存し cache を無効化。返り値 = version。"""
    from src.storage.config_store import save_config

    version = save_config(
        PIR_CONFIG_KEY,
        config.model_dump(mode="json", exclude_none=False),
        note="UI 編集",
    )
    invalidate_cache()
    # L1c: PIR 編集を過去記事の pir タグへ即伝播する (日次 full-replace バッチ待ちを解消)。
    # inline タグ (取込時) は新記事のみ現行 config を反映するため、編集で変わった過去記事の
    # match は rebuild で更新する。編集は稀なので同期 full 90d scan で許容。失敗は飲む。
    try:
        from src.pir.persist import rebuild_pir_entities

        _log.info("pir_edit_rebuilt", **rebuild_pir_entities())
    except Exception as e:  # noqa: BLE001 — rebuild 失敗で保存自体は成功させる
        _log.warning("pir_edit_rebuild_failed", error=str(e))
    return version


def seed_pir_if_absent() -> bool:
    """DB 未投入なら config/pir.yaml seed を version 1 として取り込む (起動時)。"""
    try:
        from src.storage.config_store import seed_config_if_absent

        return seed_config_if_absent(
            PIR_CONFIG_KEY,
            load_pir_config().model_dump(mode="json", exclude_none=False),
            note="初期 seed (config/pir.yaml)",
        )
    except Exception as e:  # noqa: BLE001
        _log.warning("pir_seed_failed", error=str(e))
        return False


def invalidate_cache() -> None:
    """PIR config cache を無効化 (編集後に呼ぶ)。"""
    global _cached_config
    _cached_config = None
    # salience の PIR 優先度表 (リスト順由来) も config 変更で無効化する。
    try:
        from src.assessment.salience import _pir_priority_map

        _pir_priority_map.cache_clear()
    except Exception:  # noqa: BLE001 — cache clear 失敗は無害 (次回 restart で更新)
        pass


def is_pir_driven_triage_enabled() -> bool:
    """PIR-driven triage を有効化する条件。

    env var で disable されているか、PIR yaml が空なら False (legacy 動作)。
    """
    if os.environ.get(_PIR_DRIVEN_TRIAGE_ENV, "1").strip() in ("0", "false", "no", "off"):
        return False
    cfg = get_pir_config()
    return bool(cfg.enabled_priorities())


# ----- triage prompt 注入 -----


def build_triage_high_criteria(pirs: list[Pir]) -> str:
    """target_importance=high な PIR の title 群を、triage prompt の "high" 基準として整形。"""
    high_pirs = [p for p in pirs if p.enabled and p.target_importance == "high"]
    if not high_pirs:
        return ""
    lines: list[str] = []
    for i, p in enumerate(high_pirs, 1):
        # title を 1 行で、description 先頭を補足として
        desc_first = p.description.strip().splitlines()[0] if p.description.strip() else ""
        desc_brief = f" — {desc_first[:120]}" if desc_first else ""
        lines.append(f"{i}. {p.title}{desc_brief}")
    return "\n".join(lines)


def build_triage_medium_criteria(pirs: list[Pir]) -> str:
    """target_importance=medium な PIR の title 群を、triage prompt の "medium" 基準として整形。"""
    medium_pirs = [p for p in pirs if p.enabled and p.target_importance == "medium"]
    if not medium_pirs:
        return ""
    lines: list[str] = []
    for p in medium_pirs:
        desc_first = p.description.strip().splitlines()[0] if p.description.strip() else ""
        desc_brief = f" — {desc_first[:100]}" if desc_first else ""
        lines.append(f"- {p.title}{desc_brief}")
    return "\n".join(lines)


# ----- synthesis 統合 -----


def build_synthesis_pir_context(pirs: list[Pir]) -> list[dict[str, str]]:
    """synthesis prompt に渡す PIR list (簡易版)。

    各 PIR の title + description だけを context として渡し、"PIR 達成度" section の
    生成に使う。
    """
    return [
        {
            "id": p.id,
            "title": p.title,
            "description": (p.description or "").strip()[:300],
        }
        for p in pirs
        if p.enabled
    ]
