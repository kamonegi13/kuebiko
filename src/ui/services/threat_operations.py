"""Phase Threats: Actor 中心の Threat Operations data layer。

Threat Operations は単一 interactive page で actor を主軸に状況把握を行う。
本 service は actor activity 集計、family 集計、共起 actor、discovery 信号
(新規 actor / spike / waking-up actor) を計算する。

actor 抽出:
    各 article について以下から actor を推定:
      (a) LLM 由来 ``primary_actor_id`` (将来) — まだ pipeline で永続化していない
      (b) ``actor_aliases.yaml`` の aliases を title + summary に対して走査
      (c) (b) で 0-match なら ``unknown`` bucket に投入

actor 抽出は title + summary を対象とし、word-boundary を緩く考える
(Japanese は word boundary 概念がないため substring match)。
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.cti.actor_normalizer import (
    ActorAlias,
    _alias_pattern,
    load_actor_aliases,
    load_actor_families,
    resolve_ambiguous_cues,
)
from src.cti.japan_relevance import JAPAN_TARGETED_SQL_FRAGMENT, is_japan_targeted_row
from src.cti.subject_gate import passes_subject_gate
from src.logging_config import get_logger

_log = get_logger(__name__)

DEFAULT_DB_PATH = Path("data/run_history.db")

# 1 sparkline に使う bucket 数 (lookback_days を等分)
SPARKLINE_BUCKETS = 12

# discovery 判定パラメータ
SPIKE_RATIO_THRESHOLD = 2.0  # 直近 N 日 vs prior N 日で >= 2x
WAKING_SILENT_DAYS = 30  # silent >= N 日後の activation を waking 判定

_CVE_RE = re.compile(r"CVE-\d{4}-\d{4,7}", re.IGNORECASE)
# MITRE ATT&CK technique IDs (T1234 or T1234.001)
_TTP_RE = re.compile(r"\bT\d{4}(?:\.\d{3})?\b")


@dataclass(frozen=True)
class ActorActivity:
    """1 actor の活動 summary。"""

    actor_id: str
    canonical: str
    aliases: tuple[str, ...]
    nation: str
    family: str
    mitre_group: str
    sponsor: str
    description: str

    total_articles: int
    daily_counts: tuple[int, ...]  # SPARKLINE_BUCKETS 個
    sparkline: str
    last_seen_iso: str

    top_sectors: tuple[tuple[str, int], ...]
    top_countries: tuple[tuple[str, int], ...]
    top_cves: tuple[str, ...]
    top_ttps: tuple[str, ...]
    # Phase Diamond: Capability 軸の構造化集計 (summarizer prompt の新フィールド由来)
    top_malware_families: tuple[tuple[str, int], ...] = ()
    top_tools: tuple[tuple[str, int], ...] = ()
    # Phase Diamond: Infrastructure 軸の IoC 集計 (type ごとに上位 N 件)
    top_iocs_ip: tuple[tuple[str, int], ...] = ()
    top_iocs_domain: tuple[tuple[str, int], ...] = ()
    top_iocs_hash: tuple[tuple[str, int], ...] = ()  # sha256/sha1/md5 合算
    top_iocs_url: tuple[tuple[str, int], ...] = ()
    # Phase Diamond-Axes: socio-political 軸 = この actor (Adversary) が victim に対して
    # 持つ意図の分布 (intent canonical, 件数)。actor 単位の Adversary→Victim 軸の集約。
    top_intents: tuple[tuple[str, int], ...] = ()
    # Actors Stage 5: 実体種別。organization/contractor は UI で「活動」でなく「言及」表記
    kind: str = "group"
    sponsor_org: str | None = None

    is_new: bool = False  # baseline 期間に登場なし、current 期間で登場
    is_spike: bool = False  # current/baseline >= threshold
    spike_ratio: float = 0.0
    is_quiet_waking: bool = False  # 30+日 silent → 直近 active

    japan_targeted_count: int = 0  # 該当期間内の japan_watch 投稿件数

    # ミッション脅威評価用の被害観測スライス: (victim_country_iso 大文字, sector_canonical
    # または "", 件数)。top_countries と違い **切り詰めない** (JP が top5 圏外でも落ちない)。
    # 純粋な観測集計 — 同盟/敵性の解釈は評価側 (actor_threat.py) が doctrine で行う。
    victim_country_sectors: tuple[tuple[str, str, int], ...] = ()

    # ③ PIR 連携: この actor が該当する enabled PIR の id 群 (strong_signals.actors を
    # 辞書 alias で解決して付与)。knowledge(辞書)×observation(本ページ) と独立に、
    # user の PIR が「どの actor を重視するか」を UI で優先表示/フィルタするための signal。
    matched_pir_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ActorRelations:
    """actor 関係 (family / 共起 / campaign)。"""

    family_members: tuple[tuple[str, int], ...]  # (actor_id, total_articles)
    cooccur_actors: tuple[tuple[str, int], ...]  # (actor_id, cooccur_count)
    related_campaigns: tuple[str, ...]  # 簡易 (将来 campaign yaml で拡張)


@dataclass(frozen=True)
class ActorArticleRef:
    """actor 詳細 panel に表示する 1 article。"""

    article_id: str
    title: str
    url: str
    feed_title: str
    importance: str
    created_at: str
    posted_channel: str
    discord_message_id: str | None = None
    discord_channel_id: str | None = None
    # Phase Diamond-Axes: この incident の socio-political 軸 (intent) と technical 軸 narrative
    socio_political_intent: str | None = None
    technical_axis_summary: str | None = None


@dataclass(frozen=True)
class ActorDetail:
    """1 actor の詳細 page payload。"""

    activity: ActorActivity
    relations: ActorRelations
    recent_articles: tuple[ActorArticleRef, ...]
    # timeline (date_iso, count) — 日次 series for Plotly chart (報道時刻 = created_at)
    timeline_daily: tuple[tuple[str, int], ...]
    # 時間軸トグル (2026-06-27): 発生時刻 (event_date) 系列 + カバレッジ。報道時刻と切替え、
    # アクターの「実際に活動した年代」(作戦史) を dated subset で復元する。過去事象に偏るので
    # 1y+ 窓で効く。coverage=(dated, total, event_in_window)。
    timeline_event: tuple[tuple[str, int], ...] = ()
    timeline_coverage: tuple[int, int, int] = (0, 0, 0)
    # 既知 TTP (Actor 辞書の mitre_ttps、knowledge 側)。観測 top_ttps との
    # 突き合わせで「MITRE 既知外の観測 TTP」をハイライトするために返す
    known_ttps: tuple[str, ...] = ()
    # 配下グループの活動 rollup (kind=organization のみ)。
    # (actor_id, canonical, total_articles, sparkline)。機関単位で配下の動きを俯瞰。
    child_groups: tuple[tuple[str, str, int, str], ...] = ()


@dataclass(frozen=True)
class DiscoveryPanel:
    """Discovery panel (新規 / spike / waking-up actor)。"""

    new_actors: tuple[ActorActivity, ...]
    spiking_actors: tuple[ActorActivity, ...]
    waking_actors: tuple[ActorActivity, ...]
    unknown_bucket_count: int  # 未登録 actor 言及記事数


@dataclass(frozen=True)
class ThreatOperationsSnapshot:
    """page 全体 payload。"""

    actors: tuple[ActorActivity, ...]
    families: dict[str, dict[str, str]]
    nations: tuple[str, ...]
    discovery: DiscoveryPanel
    lookback_days: int
    filters: dict[str, str] = field(default_factory=dict)


# ====================== Helpers ======================


def _connect(db_path: Path) -> Any:
    """Phase Y-3: DATABASE_URL set なら PG、 未設定なら SQLite。"""
    from src.storage.db_backend import connect as backend_connect

    con = backend_connect(db_path)
    if hasattr(con, "row_factory"):
        con.row_factory = sqlite3.Row
    return con


def _make_sparkline(counts: tuple[int, ...]) -> str:
    """日次 count から SVG sparkline (HTML string) を返す。

    Modern UI 向けに smooth area chart で表示する。
    幅 70px / 高さ 18px、accent 色のラインと薄い fill area。
    """
    if not counts:
        return ""
    if all(c == 0 for c in counts):
        # 全 0 のときも視覚的に空であることを示す flat line
        return (
            '<svg class="sparkline-svg" width="70" height="18" viewBox="0 0 70 18" '
            'aria-hidden="true">'
            '<line x1="0" y1="14" x2="70" y2="14" stroke="rgba(255,255,255,0.10)" '
            'stroke-width="1"/></svg>'
        )
    mx = max(counts)
    n = len(counts)
    if n < 2:
        return ""
    # path 計算 (height=18, top padding=2)
    width = 70
    h = 14  # 描画域 height
    top_pad = 2
    step = width / (n - 1)
    points: list[tuple[float, float]] = []
    for i, c in enumerate(counts):
        x = i * step
        y = top_pad + h - (c / mx * h if mx else 0)
        points.append((x, y))
    # area path (closed for fill)
    area = (
        f"M{points[0][0]:.1f},{top_pad + h} "
        + " ".join(f"L{x:.1f},{y:.1f}" for x, y in points)
        + f" L{points[-1][0]:.1f},{top_pad + h} Z"
    )
    # line path
    line = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in points)
    return (
        f'<svg class="sparkline-svg" width="{width}" height="18" '
        f'viewBox="0 0 {width} 18" aria-hidden="true">'
        f'<path class="area" d="{area}"/>'
        f'<path d="{line}"/>'
        f"</svg>"
    )


@dataclass(frozen=True)
class _ActorMatcher:
    """actor 名 → id の lookup。substring 事前フィルタ + 境界検証で照合する。"""

    name_to_ids: dict[str, frozenset[str]]
    # kind=group の actor id → 親 organization id (二重計上抑止用、Actors Stage 5)
    group_to_org: dict[str, str]
    # 曖昧アクター (一般語と衝突する名前) の actor id → 文脈 cue。名前一致に加えて
    # cue 共起を要求する (actor_normalizer の ambiguous gate と同じ規則。従来この
    # ページの抽出だけ gate を通っておらず、一般語アクターが過剰計上され得た)
    ambiguous_cues: dict[str, tuple[str, ...]]


def _build_actor_lookup(
    registry_actors: tuple[ActorAlias, ...],
) -> _ActorMatcher:
    """actor 名 matching 用の lookup を構築する。

    照合は ``_extract_actors_from_text`` で「substring 事前フィルタ → 境界検証」の
    2 段で行う。境界規則は ``actor_normalizer._alias_pattern`` と同一 (ASCII 英数の
    直前後のみ境界) で "Grumman"→GRU / "MSSP"→MSS / "APT38"→APT3 等の誤帰属を防ぐ。

    Perf 注記: 以前は全名を 1 本の巨大 alternation regex に束ねていたが、587 名 ×
    lookaround を長文記事に finditer すると 0.9ms/件と遅く、snapshot/detail が
    数秒〜十数秒に膨らんでいた。substring の ``in`` (C 実装) で 99% の名前を弾き、
    一致した数件だけ境界 regex を当てる方式で 0.14ms/件に短縮 (実測 6.5x)。
    """
    name_to_ids: dict[str, set[str]] = defaultdict(set)
    group_to_org: dict[str, str] = {}
    ambiguous_cues: dict[str, tuple[str, ...]] = {}
    for actor in registry_actors:
        if actor.kind == "group" and actor.sponsor_org:
            group_to_org[actor.id] = actor.sponsor_org
        if actor.ambiguous:
            ambiguous_cues[actor.id] = resolve_ambiguous_cues(actor)
        for name in actor.all_names:
            # 2 文字以下は boundary 付きでも generic すぎるため除外 (旧実装から踏襲)
            if len(name) < 3:
                continue
            name_to_ids[name.lower()].add(actor.id)
    return _ActorMatcher(
        name_to_ids={n: frozenset(ids) for n, ids in name_to_ids.items()},
        group_to_org=group_to_org,
        ambiguous_cues=ambiguous_cues,
    )


def _load_enabled_pir_priorities() -> list[Any]:
    """enabled PIR list を取得 (PIR システム障害時は空 = 既存挙動)。"""
    try:
        from src.pir.integration import get_pir_config

        return [p for p in get_pir_config().priorities if p.enabled]
    except Exception as e:  # noqa: BLE001 — PIR 障害で Threats page を壊さない
        _log.warning("threat_pir_index_load_failed", error=str(e))
        return []


def _build_pir_actor_index(
    lookup: _ActorMatcher,
    pir_priorities: list[Any],
) -> dict[str, frozenset[str]]:
    """enabled PIR の ``strong_signals.actors`` を辞書 alias で actor_id に解決し、
    ``actor_id -> {pir_id}`` を返す (③ の PIR→辞書ID 解決リンク)。

    PIR の actors は自由文字列 (例: pir.yaml の "Lazarus") で、辞書 canonical
    (例: "Lazarus Group") と完全一致しないことがある。そのため **双方向の
    word-boundary 一致** で解決する: PIR 名が辞書名に語として含まれる
    (Lazarus ⊆ Lazarus Group)、または辞書名が PIR 名に語として含まれる、
    のいずれかで該当とみなす。境界規則は ``_alias_pattern`` (本ページの actor
    抽出と同一) で "Grumman"→GRU のような誤帰属を防ぐ。
    解決できない名前 (辞書外) は静かに skip。
    """
    index: dict[str, set[str]] = defaultdict(set)
    items = list(lookup.name_to_ids.items())  # (name_low, ids)、name は 3 文字以上
    for pir in pir_priorities:
        pir_id = getattr(pir, "id", None)
        signals = getattr(pir, "strong_signals", None)
        actors = list(getattr(signals, "actors", []) or [])
        if not pir_id or not actors:
            continue
        for raw in actors:
            pir_low = str(raw).lower().strip()
            if len(pir_low) < 3:
                continue
            pir_pat = _alias_pattern(pir_low)
            for name_low, ids in items:
                hit = (pir_low in name_low and bool(pir_pat.search(name_low))) or (
                    name_low in pir_low and bool(_alias_pattern(name_low).search(pir_low))
                )
                if hit:
                    for aid in ids:
                        index[aid].add(pir_id)
    return {aid: frozenset(pids) for aid, pids in index.items()}


def _extract_actors_from_text(
    text: str,
    lookup: _ActorMatcher,
) -> set[str]:
    """text 内に出現する actor_id 集合を返す (word-boundary 一致、大文字小文字区別なし)。

    照合は 2 段: (1) ``name in low`` の C-level substring 事前フィルタで大多数の名前を
    高速に弾き、(2) 一致した名前だけ ``_alias_pattern`` (lru_cache 済) の境界検証を当てる。

    二重計上抑止 (Actors Stage 5): 同一記事でグループ (kind=group) が特定できた場合、
    そのグループの親 organization への帰属は抑止する。「APT28 (GRU)」の記事は APT28 の
    活動であって GRU 機関への独立した言及ではないため。グループ名なしで機関のみが
    言及される記事では organization が残り、機関 watch の受け皿として機能する。
    """
    if not text:
        return set()
    low = text.lower()
    out: set[str] = set()
    for name_low, ids in lookup.name_to_ids.items():
        if name_low in low and _alias_pattern(name_low).search(low):
            out |= ids
    # 曖昧アクター gate: 名前一致だけでは確定させず、文脈 cue の共起を要求
    # (actor_normalizer._matched_name と同じ規則を本ページの抽出にも適用)
    for aid in [a for a in out if a in lookup.ambiguous_cues]:
        cues = lookup.ambiguous_cues[aid]
        if not any(cue.lower() in low for cue in cues):
            out.discard(aid)
    sponsored_orgs = {org for aid in out if (org := lookup.group_to_org.get(aid)) is not None}
    return out - sponsored_orgs


def _gated_actor_ids(row: Any, lookup: _ActorMatcher) -> set[str]:
    """記事の title+summary から mention actor を抽出し **subject-gate** を適用する。

    脅威スナップショットは従来 mention をそのまま「活動」に計上しており、ランサム列挙
    ダイジェスト・比較言及・報道機関言及が過剰計上されていた (entity 棚卸し 2026-07-29、
    docs/entity_pipeline_inventory.md §6、下流5箇所 subject-gating の未実装分)。

    subject-gate = 評価済み行 (subject_actor_source 非空) は主題 actor のみ、legacy 行
    (07-17 主題層稼働前) は mention をそのまま通す。SQL 消費者の ``subject_gate_clause`` と
    同一意味論 (``passes_subject_gate``)。row には subject_actor_ids/subject_actor_source が
    含まれている前提 (各 SELECT で取得)。
    """
    text = f"{row['title'] or ''} {row['summary'] or ''}"
    found = _extract_actors_from_text(text, lookup)
    if not found:
        return found
    ids_csv = row["subject_actor_ids"]
    source = row["subject_actor_source"]
    return {
        aid
        for aid in found
        if passes_subject_gate(mention_id=aid, subject_ids_csv=ids_csv, subject_source=source)
    }


def _bucketize_daily(
    rows: list[sqlite3.Row],
    *,
    lookback_days: int,
    now: datetime,
    buckets: int = SPARKLINE_BUCKETS,
) -> tuple[int, ...]:
    """rows を等分の bucket に分けた count 配列を返す。"""
    if not rows or buckets <= 0:
        return tuple([0] * buckets)
    earliest = now - timedelta(days=lookback_days)
    span = lookback_days * 86400  # seconds
    if span <= 0:
        return tuple([0] * buckets)
    bucket_size = span / buckets
    counts = [0] * buckets
    for r in rows:
        try:
            dt = datetime.fromisoformat(str(r["created_at"]).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        delta_s = (dt - earliest).total_seconds()
        if delta_s < 0 or delta_s > span:
            continue
        idx = min(buckets - 1, int(delta_s / bucket_size))
        counts[idx] += 1
    return tuple(counts)


def _daily_series(
    rows: list[sqlite3.Row],
    *,
    lookback_days: int,
    now: datetime,
) -> tuple[tuple[str, int], ...]:
    """日次 (date_iso, count) series。Plotly chart 用、JST グルーピング。

    Phase Diamond fix: UTC で grouping すると深夜帯 article が翌日扱いとなり
    JST 表示と 1 日ずれる事象が発生する。Asia/Tokyo zoneinfo に変換してから date 抽出。
    """
    from zoneinfo import ZoneInfo

    jst = ZoneInfo("Asia/Tokyo")
    counts: dict[str, int] = {}
    for r in rows:
        try:
            dt = datetime.fromisoformat(str(r["created_at"]).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        d = dt.astimezone(jst).strftime("%Y-%m-%d")
        counts[d] = counts.get(d, 0) + 1
    out: list[tuple[str, int]] = []
    earliest = now.astimezone(jst) - timedelta(days=lookback_days)
    for i in range(lookback_days + 1):
        d = (earliest + timedelta(days=i)).strftime("%Y-%m-%d")
        out.append((d, counts.get(d, 0)))
    return tuple(out)


def _event_daily_series(
    rows: list[sqlite3.Row],
    *,
    lookback_days: int,
    now: datetime,
) -> tuple[tuple[tuple[str, int], ...], tuple[int, int, int]]:
    """発生時刻 (event_date) 日次 series + カバレッジ (dated, total, event_in_window)。

    時間軸トグルの発生時刻側。event_date 無し (速報の大半) は dated に数えず系列にも出ない
    =「不明」(≠無し)。窓内に着地した発生件数を event_in_window で正直に返す。
    """
    from zoneinfo import ZoneInfo

    jst = ZoneInfo("Asia/Tokyo")
    counts: dict[str, int] = {}
    dated = 0
    for r in rows:
        try:
            ev = r["event_date"]
        except (KeyError, IndexError):
            ev = None
        if not ev:
            continue
        dated += 1
        d = str(ev)[:10]
        counts[d] = counts.get(d, 0) + 1
    out: list[tuple[str, int]] = []
    earliest = now.astimezone(jst) - timedelta(days=lookback_days)
    in_window = 0
    for i in range(lookback_days + 1):
        d = (earliest + timedelta(days=i)).strftime("%Y-%m-%d")
        c = counts.get(d, 0)
        in_window += c
        out.append((d, c))
    return tuple(out), (dated, len(rows), in_window)


# ====================== Public API ======================


# Phase Diamond P4: 検索 prefix syntax (cve:/ioc:/ttp:/malware:/tool:/sector:/country:)
_SEARCH_PREFIXES = ("cve", "ioc", "ttp", "malware", "tool", "sector", "country")


def _parse_search_prefix(query: str) -> tuple[str | None, str]:
    """検索 query を ``prefix:value`` に分解。prefix 不一致時は (None, query)。"""
    if ":" not in query:
        return None, query
    prefix, _, value = query.partition(":")
    p = prefix.strip().lower()
    v = value.strip()
    if p in _SEARCH_PREFIXES and v:
        return p, v
    return None, query


def _entity_search_match(activity: ActorActivity, prefix: str, value: str) -> bool:
    """actor が指定 entity を持つかを判定。case-insensitive 部分一致。

    prefix と activity フィールドの対応:
        cve     → top_cves
        ttp     → top_ttps
        malware → top_malware_families
        tool    → top_tools
        ioc     → top_iocs_ip + ioc_domain + ioc_hash + ioc_url (全 IoC 種を横断)
        sector  → top_sectors
        country → top_countries
    """
    v_lower = value.lower()
    if prefix == "cve":
        return any(v_lower in c.lower() for c in activity.top_cves)
    if prefix == "ttp":
        return any(v_lower in t.lower() for t in activity.top_ttps)
    if prefix == "malware":
        return any(v_lower in m.lower() for m, _ in activity.top_malware_families)
    if prefix == "tool":
        return any(v_lower in t.lower() for t, _ in activity.top_tools)
    if prefix == "ioc":
        for src in (
            activity.top_iocs_ip,
            activity.top_iocs_domain,
            activity.top_iocs_hash,
            activity.top_iocs_url,
        ):
            if any(v_lower in v.lower() for v, _ in src):
                return True
        return False
    if prefix == "sector":
        return any(v_lower in s.lower() for s, _ in activity.top_sectors)
    if prefix == "country":
        return any(v_lower in c.lower() for c, _ in activity.top_countries)
    return False


@dataclass(frozen=True)
class _SnapshotCore:
    """フィルタ非依存の重い集計結果 (TTL cache 単位)。

    activities は dormant (観測 0 件の辞書 group) 込み。nation/family/search/pir/
    include_dormant はこの結果への軽量 post-filter で表現できるため含めない。
    japan_targeted_only / high_importance_only は SQL の WHERE を変え集計値そのものが
    変わるため cache key の一部としてここより上流で分岐する。
    """

    activities: tuple[ActorActivity, ...]
    families: dict[str, dict[str, str]]
    unknown_bucket_count: int


# 記事全走査 + actor 抽出は 30-90d 窓で数秒かかるため、production (既定 DB) では
# TTL cache する。収集は毎時 interval なので 10 分は十分新しい (actor_threat.py の
# 評価 cache と同じ根拠)。tests は tmp db_path / now 指定で cache を素通りする。
_SNAPSHOT_CACHE_TTL_SECONDS = 600
_snapshot_core_cache: dict[tuple[int, bool, bool], tuple[float, _SnapshotCore]] = {}
_snapshot_cache_lock = threading.Lock()


def invalidate_threat_snapshot_cache() -> None:
    """アクター辞書の編集直後などに TTL を待たず snapshot cache を破棄する。"""
    with _snapshot_cache_lock:
        _snapshot_core_cache.clear()


def _compute_snapshot_core(
    *,
    lookback_days: int,
    japan_targeted_only: bool,
    high_importance_only: bool,
    now: datetime,
    db_path: Path,
) -> _SnapshotCore:
    """記事全走査 + actor 抽出 + 集計 (重い部分の本体)。dormant group 込みで返す。"""
    base = now
    earliest = base - timedelta(days=lookback_days)

    registry = load_actor_aliases()
    families = load_actor_families()
    # D1 (2026-07-27): 同盟国の報告/防御機関 (NSA 等) を脅威アクター snapshot から除外
    # (mention 混入で報告機関が「活動中の脅威アクター」に混ざる問題の解消)。
    from src.cti.actor_roles import reporter_org_actor_ids

    _reporter_ids = reporter_org_actor_ids()
    _snapshot_actors = tuple(a for a in registry.actors if a.id not in _reporter_ids)
    lookup = _build_actor_lookup(_snapshot_actors)
    actor_by_id: dict[str, ActorAlias] = {a.id: a for a in registry.actors}
    pir_actor_index = _build_pir_actor_index(lookup, _load_enabled_pir_priorities())

    # 1 query で必要な期間 article をすべて取得 (article 件数は ~1000s/30d なので OK)
    sql_clauses = [
        "status='posted'",
        "datetime(created_at) >= datetime(?)",
    ]
    params: list[object] = [earliest.isoformat()]
    if high_importance_only:
        sql_clauses.append("importance='high'")
    if japan_targeted_only:
        # H4 (japan_relevance SSoT): channel のみの旧方言は victim=JP を落としていた
        sql_clauses.append(JAPAN_TARGETED_SQL_FRAGMENT)
    where = " AND ".join(sql_clauses)
    with _connect(db_path) as con:
        rows = con.execute(
            f"""
            SELECT article_id, title, summary, feed_title, importance,
                   posted_channel, created_at, victim_sector_canonical,
                   victim_country_iso, socio_political_intent,
                   subject_actor_ids, subject_actor_source
              FROM articles
             WHERE {where}
             ORDER BY datetime(created_at) ASC
            """,  # noqa: S608
            params,
        ).fetchall()

    # baseline 期間 (current lookback 直前の同等期間) — spike 判定用
    baseline_until = earliest
    baseline_since = baseline_until - timedelta(days=lookback_days)
    with _connect(db_path) as con:
        baseline_rows = con.execute(
            """
            SELECT article_id, title, summary,
                   subject_actor_ids, subject_actor_source
              FROM articles
             WHERE status='posted'
               AND datetime(created_at) >= datetime(?)
               AND datetime(created_at) < datetime(?)
            """,
            (baseline_since.isoformat(), baseline_until.isoformat()),
        ).fetchall()

    # waking 判定用 (lookback の直前 30 日に活動があるか)
    silent_until = earliest
    silent_since = silent_until - timedelta(days=WAKING_SILENT_DAYS)
    with _connect(db_path) as con:
        silent_period_rows = con.execute(
            """
            SELECT article_id, title, summary,
                   subject_actor_ids, subject_actor_source
              FROM articles
             WHERE status='posted'
               AND datetime(created_at) >= datetime(?)
               AND datetime(created_at) < datetime(?)
            """,
            (silent_since.isoformat(), silent_until.isoformat()),
        ).fetchall()

    # actor_id ごとに rows 集計 (共起は fetch_actor_detail が自前計算するためここでは不要)
    actor_rows: dict[str, list[sqlite3.Row]] = defaultdict(list)
    unknown_count = 0
    for r in rows:
        found = _gated_actor_ids(r, lookup)
        if not found:
            unknown_count += 1
            continue
        for aid in found:
            actor_rows[aid].append(r)

    # baseline での actor 出現 (spike 判定の分母。gate は現行と同一条件で揃える)
    baseline_seen: Counter[str] = Counter()
    for r in baseline_rows:
        for aid in _gated_actor_ids(r, lookup):
            baseline_seen[aid] += 1

    # silent period actor 出現
    silent_period_seen: set[str] = set()
    for r in silent_period_rows:
        silent_period_seen.update(_gated_actor_ids(r, lookup))

    # actor activities
    activities: list[ActorActivity] = []

    # Phase Diamond: article_entities テーブルから一括取得 (regex fallback と併用)。
    # 取得失敗時は in-memory regex のみで動く (後方互換)。
    all_article_ids = [str(r["article_id"]) for ar in actor_rows.values() for r in ar]
    entity_counts_by_article: dict[str, dict[str, dict[str, int]]] = {}
    try:
        from src.storage.run_history import RunHistoryRepository

        _repo = RunHistoryRepository()
        # 1 article ごとの entities ではなく、全 article まとめて取得して memory で振り分け
        if all_article_ids:
            agg = _repo.count_entities_for_articles(all_article_ids)
            # agg: {entity_type: {value: count}} — でも actor 別に集計したいので個別に取り直す
            # 軽量化: article ごとの entity を 1 度 fetch (N+1 を避けるため 1 query で)
            with _repo._connect() as _conn:  # noqa: SLF001
                ph = ",".join("?" * len(all_article_ids))
                rows_e = _conn.execute(
                    f"SELECT article_id, entity_type, value FROM article_entities "
                    f"WHERE article_id IN ({ph})",
                    all_article_ids,
                ).fetchall()
            for row in rows_e:
                aid_e = str(row["article_id"])
                et = str(row["entity_type"])
                val = str(row["value"])
                entity_counts_by_article.setdefault(aid_e, {}).setdefault(et, {})[val] = (
                    entity_counts_by_article.get(aid_e, {}).get(et, {}).get(val, 0) + 1
                )
            _ = agg  # silence linter
    except Exception:  # noqa: BLE001
        pass  # 失敗時は regex のみで動く

    for aid, ar in actor_rows.items():
        meta = actor_by_id.get(aid)
        if meta is None:
            continue
        total = len(ar)
        bs = baseline_seen.get(aid, 0)
        spike_ratio = float("inf") if bs == 0 and total > 0 else (total / bs if bs else 0.0)
        is_new = bs == 0 and total > 0
        is_spike = is_new or spike_ratio >= SPIKE_RATIO_THRESHOLD
        is_quiet_waking = (aid not in silent_period_seen) and bs == 0 and total > 0
        sectors_c: Counter[str] = Counter()
        countries_c: Counter[str] = Counter()
        country_sector_c: Counter[tuple[str, str]] = Counter()
        intents_c: Counter[str] = Counter()
        cves_c: Counter[str] = Counter()
        ttps_c: Counter[str] = Counter()
        malware_c: Counter[str] = Counter()
        tools_c: Counter[str] = Counter()
        ip_c: Counter[str] = Counter()
        dom_c: Counter[str] = Counter()
        hash_c: Counter[str] = Counter()
        url_c: Counter[str] = Counter()
        japan_n = 0
        last_seen = ""
        for r in ar:
            if r["victim_sector_canonical"]:
                sectors_c[str(r["victim_sector_canonical"])] += 1
            if r["victim_country_iso"]:
                countries_c[str(r["victim_country_iso"])] += 1
                country_sector_c[
                    (
                        str(r["victim_country_iso"]).upper(),
                        str(r["victim_sector_canonical"] or ""),
                    )
                ] += 1
            _intent = r["socio_political_intent"]
            if _intent and str(_intent) != "unknown":
                intents_c[str(_intent)] += 1
            text = f"{r['title'] or ''} {r['summary'] or ''}"
            # 構造化 entities (article_entities テーブル由来) を優先
            ents = entity_counts_by_article.get(str(r["article_id"]), {})
            for v in ents.get("cve", {}):
                cves_c[v.upper()] += 1
            for v in ents.get("ttp", {}):
                ttps_c[v.upper()] += 1
            for v in ents.get("malware_family", {}):
                malware_c[v] += 1
            for v in ents.get("tool", {}):
                tools_c[v] += 1
            for v in ents.get("ioc_ip", {}):
                ip_c[v] += 1
            for v in ents.get("ioc_domain", {}):
                dom_c[v] += 1
            for v in ents.get("ioc_sha256", {}):
                hash_c[v] += 1
            for v in ents.get("ioc_sha1", {}):
                hash_c[v] += 1
            for v in ents.get("ioc_md5", {}):
                hash_c[v] += 1
            for v in ents.get("ioc_url", {}):
                url_c[v] += 1
            # 後方互換: structured entities が無い article は title+summary regex でも拾う
            if not ents.get("cve") and not ents.get("ttp"):
                for m in _CVE_RE.finditer(text):
                    cves_c[m.group(0).upper()] += 1
                for m in _TTP_RE.finditer(text):
                    ttps_c[m.group(0).upper()] += 1
            if is_japan_targeted_row(r["victim_country_iso"], r["posted_channel"]):
                japan_n += 1
            if r["created_at"] and (not last_seen or str(r["created_at"]) > last_seen):
                last_seen = str(r["created_at"])
        counts = _bucketize_daily(ar, lookback_days=lookback_days, now=base)
        activities.append(
            ActorActivity(
                actor_id=aid,
                canonical=meta.canonical,
                aliases=meta.aliases,
                nation=meta.nation or "",
                family=meta.family or "",
                mitre_group=meta.mitre_group or "",
                sponsor=meta.sponsor or "",
                kind=meta.kind,
                sponsor_org=meta.sponsor_org,
                description=meta.description,
                total_articles=total,
                daily_counts=counts,
                sparkline=_make_sparkline(counts),
                last_seen_iso=last_seen,
                top_sectors=tuple(sectors_c.most_common(5)),
                top_countries=tuple(countries_c.most_common(5)),
                top_cves=tuple(c for c, _ in cves_c.most_common(8)),
                top_ttps=tuple(t for t, _ in ttps_c.most_common(8)),
                top_malware_families=tuple(malware_c.most_common(8)),
                top_tools=tuple(tools_c.most_common(8)),
                top_iocs_ip=tuple(ip_c.most_common(8)),
                top_iocs_domain=tuple(dom_c.most_common(8)),
                top_iocs_hash=tuple(hash_c.most_common(8)),
                top_iocs_url=tuple(url_c.most_common(8)),
                top_intents=tuple(intents_c.most_common()),
                is_new=is_new,
                is_spike=is_spike,
                spike_ratio=spike_ratio,
                is_quiet_waking=is_quiet_waking,
                japan_targeted_count=japan_n,
                victim_country_sectors=tuple(
                    (c, s, n) for (c, s), n in country_sector_c.most_common()
                ),
                matched_pir_ids=tuple(sorted(pir_actor_index.get(aid, frozenset()))),
            ),
        )

    # 休眠アクター (期間内観測 0 件) は常に含めて cache し、include_dormant=False 側で
    # post-filter する (kind=group のみ。organization/contractor は「言及の受け皿」で
    # あり観測 0 の行を出す意味がない — 脅威評価も非対象)
    zero_counts = tuple([0] * SPARKLINE_BUCKETS)
    zero_spark = _make_sparkline(zero_counts)
    for meta in registry.actors:
        if meta.kind != "group" or meta.id in actor_rows:
            continue
        activities.append(
            ActorActivity(
                actor_id=meta.id,
                canonical=meta.canonical,
                aliases=meta.aliases,
                nation=meta.nation or "",
                family=meta.family or "",
                mitre_group=meta.mitre_group or "",
                sponsor=meta.sponsor or "",
                kind=meta.kind,
                sponsor_org=meta.sponsor_org,
                description=meta.description,
                total_articles=0,
                daily_counts=zero_counts,
                sparkline=zero_spark,
                last_seen_iso="",
                top_sectors=(),
                top_countries=(),
                top_cves=(),
                top_ttps=(),
                matched_pir_ids=tuple(sorted(pir_actor_index.get(meta.id, frozenset()))),
            ),
        )

    return _SnapshotCore(
        activities=tuple(activities),
        families=families,
        unknown_bucket_count=unknown_count,
    )


def fetch_threat_operations_snapshot(
    *,
    lookback_days: int = 30,
    nation_filter: str | None = None,
    family_filter: str | None = None,
    japan_targeted_only: bool = False,
    high_importance_only: bool = False,
    search_query: str | None = None,
    pir_filter: str | None = None,
    include_dormant: bool = False,
    now: datetime | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> ThreatOperationsSnapshot:
    """Threat Operations page 全体の data を集計。

    重い集計 (``_compute_snapshot_core``) は production (既定 DB・now 未指定) で
    TTL cache し、filter / 検索の変更はキャッシュ済み activities への軽量
    post-filter だけで返す。

    ``pir_filter``: ``"__any__"`` で「いずれかの enabled PIR に該当する actor」のみ、
    具体的な pir_id でその PIR に該当する actor のみに絞る (③ PIR 優先フィルタ)。

    ``include_dormant``: 期間内観測 0 件の辞書 group アクターも件数 0 の activity
    として含める。ミッション脅威評価 (actor_threat.py) は休眠アクターも評価対象と
    するため (暗域=不明≠安全 — 休眠 = 脅威消滅ではない)。既定 False で従来挙動。
    """
    base = now or datetime.now(UTC)
    use_cache = now is None and db_path == DEFAULT_DB_PATH
    cache_key = (lookback_days, japan_targeted_only, high_importance_only)
    if use_cache:
        with _snapshot_cache_lock:
            hit = _snapshot_core_cache.get(cache_key)
            if hit is not None and time.monotonic() - hit[0] < _SNAPSHOT_CACHE_TTL_SECONDS:
                core = hit[1]
            else:
                core = _compute_snapshot_core(
                    lookback_days=lookback_days,
                    japan_targeted_only=japan_targeted_only,
                    high_importance_only=high_importance_only,
                    now=base,
                    db_path=db_path,
                )
                _snapshot_core_cache[cache_key] = (time.monotonic(), core)
    else:
        core = _compute_snapshot_core(
            lookback_days=lookback_days,
            japan_targeted_only=japan_targeted_only,
            high_importance_only=high_importance_only,
            now=base,
            db_path=db_path,
        )

    activities: list[ActorActivity] = list(core.activities)
    if not include_dormant:
        # dormant 行 = 観測 0 件で追加された group のみ (観測ありは常に >= 1 件)
        activities = [a for a in activities if a.total_articles > 0]
    unknown_count = core.unknown_bucket_count

    # filter 適用
    def _passes_filter(a: ActorActivity) -> bool:
        if nation_filter and a.nation != nation_filter:
            return False
        if family_filter and a.family != family_filter:
            return False
        if japan_targeted_only and a.japan_targeted_count == 0:
            return False
        if pir_filter:
            if pir_filter == "__any__":
                if not a.matched_pir_ids:
                    return False
            elif pir_filter not in a.matched_pir_ids:
                return False
        if search_query:
            prefix, value = _parse_search_prefix(search_query)
            if prefix:
                if not _entity_search_match(a, prefix, value):
                    return False
            else:
                q = search_query.lower()
                haystack = " ".join(
                    [
                        a.canonical.lower(),
                        *(al.lower() for al in a.aliases),
                        a.mitre_group.lower(),
                        a.nation.lower(),
                        a.family.lower(),
                    ],
                )
                if q not in haystack:
                    return False
        return True

    filtered = [a for a in activities if _passes_filter(a)]
    # activity desc 順
    filtered.sort(key=lambda a: -a.total_articles)

    # 全 nation の列挙 (filter dropdown 用)
    all_nations = tuple(sorted({a.nation for a in activities if a.nation}))

    # Discovery panel (filter とは独立、常に全 activities から計算)
    new_actors = sorted(
        [a for a in activities if a.is_new],
        key=lambda a: -a.total_articles,
    )[:8]
    spiking = sorted(
        [a for a in activities if a.is_spike and not a.is_new],
        key=lambda a: -a.spike_ratio,
    )[:8]
    waking = sorted(
        [a for a in activities if a.is_quiet_waking],
        key=lambda a: -a.total_articles,
    )[:8]

    discovery = DiscoveryPanel(
        new_actors=tuple(new_actors),
        spiking_actors=tuple(spiking),
        waking_actors=tuple(waking),
        unknown_bucket_count=unknown_count,
    )

    return ThreatOperationsSnapshot(
        actors=tuple(filtered),
        families=dict(core.families),
        nations=all_nations,
        discovery=discovery,
        lookback_days=lookback_days,
        filters={
            "nation": nation_filter or "",
            "family": family_filter or "",
            "japan_targeted_only": "1" if japan_targeted_only else "",
            "high_importance_only": "1" if high_importance_only else "",
            "search": search_query or "",
            "pir": pir_filter or "",
        },
    )


def fetch_actor_detail(
    actor_id: str,
    *,
    lookback_days: int = 30,
    now: datetime | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> ActorDetail | None:
    """1 actor の詳細 view (timeline + 関連 + 記事 list)。"""
    base = now or datetime.now(UTC)
    earliest = base - timedelta(days=lookback_days)

    registry = load_actor_aliases()
    actor_by_id = {a.id: a for a in registry.actors}
    meta = actor_by_id.get(actor_id)
    if meta is None:
        return None
    lookup = _build_actor_lookup(registry.actors)

    with _connect(db_path) as con:
        rows = con.execute(
            """
            SELECT article_id, title, summary, url, feed_title, importance,
                   posted_channel, created_at, discord_message_id, discord_channel_id,
                   victim_sector_canonical, victim_country_iso,
                   socio_political_intent, technical_axis_summary, event_date,
                   subject_actor_ids, subject_actor_source
              FROM articles
             WHERE status='posted'
               AND datetime(created_at) >= datetime(?)
             ORDER BY datetime(created_at) DESC
            """,
            (earliest.isoformat(),),
        ).fetchall()

    matched_rows: list[sqlite3.Row] = []
    cooccur_c: Counter[str] = Counter()
    for r in rows:
        # snapshot と同一の subject-gate を適用 (詳細の記事 list / 共起も主題ベースで揃える)
        found = _gated_actor_ids(r, lookup)
        if actor_id not in found:
            continue
        matched_rows.append(r)
        # 共起 actor (自分以外)
        for other in found:
            if other != actor_id:
                cooccur_c[other] += 1

    # snapshot 共有: activity 計算 (filter なしで全 activity に該当行)。
    # now は呼び出し元の値をそのまま渡す — production (now=None) では TTL cache に
    # 乗り、detail 表示のたびに全走査し直さない (tests は now 指定で素通り)。
    snap = fetch_threat_operations_snapshot(
        lookback_days=lookback_days,
        now=now,
        db_path=db_path,
    )
    activity = next((a for a in snap.actors if a.actor_id == actor_id), None)
    if activity is None:
        # snapshot 圏外 (期間内観測 0 = 休眠) でも辞書に居る actor は詳細を返す。
        # 休眠アクターのミッション脅威評価 (Critical・休眠) を点検可能にするため
        # (旧挙動は None → 詳細不可。暗域=不明≠安全)。
        counts = _bucketize_daily(matched_rows, lookback_days=lookback_days, now=base)
        activity = ActorActivity(
            actor_id=actor_id,
            canonical=meta.canonical,
            aliases=meta.aliases,
            nation=meta.nation or "",
            family=meta.family or "",
            mitre_group=meta.mitre_group or "",
            sponsor=meta.sponsor or "",
            kind=meta.kind,
            sponsor_org=meta.sponsor_org,
            description=meta.description,
            total_articles=len(matched_rows),
            daily_counts=counts,
            sparkline=_make_sparkline(counts),
            last_seen_iso=str(matched_rows[0]["created_at"]) if matched_rows else "",
            top_sectors=(),
            top_countries=(),
            top_cves=(),
            top_ttps=(),
            is_new=False,
            is_spike=False,
            spike_ratio=0.0,
            is_quiet_waking=False,
            japan_targeted_count=0,
            matched_pir_ids=tuple(
                sorted(
                    _build_pir_actor_index(lookup, _load_enabled_pir_priorities()).get(
                        actor_id, frozenset()
                    )
                )
            ),
        )

    # Family members (同 family の他 actor の activity)
    family_members: list[tuple[str, int]] = []
    if meta.family:
        for a in snap.actors:
            if a.family == meta.family and a.actor_id != actor_id:
                family_members.append((a.actor_id, a.total_articles))
        family_members.sort(key=lambda x: -x[1])

    # Co-occur (this lookup から)
    cooccur_list: list[tuple[str, int]] = []
    for other_id, n in cooccur_c.most_common(8):
        if other_id not in actor_by_id:
            continue
        cooccur_list.append((other_id, n))

    relations = ActorRelations(
        family_members=tuple(family_members[:8]),
        cooccur_actors=tuple(cooccur_list),
        related_campaigns=(),  # 将来 campaign yaml で拡張
    )

    # Recent articles (新しい順 上位 20)
    recent: list[ActorArticleRef] = []
    for r in matched_rows[:20]:
        recent.append(
            ActorArticleRef(
                article_id=str(r["article_id"]),
                title=str(r["title"] or ""),
                url=str(r["url"] or ""),
                feed_title=str(r["feed_title"] or ""),
                importance=str(r["importance"] or ""),
                created_at=str(r["created_at"])[:16].replace("T", " "),
                posted_channel=str(r["posted_channel"] or ""),
                discord_message_id=(
                    str(r["discord_message_id"]) if r["discord_message_id"] else None
                ),
                discord_channel_id=(
                    str(r["discord_channel_id"]) if r["discord_channel_id"] else None
                ),
                socio_political_intent=(
                    str(r["socio_political_intent"]) if r["socio_political_intent"] else None
                ),
                technical_axis_summary=(
                    str(r["technical_axis_summary"]) if r["technical_axis_summary"] else None
                ),
            ),
        )

    # organization の配下グループ rollup: sponsor_org==この機関の group の活動を集計。
    # 「GRU 全体でいま何が起きているか」を機関単位で俯瞰する (observation 側の rollup)。
    child_groups: tuple[tuple[str, str, int, str], ...] = ()
    if meta.kind == "organization":
        children = [a for a in registry.actors if a.sponsor_org == actor_id]
        child_ids = {c.id for c in children}
        child_counts: dict[str, int] = {c.id: 0 for c in children}
        child_rows: dict[str, list[sqlite3.Row]] = {c.id: [] for c in children}
        for r in rows:
            for cid in _gated_actor_ids(r, lookup) & child_ids:
                child_counts[cid] += 1
                child_rows[cid].append(r)
        child_groups = tuple(
            (
                c.id,
                c.canonical,
                child_counts[c.id],
                _make_sparkline(
                    _bucketize_daily(child_rows[c.id], lookback_days=lookback_days, now=base)
                ),
            )
            for c in sorted(children, key=lambda x: -child_counts[x.id])
        )

    timeline_event, timeline_coverage = _event_daily_series(
        matched_rows, lookback_days=lookback_days, now=base
    )
    return ActorDetail(
        activity=activity,
        relations=relations,
        recent_articles=tuple(recent),
        timeline_daily=_daily_series(matched_rows, lookback_days=lookback_days, now=base),
        timeline_event=timeline_event,
        timeline_coverage=timeline_coverage,
        known_ttps=meta.mitre_ttps,
        child_groups=child_groups,
    )


def actor_canonical_lookup() -> dict[str, str]:
    """actor_id → canonical 名 lookup (UI 表示用)。"""
    return {a.id: a.canonical for a in load_actor_aliases().actors}
