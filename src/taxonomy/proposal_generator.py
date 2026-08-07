"""Phase H: taxonomy 提案生成 (機械 + LLM ハイブリッド)。

実装済み:
    - pattern_1 (未分類 sector/country の統合 / 新規判定) — 機械
    - pattern_4 (typo / 表記ゆれ) — 機械 (Levenshtein)

退役:
    - pattern_5 (新興 actor、タイトル正規表現の旧 MVP) — 2026-08-01 に
      アクター・リコール層 (actor_candidates.py + corpus_emerging_actor 提案) へ一本化。
      UAT/APT designation は収穫正規表現に引き継ぎ済み。既存レコードは履歴として残る。

その他 (2/3/6/7/8) は Phase 3 で追加実装、現状は skeleton として interface のみ。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.cti.taxonomy_normalizer import (
    UNCATEGORIZED_SECTOR,
    TaxonomyNormalizer,
    load_normalizer,
)
from src.logging_config import get_logger

_log = get_logger(__name__)

DEFAULT_DB_PATH = Path("data/run_history.db")

# Tier 1 (自動補正候補) と判定する Levenshtein 距離の上限
TYPO_MAX_DISTANCE = 2
# 距離 / 最大長 の上限。**短い CJK 語の誤爆を防ぐ核心のゲート**。固定の絶対距離だけだと
# 2 文字の日本語語 (航空/化学 等) は任意の別 2 文字 alias と距離 2 以内になり、無関係な
# canonical (金融/教育 等) に typo 誤判定される。長さ比で「3 文字に 1 編集」程度に制限する。
# 例: 航空↔金融 = 2/2 = 1.0 (却下) / govenment↔government = 1/10 = 0.1 (採用)。
TYPO_MAX_RATIO = 0.34
# Tier 1 で typo と判定する最小観測件数 (1 件だけだと誤検知 risk)
TYPO_MIN_OCCURRENCES = 2
# pattern_1 で新カテゴリ提案を出す最小件数
NEW_CATEGORY_MIN_COUNT = 3


@dataclass(frozen=True)
class GeneratedProposal:
    """taxonomy review pipeline が生成する 1 提案 (DB 永続化前)。"""

    proposal_type: str
    tier: str
    target_yaml: str
    target_canonical: str | None
    proposed_change: dict[str, object]  # 永続化時に JSON 化
    rationale: str
    confidence: str  # 'high' / 'medium' / 'low'
    evidence_count: int
    evidence_ids: list[str]


# ---------- pattern_4: typo / 表記ゆれ (機械検出) ----------


def _levenshtein(a: str, b: str) -> int:
    """簡易 Levenshtein 距離 (O(len(a)*len(b)))。"""
    if a == b:
        return 0
    if not a or not b:
        return max(len(a), len(b))
    a = a.lower()
    b = b.lower()
    prev = list(range(len(b) + 1))
    curr = [0] * (len(b) + 1)
    for i, ca in enumerate(a, 1):
        curr[0] = i
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            curr[j] = min(
                prev[j] + 1,  # deletion
                curr[j - 1] + 1,  # insertion
                prev[j - 1] + cost,  # substitution
            )
        prev, curr = curr, prev
    return prev[len(b)]


def _looks_like_typo(a: str, b: str, *, max_distance: int = TYPO_MAX_DISTANCE) -> tuple[bool, int]:
    """a と b が typo/表記ゆれ関係か + その編集距離を返す。

    絶対距離 (max_distance 以下) と長さ比 (TYPO_MAX_RATIO 以下) の**両方**を満たすときだけ True。
    長さ比ゲートが短い CJK 語の誤爆 (航空↔金融 等) を弾く。
    """
    d = _levenshtein(a, b)
    if d == 0 or d > max_distance:
        return False, d
    if d / max(len(a), len(b), 1) > TYPO_MAX_RATIO:
        return False, d
    return True, d


def detect_typo_candidates(
    *,
    normalizer: TaxonomyNormalizer,
    db_path: Path,
    lookback_days: int,
    now: datetime | None = None,
) -> list[GeneratedProposal]:
    """uncategorized な victim_sector_raw を集計し、既存 alias との編集距離で typo 検出。"""
    base = now or datetime.now(UTC)
    since = base - timedelta(days=lookback_days)
    proposals: list[GeneratedProposal] = []

    from src.storage.db_backend import connect as _backend_connect

    con = _backend_connect(db_path)
    if hasattr(con, "row_factory"):
        con.row_factory = sqlite3.Row
    try:
        # victim_sector_raw が uncategorized になっている article を頻度集計。
        # 2026-07-31: 同一 article が run 横断で複数行あると件数が水増しされ、
        # 実質 1-2 記事の raw が閾値を超えて提案化していた → DISTINCT で記事単位に。
        rows = con.execute(
            """
            SELECT victim_sector_raw AS raw, COUNT(DISTINCT article_id) AS n,
                   GROUP_CONCAT(article_id) AS ids
              FROM articles
             WHERE victim_sector_canonical = 'uncategorized'
               AND victim_sector_raw IS NOT NULL
               AND datetime(created_at) >= datetime(?)
             GROUP BY victim_sector_raw
             ORDER BY n DESC
             LIMIT 200
            """,
            (since.isoformat(),),
        ).fetchall()
    finally:
        con.close()

    for r in rows:
        raw = str(r["raw"]).strip()
        # 現辞書で未解決の raw のみ提案対象 (2026-07-12 根治)。これが弾くのは
        # (a) 番兵語 (Not Found/不明 等 → normalize が None) と
        # (b) alias 追加で解決済みの raw — DB の旧 uncategorized 行は書き換わらないため、
        #     窓内に残る間この skip が無いと「学習済みの値」を毎週再提案し続ける。
        if not raw or normalizer.normalize_sector(raw)[0] != UNCATEGORIZED_SECTOR:
            continue
        count = int(r["n"])
        if count < TYPO_MIN_OCCURRENCES:
            continue
        # 既存 alias 全件で最も近いものを探す (長さ比ゲートで短い CJK 語の誤爆を排除)
        best: tuple[int, str] | None = None
        raw_lower = raw.lower()
        for alias_lower, canonical in normalizer.sector_lookup.items():
            ok, d = _looks_like_typo(raw_lower, alias_lower)
            if ok and (best is None or d < best[0]):
                best = (d, canonical)
        if best is None:
            continue
        distance, canonical = best
        # 同一 article_id の run 重複行を evidence からも除去 (順序保持)
        evidence_ids = list(dict.fromkeys(str(r["ids"]).split(",") if r["ids"] else []))[:5]
        proposals.append(
            GeneratedProposal(
                proposal_type="pattern_4",
                tier="tier_1_auto",
                target_yaml="victim_sectors",
                target_canonical=canonical,
                proposed_change={
                    "kind": "add_alias",
                    "alias": raw,
                    "canonical": canonical,
                },
                rationale=(
                    f"'{raw}' は既存 canonical '{canonical}' との編集距離 {distance} で typo "
                    f"の可能性が高い ({count} 件観測)"
                ),
                confidence="high" if distance == 1 else "medium",
                evidence_count=count,
                evidence_ids=evidence_ids,
            ),
        )
    return proposals


# ---------- pattern_1: 未分類カテゴリの統合 / 新規判定 ----------


def detect_uncategorized_clusters(
    *,
    normalizer: TaxonomyNormalizer,
    db_path: Path,
    lookback_days: int,
    now: datetime | None = None,
    min_count: int = NEW_CATEGORY_MIN_COUNT,
) -> list[GeneratedProposal]:
    """uncategorized の raw を頻度集計し、min_count 以上のものを新カテゴリ候補として提示。

    typo は pattern_4 で別途扱うため、ここでは編集距離が大きい (= 既存と異なる) ものを
    集める。LLM は将来連携、MVP では機械抽出のみで「user が new 判断する材料」を提供。
    """
    base = now or datetime.now(UTC)
    since = base - timedelta(days=lookback_days)
    proposals: list[GeneratedProposal] = []

    from src.storage.db_backend import connect as _backend_connect

    con = _backend_connect(db_path)
    if hasattr(con, "row_factory"):
        con.row_factory = sqlite3.Row
    try:
        # 2026-07-31: run 重複行の水増し排除 (pattern_4 と同じ) — DISTINCT で記事単位に
        rows = con.execute(
            """
            SELECT victim_sector_raw AS raw, COUNT(DISTINCT article_id) AS n,
                   GROUP_CONCAT(article_id) AS ids
              FROM articles
             WHERE victim_sector_canonical = 'uncategorized'
               AND victim_sector_raw IS NOT NULL
               AND datetime(created_at) >= datetime(?)
             GROUP BY victim_sector_raw
             HAVING COUNT(DISTINCT article_id) >= ?
             ORDER BY n DESC
             LIMIT 50
            """,
            (since.isoformat(), min_count),
        ).fetchall()
    finally:
        con.close()

    for r in rows:
        raw = str(r["raw"]).strip()
        # 現辞書で未解決の raw のみ「新カテゴリ候補」(2026-07-12 根治)。番兵語
        # ('Not Found' 167 件が毎週浮上) と、alias 追加で解決済みの raw (旧 uncategorized 行が
        # 窓内に残る間の再提案) の両方を弾く。
        if not raw or normalizer.normalize_sector(raw)[0] != UNCATEGORIZED_SECTOR:
            continue
        count = int(r["n"])
        # 既存 alias と typo っぽいもの (距離1・長さ比内) は pattern_4 に任せる。
        # 長さ比ゲートにより短い CJK 語 (化学 等) は typo 扱いされず、正しく新カテゴリ候補になる。
        raw_lower = raw.lower()
        is_typo_likely = any(
            _looks_like_typo(raw_lower, alias_lower, max_distance=1)[0]
            for alias_lower in normalizer.sector_lookup
        )
        if is_typo_likely:
            continue
        evidence_ids = list(dict.fromkeys(str(r["ids"]).split(",") if r["ids"] else []))[:5]
        # canonical id 候補 (slugify ぽい簡易)
        suggested_id = raw_lower.replace(" ", "_").replace("-", "_")
        suggested_id = "".join(c for c in suggested_id if c.isalnum() or c == "_")[:32]
        proposals.append(
            GeneratedProposal(
                proposal_type="pattern_1",
                tier="tier_2_review",
                target_yaml="victim_sectors",
                target_canonical=None,  # 新カテゴリ提案なので既存指定なし
                proposed_change={
                    "kind": "new_canonical",
                    "suggested_id": suggested_id,
                    "display": raw,
                    "aliases": [raw],
                },
                rationale=(
                    f"'{raw}' は {count} 件観測されているが既存 canonical に該当しない。"
                    f"新カテゴリ '{suggested_id}' の作成、または既存 alias 追加を検討。"
                    # 差配判断の材料: レビュー画面だけで「どの既存に寄せるか」を決められるよう
                    # 既存 canonical の一覧を提示する (両論併記だけでは判断できない問題の緩和)。
                    f"既存 canonical: {', '.join(normalizer.sector_canonical_ids)}"
                ),
                confidence="medium" if count >= 5 else "low",
                evidence_count=count,
                evidence_ids=evidence_ids,
            ),
        )
    return proposals


# ---------- 集約 ----------


def generate_all_proposals(
    *,
    lookback_days: int = 30,
    now: datetime | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[GeneratedProposal]:
    """pattern 1/4 を生成 (pattern_5 はリコール層へ一本化済み、2/3/6/7/8 は Phase 3)。"""
    normalizer = load_normalizer()
    proposals: list[GeneratedProposal] = []
    try:
        proposals.extend(
            detect_typo_candidates(
                normalizer=normalizer,
                db_path=db_path,
                lookback_days=lookback_days,
                now=now,
            ),
        )
    except Exception as e:  # noqa: BLE001
        _log.warning("typo_detection_failed", error=str(e))
    try:
        proposals.extend(
            detect_uncategorized_clusters(
                normalizer=normalizer,
                db_path=db_path,
                lookback_days=lookback_days,
                now=now,
            ),
        )
    except Exception as e:  # noqa: BLE001
        _log.warning("uncategorized_cluster_failed", error=str(e))
    _log.info(
        "taxonomy_proposals_generated",
        total=len(proposals),
        tier_1=sum(1 for p in proposals if p.tier == "tier_1_auto"),
        tier_2=sum(1 for p in proposals if p.tier == "tier_2_review"),
        tier_3=sum(1 for p in proposals if p.tier == "tier_3_strategic"),
    )
    return proposals


def proposed_change_to_json(change: dict[str, object]) -> str:
    """ensure_ascii=False で日本語 alias を保持。"""
    return json.dumps(change, ensure_ascii=False, sort_keys=True)
