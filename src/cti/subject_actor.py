"""主題アクター判定 (subject actor attribution) — 言及と主題の分離。

docs/subject_actor_attribution_design.md の実装。契機 = 比較言及 (「Salt Typhoon の
手法とも類似」) が PIR の国別 APT セクション分類にヒットした誤分類 (2026-07-17)。

2 層判定 (新規 LLM 呼出なし):
  1. **タイトル alias 走査 (決定論)**: タイトルは主題の強い近似。org 抑止 + R-A
     フィルタ (非 cyber カテゴリでは機関/請負を主題にしない) は既存規則を再利用。
  2. **LLM 補完 (語彙固定)**: summarizer が既に出力している
     ``routing_flags.primary_actor_id`` を辞書に解決し、**言及集合
     (detected_actor_ids) に含まれる場合のみ**主題とする。①辞書ゲート (確定帰属は
     辞書のみ = actor recall layer と同一原則) + ②言及所属 (記事に名前が出ていない
     アクターを主題にしない) の二重ゲートで、LLM の最悪の失敗は「実在言及の中の
     選び間違い」に限定される。confidence ∈ {high, medium} のみ採用
     (routing_signals の ``use_llm_primary`` と同一規約)。

どちらも不成立なら「主題なし (評価済み)」— 不明を無理に埋めない。
rollback: env ``SUBJECT_ACTOR_LLM=0`` で第 2 層を停止 (title 層のみ)。
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from src.cti.actor_normalizer import ActorAlias, ActorAliasRegistry
from src.logging_config import get_logger

_log = get_logger(__name__)

SUBJECT_ACTOR_LLM_ENV = "SUBJECT_ACTOR_LLM"

# subject_actor_source の値。NULL (未評価/legacy) は DB 上でのみ存在する。
# 証拠等級 (2026-07-26): source (構造的断言) > title (決定論照合) > llm (LLM 読解)。
# SOURCE_FEED = source データが帰属をデータとして主張 (ransomware.live の group、
# MITRE の G-id 等)。最強の証拠 — 名前照合を経ず辞書 id 解決のみで採用する。
SOURCE_FEED = "feed"
SOURCE_TITLE = "title"
SOURCE_LLM = "llm"
SOURCE_NONE = "none"

# LLM 補完を採用する confidence (routing_signals.use_llm_primary と同一規約)
_LLM_USABLE_CONFIDENCE = ("high", "medium")

# R-A: 「脅威活動」とみなす category (organization/contractor アクターの帰属を認める条件)。
# geopolitical / policy / research / other / recap は非 cyber → 機関の単なる言及を弾く。
# (2026-07-17 に persistence.py から verbatim 移設 — 主題判定と帰属フィルタは同じ規則)
_CYBER_CATEGORIES: frozenset[str] = frozenset(
    {
        "apt",
        "apt_leak",
        "malware",
        "incident",
        "incident_breach",
        "breach",
        "advisory",
        "vulnerability",
        "phishing",
    }
)


def _relevant_actors(matched_actors: list[Any], category: str | None) -> list[Any]:
    """R-A (存在≠関連): 検出アクターのうち脅威帰属として妥当なものに絞る。

    group-kind (実 APT = 「活動」) は常に残す。organization/contractor (GRU/FSB/IRGC/
    i-Soon 等の国家機関・請負 = 「言及」) は cyber カテゴリ記事でのみ残し、地政学・軍事・
    政策記事での単なる言及を弾く。ActorAlias.kind の設計意図 (活動 vs 言及) に沿う。
    """
    is_cyber = (category or "").strip().lower() in _CYBER_CATEGORIES
    return [a for a in matched_actors if getattr(a, "kind", "group") == "group" or is_cyber]


@dataclass(frozen=True)
class SubjectActors:
    """1 記事の主題アクター判定結果 (取込時に articles 列へ永続化)。"""

    ids: tuple[str, ...]  # 辞書 id (org 抑止 + R-A 済)。空 = 主題なし
    source: str  # SOURCE_TITLE | SOURCE_LLM | SOURCE_NONE
    confidence: str | None  # source='llm' のときのみ high/medium


def llm_complement_enabled() -> bool:
    """第 2 層 (LLM 補完) の有効判定。既定 ON、SUBJECT_ACTOR_LLM=0 で停止。"""
    raw = os.environ.get(SUBJECT_ACTOR_LLM_ENV, "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _suppress_sponsor_orgs(actors: list[ActorAlias]) -> list[ActorAlias]:
    """グループ特定時は親 organization を主題から抑止 (entity 抽出と同じ二重計上規則)。

    「APT28 (GRU 配下)」のタイトルは APT28 の活動であって GRU 機関の主題ではない。
    """
    org_ids = {a.sponsor_org for a in actors if a.kind == "group" and a.sponsor_org}
    return [a for a in actors if a.id not in org_ids]


def resolve_actor_by_name(name_text: str, registry: ActorAliasRegistry) -> ActorAlias | None:
    """アクター名文字列 (LLM slug 等) を辞書エントリに解決する。

    完全一致 (canonical/alias、小文字) を最優先 — slug は「この名前のアクター」という
    主張であり散文ではないため、ambiguous gate (文脈 cue) を要求しない。
    完全一致しない場合のみ ``registry.find`` の部分形照合 (例: "lazarus" ⊆
    "Lazarus Group") に fallback する。
    """
    key = (name_text or "").strip().lower()
    if not key:
        return None
    for actor in registry.actors:
        if any(n.lower() == key for n in actor.all_names):
            return actor
    return registry.find(name_text)


def determine_subject_actors(
    *,
    titles: Iterable[str],
    detected_actor_ids: Iterable[str],
    llm_primary_actor_id: str,
    llm_confidence: str,
    category: str | None,
    registry: ActorAliasRegistry,
    asserted_actor_ids: Iterable[str] = (),
) -> SubjectActors:
    """記事の主題アクターを証拠等級順に判定する (モジュール docstring 参照)。

    **全取込経路が通る単一の主題判定点** (R1)。source 断言 (層 0) → title (層 1) →
    LLM (層 2) の順に評価する。

    ``asserted_actor_ids`` = source データが構造的に主張する帰属 (ransomware.live の
    被害者掲載グループ等)。辞書 id に解決できるものだけを最強証拠として採用する
    (未解決の断言は呼び出し側が actor_provisional として人承認へ回す)。
    ``titles`` は表示タイトル + 原題。``detected_actor_ids`` は言及集合 (R-A 済)。
    """
    # ---- 第 0 層: source 構造的断言 (最強証拠。名前照合を経ず辞書 id 解決のみ) ----
    asserted = sorted(
        {
            registry.resolve_actor_id(a.strip())
            for a in asserted_actor_ids
            if a.strip() and registry.by_id(registry.resolve_actor_id(a.strip())) is not None
        }
    )
    if asserted:
        return SubjectActors(ids=tuple(asserted), source=SOURCE_FEED, confidence=None)

    # ---- 第 1 層: タイトル alias 走査 (決定論) ----
    hits: list[ActorAlias] = []
    seen: set[str] = set()
    for t in titles:
        if not t:
            continue
        for actor in registry.find_all(t):
            if actor.id not in seen:
                seen.add(actor.id)
                hits.append(actor)
    hits = _suppress_sponsor_orgs(_relevant_actors(hits, category))
    if hits:
        return SubjectActors(
            ids=tuple(sorted(a.id for a in hits)),
            source=SOURCE_TITLE,
            confidence=None,
        )

    # ---- 第 2 層: LLM 補完 (語彙固定の二重ゲート) ----
    detected = {str(i).strip() for i in detected_actor_ids if str(i).strip()}
    pid = (llm_primary_actor_id or "").strip()
    if pid and detected and llm_complement_enabled() and llm_confidence in _LLM_USABLE_CONFIDENCE:
        # slug は空白→ハイフン正規化済み (llm_routing_flags._normalize_slug) — 逆変換して解決。
        # focused 分類器は解決済み id を渡すため by_id fallback も試す (id/name 形状に非依存)。
        resolved = resolve_actor_by_name(
            pid.replace("-", " ").replace("_", " "), registry
        ) or registry.by_id(pid)
        if resolved is not None and resolved.id in detected:
            return SubjectActors(ids=(resolved.id,), source=SOURCE_LLM, confidence=llm_confidence)
        if resolved is not None:
            # 辞書解決はできたが記事の言及集合に無い = 帰属の捏造リスク → 不採用 (観測ログのみ)
            _log.info(
                "subject_actor_llm_not_in_mentions",
                primary_actor_id=pid,
                resolved_id=resolved.id,
            )

    return SubjectActors(ids=(), source=SOURCE_NONE, confidence=None)
