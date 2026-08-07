"""新興アクター候補の集計→提案 (Actor Recall Layer Part C2)。

``actor_provisional`` entity を裏取り (N 記事) で絞り、``actor_update_proposals`` に
``corpus_emerging_actor`` として上げる。dedup_key で重複/却下再提案を防止 (MITRE 同期と同型)。
人が承認すると pages.py が辞書化 + backfill する。設計: docs/actor_recall_layer.md。
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from src.cti.actor_normalizer import load_actor_aliases
from src.cti.generic_alias_words import is_generic_alias
from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository

_log = get_logger(__name__)

_MIN_ARTICLES = 3  # この本数以上の独立記事で観測された候補のみ提案 (one-off 誤parse を排除)
_WINDOW_DAYS = 90  # 現在進行形の新興アクターに focus (古い一過性候補は提案しない)
PROPOSAL_TYPE = "corpus_emerging_actor"
# ベンダ designation key の判定 (表示名復元・signal 推定用)。harvest の regex と対応。
_VENDOR_KEY_RE = re.compile(r"^(storm|unc|ta|tag|dev|cl|uat|apt)[-\d]", re.IGNORECASE)
_VENDOR_UPPER: frozenset[str] = frozenset({"unc", "ta", "tag", "dev", "cl", "uat", "apt"})


def _slug(key: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    return s or "actor"


def _display_name(key: str) -> str:
    """正規キーから提案用の表示名を復元 (ベンダ designation は prefix を大文字化)。"""
    if _VENDOR_KEY_RE.match(key):
        if "-" in key:
            head, _, tail = key.partition("-")
            head_disp = head.upper() if head.lower() in _VENDOR_UPPER else head.capitalize()
            return f"{head_disp}-{tail.upper()}" if tail else head_disp
        m = re.match(r"^([a-z]+)(\d+)$", key, re.IGNORECASE)
        if m and m.group(1).lower() in _VENDOR_UPPER:
            return m.group(1).upper() + m.group(2)  # apt77 → APT77 / unc5221 → UNC5221
    return " ".join(w.capitalize() for w in key.split())


def _signal_of(key: str) -> str:
    return "vendor_designation" if _VENDOR_KEY_RE.match(key) else "llm_primary"


def propose_emerging_actors(
    repo: RunHistoryRepository,
    *,
    run_id: int | None = None,
    min_articles: int = _MIN_ARTICLES,
    window_days: int = _WINDOW_DAYS,
) -> dict[str, int]:
    """裏取り済み新興候補を proposal キューに上げる。{proposed, skipped, candidates} を返す。"""
    since = datetime.now(UTC) - timedelta(days=window_days)
    registry = load_actor_aliases()
    candidates = repo.count_provisional_actor_candidates(min_articles=min_articles, since=since)
    proposed = skipped = 0
    for key, n in candidates:
        # knows_name = 散文名の word-boundary 照合 / resolve_source_slug = slug 正規化照合。
        # 後者が無いと "thegentlemen" 型の綴り違い slug が未知扱いで提案され、既存 actor の
        # id 分裂 (2026-08-01 事故) を起こす。
        if registry.knows_name(key) or registry.resolve_source_slug(key) is not None:
            skipped += 1
            continue
        # 地政学ノイズ (国名/統治・軍事組織) は提案しない — 取込 filter (2026-08-01) 以前の
        # 過去蓄積が provisional に残っていても、提案経路で二重に堰き止める。
        from src.cti.actor_candidates import is_geopolitical_noise

        if is_geopolitical_noise(key):
            skipped += 1
            continue
        dedup_key = f"corpus:{key}"
        if repo.find_actor_update_proposal(proposal_type=PROPOSAL_TYPE, dedup_key=dedup_key):
            skipped += 1  # 既に提案済み or 却下済み (却下は再提案しない)
            continue
        samples = repo.sample_articles_for_provisional(key, limit=3)
        display = _display_name(key)
        signal = _signal_of(key)
        payload: dict[str, Any] = {
            "id": _slug(key),
            "canonical": display,
            "aliases": [],  # canonical が case-insensitive に key を拾うため alias 不要
            "kind": "group",
            "family": "",
            "_evidence": {
                "key": key,
                "article_count": n,
                "signal": signal,
                "sample_article_ids": [aid for aid, _ in samples],
                "sample_titles": [t for _, t in samples],
            },
        }
        rationale = f"コーパス {n} 記事で観測 (signal={signal})、辞書未収録の新興アクター候補"
        # 一般語衝突ゲート (2026-07-31 運用レビュー評価の再発防止): 07-26 のバッチ承認で
        # Play/Chaos/Deadlock 等 11 体が ambiguous なしで辞書入りし、4 日で言及層の 59% が
        # 誤検出化した。既知一般語 (GENERIC_ALIAS_WORDS SSoT) は起票時に ambiguous=true を
        # 自動付与する (承認 handler は payload をそのまま yaml へ通すため辞書にもそのまま載る)。
        # SSoT 未収録の 1 語名にもレビュー注記を付け、承認前の人の目で衝突を判断させる。
        if is_generic_alias(display) or is_generic_alias(key):
            payload["ambiguous"] = True
            rationale += "。⚠一般語衝突 — ambiguous=true を自動付与 (固有 cue の追記を推奨)"
        elif " " not in display.strip():
            rationale += "。1 語名 — 一般語衝突でないか承認前に確認"
        repo.insert_actor_update_proposal(
            run_id=run_id,
            proposal_type=PROPOSAL_TYPE,
            mitre_group="",
            dedup_key=dedup_key,
            actor_id=None,
            payload=json.dumps(payload, ensure_ascii=False),
            rationale=rationale,
        )
        proposed += 1
    _log.info(
        "emerging_actor_proposals",
        proposed=proposed,
        skipped=skipped,
        candidates=len(candidates),
    )
    return {"proposed": proposed, "skipped": skipped, "candidates": len(candidates)}
