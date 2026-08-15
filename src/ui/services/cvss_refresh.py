"""CVSS 補給ジョブ (2026-08-15)。

routing の深刻度ゲート (``max_cvss``) と記事表示は ``data/nvd_cache.json`` を
**cache 読みするだけ**で、補給は本ジョブが担う。

背景: 従来の補給は app 起動時の 8 件のみで、直近 30 日の CVE 2,000 件に対し
未取得が 1,700 件超に滞留していた。深刻度で alert を絞る判定 (案 D) は
「CVSS 不明 = 判定不能」に倒れるため、補給が滞ると**重大脆弱性を watch へ
落とす方向**に静かに劣化する。毎時 bounded に取得して枯渇を防ぐ。

NVD のレート制限を尊重し 1 回あたり件数・時間の両方で打ち切る (fail-safe)。
"""

from __future__ import annotations

from src.logging_config import get_logger

_log = get_logger(__name__)

# 1 回あたりの取得上限。NVD の公開 API は無認証で 5 req/30s が目安のため、
# 15 件/90 秒 = ちょうど上限ペースに揃える (毎時 15 件 = 1 日 360 件)。
MAX_FETCH_PER_RUN = 15
DEADLINE_SECONDS = 90.0
# 補給対象とする記事の遡り日数 (古い CVE は表示需要が低く routing も過ぎている)。
LOOKBACK_DAYS = 30
# 候補上限。⚠ 500 だと頻度上位しか候補に入らず、単発言及の CVE (~1,200 件) が
# 恒久的に「CVSS 不明」のまま残る (2026-08-15 再評価で判明)。30 日窓の全 CVE
# (~2,000) を覆う 2000 に設定 — 定常負荷は TTL 30 日の再取得 ~67/日 + 新規 ~70/日
# ≈ 140/日 < 取得能力 360/日 で持続可能。候補は頻度降順なので、続報が出た
# 重要 CVE ほど先に温まる (「重要なら続報が出る」原理と整合)。
CANDIDATE_LIMIT = 2000


async def run_cvss_refresh() -> None:
    """直近記事の CVE について CVSS cache を bounded に補給する。"""
    from src.storage.run_history import RunHistoryRepository
    from src.tools.nvd_client import refresh_cvss

    repo = RunHistoryRepository()
    candidates = repo.recent_cve_values(days=LOOKBACK_DAYS, limit=CANDIDATE_LIMIT)
    if not candidates:
        _log.info("cvss_refresh_no_candidates")
        return
    try:
        fetched = refresh_cvss(
            candidates,
            max_fetch=MAX_FETCH_PER_RUN,
            deadline_seconds=DEADLINE_SECONDS,
        )
    except Exception as e:  # noqa: BLE001 — 補給失敗で scheduler を落とさない
        _log.warning("cvss_refresh_failed", error=f"{type(e).__name__}: {e}")
        return
    _log.info("cvss_refresh_completed", candidates=len(candidates), fetched=fetched)
