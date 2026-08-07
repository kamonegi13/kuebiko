"""PIR vs article match logic.

preview (作成時) と KPI (運用時) で共有される唯一の match engine。
SQL-level クエリで主要 signal を効率的に判定し、必要に応じて Python レベルで
text 検索 (keywords) を追加で適用する hybrid design。

match 条件 (strong_signals OR logic):
- keywords: title / summary / body のいずれかに含む
- actors:   title / summary / body / article_entities (entity_type=actor) のいずれかに含む
- sectors:  victim_sector_canonical が一致
- countries: victim_country_iso が一致 (**被害/標的国** の意味)
- actor_nations: article_entities (actor) を actor_aliases 辞書の nation で解決して一致
  (**アクター国籍** の意味 — 監査 backlog 2026-07-05 の意味反転修正)
- feed_titles: feed_title が一致 (substring match)

strong_signals が全て空の場合は no-match (LLM judgement なしの MVP)。
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from src.cti.keyword_match import keyword_in_text, keyword_pattern
from src.logging_config import get_logger
from src.pir.article_facts import ArticleFacts
from src.pir.models import Pir, StrongSignals
from src.pir.signal_match import evaluate_match
from src.storage.run_history import RunHistoryRepository

_log = get_logger(__name__)


@dataclass(frozen=True)
class PirMatch:
    """1 件の article が PIR にマッチした結果。"""

    article_id: str
    title: str
    url: str
    feed_title: str
    importance: str
    posted_channel: str | None
    created_at: str  # ISO format
    matched_via: tuple[str, ...]  # 例: ("keywords", "actors")
    feed_url: str = ""  # 安定 source キー (top_feeds 集計を改名で割らない)。後方互換で末尾+default
    # R-C (痩せた入力での生成の是正): Spotlight/DailyFocus narrative の接地用。
    # SELECT 済の summary を載せ、タイトルのみ生成 (= 捏造温床) を防ぐ。
    summary: str = ""


@dataclass(frozen=True)
class PirKpi:
    """PIR の KPI snapshot。"""

    pir_id: str
    match_count_7d: int
    match_count_30d: int
    last_match_at: str | None
    top_feeds: list[tuple[str, int]]  # [(feed_title, count), ...]
    top_actors: list[tuple[str, int]]
    channel_distribution: dict[str, int]
    samples: list[PirMatch]  # 最新 10 件


def has_strong_signals(pir: Pir) -> bool:
    """strong_signals が 1 つでも設定されていれば True。"""
    ss = pir.strong_signals
    return bool(
        ss.keywords
        or ss.actors
        or ss.sectors
        or ss.countries
        or ss.actor_nations
        or ss.feed_titles
        or ss.signals,
    )


def has_match_criteria(pir: Pir) -> bool:
    """PIR が照合基準を 1 つでも持つか (strong_signals または match ツリー)。

    rebuild / KPI / flow のゲートはこちらを使う。旧 has_strong_signals だけを見ると
    match ツリーのみの PIR (keyword 残置を撤去した後の姿) が照合から漏れる
    (docs/pir_concept_llm_judge_design.md §4.5 の潜在バグ修正)。
    """
    return has_strong_signals(pir) or pir.match is not None


@lru_cache(maxsize=1)
def _actor_nation_by_name() -> dict[str, str]:
    """既知アクター名 (canonical + alias、小文字) → 国籍 ISO (小文字) の照合 map。

    actor_nations 照合用 (監査 backlog 2026-07-05)。辞書ロード失敗時は空 map =
    actor_nations 条件が単に不成立になる (fail-safe、他 signal は生きる)。
    **国家系アクターのみ** (2026-07-18): actor_nations の意味論 = 「国家系アクターの
    国籍」。犯罪ランサム/ハクティビスト (Qilin=ru 等) は nation 帰属があっても
    国別 APT PIR に載せない (NON_STATE_FAMILIES 除外、ユーザー判断)。
    ⚠ **legacy 照合 (subject 未評価行) 専用**。entity 値は辞書 id ("salt_typhoon") の
    ため複数語 id はこの map で解決できないが、主題ゲートなしに id 解決を足すと
    比較言及の entity で誤分類が再発するため意図的に直さない
    (docs/subject_actor_attribution_design.md §1 ⚠)。主題ゲート側は
    ``_actor_nation_by_id`` を使う。
    """
    try:
        from src.cti.actor_normalizer import load_actor_aliases
        from src.cti.threat_actor_doctrine import NON_STATE_FAMILIES

        registry = load_actor_aliases()
    except Exception as e:  # noqa: BLE001 — 辞書破損で evaluator 全体を殺さない
        _log.warning("actor_nation_map_load_failed", error=str(e))
        return {}
    out: dict[str, str] = {}
    for actor in registry.actors:
        nation = (actor.nation or "").strip().lower()
        if not nation or (actor.family or "") in NON_STATE_FAMILIES:
            continue
        for name in actor.all_names:
            if name:
                out[name.lower()] = nation
    return out


# ---- 主題アクター層 (2026-07-17、docs/subject_actor_attribution_design.md) ----

_SUBJECT_GATE_ENV = "SUBJECT_ACTOR_GATE"


def _subject_gate_enabled() -> bool:
    """主題ゲートの有効判定。既定 ON、SUBJECT_ACTOR_GATE=0 で全行 legacy 照合に rollback。"""
    raw = os.environ.get(_SUBJECT_GATE_ENV, "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


@lru_cache(maxsize=1)
def _actor_nation_by_id() -> dict[str, str]:
    """辞書 id → 国籍 ISO (小文字)。主題ゲート分岐の actor_nations 照合用。

    _actor_nation_by_name 同様 **国家系アクターのみ** (犯罪ランサム/ハクティビストの
    nation 帰属は国別 APT PIR の対象外 — 2026-07-18 ユーザー判断)。
    """
    try:
        from src.cti.actor_normalizer import load_actor_aliases
        from src.cti.threat_actor_doctrine import NON_STATE_FAMILIES

        registry = load_actor_aliases()
    except Exception as e:  # noqa: BLE001
        _log.warning("actor_nation_id_map_load_failed", error=str(e))
        return {}
    return {
        a.id.lower(): (a.nation or "").strip().lower()
        for a in registry.actors
        if (a.nation or "").strip() and (a.family or "") not in NON_STATE_FAMILIES
    }


@lru_cache(maxsize=128)
def _resolve_pir_actor_names(
    names: tuple[str, ...],
) -> tuple[frozenset[str], tuple[str, ...]]:
    """PIR の actors 名を辞書 id に解決する。返り値 = (解決済 id 集合, 未解決名 lower)。

    PIR 名は自由文字列 ("Lazarus") で辞書 canonical ("Lazarus Group") と完全一致しない
    ことがあるため、**双方向 word-boundary 一致** で解決する
    (threat_operations._build_pir_actor_index と同規則)。未解決名は辞書外アクター —
    主題ゲート分岐ではタイトル語境界照合のみに fallback する。
    """
    try:
        from src.cti.actor_normalizer import _alias_pattern, load_actor_aliases

        registry = load_actor_aliases()
    except Exception as e:  # noqa: BLE001
        _log.warning("pir_actor_resolution_failed", error=str(e))
        return frozenset(), tuple(n.lower() for n in names if len(n) >= 3)
    resolved: set[str] = set()
    unresolved: list[str] = []
    for raw in names:
        name_low = str(raw).strip().lower()
        if len(name_low) < 3:
            continue
        hit = False
        for actor in registry.actors:
            for cand in actor.all_names:
                cand_low = cand.lower()
                if (name_low in cand_low and _alias_pattern(name_low).search(cand_low)) or (
                    cand_low in name_low and _alias_pattern(cand_low).search(name_low)
                ):
                    resolved.add(actor.id.lower())
                    hit = True
                    break
        if not hit:
            unresolved.append(name_low)
    return frozenset(resolved), tuple(unresolved)


def _safe_row_get(row: sqlite3.Row, key: str) -> str:
    """row[key] を欠損列でも例外を出さず取得 (sqlite3.Row は欠損で IndexError)。"""
    try:
        value = row[key]
    except (IndexError, KeyError):
        return ""
    return str(value) if value is not None else ""


# 監査 2026-07-05 P3 の語境界照合は src/cti/keyword_match.py に SSoT 化した
# (M4: match_lists と共有)。既存参照のため薄い alias を維持する。
_keyword_pattern = keyword_pattern


def _keyword_in_text(kw: str, text: str) -> bool:
    """keyword 1 語のマッチ判定 (ASCII=語境界 / 日本語=substring)。text は小文字化済み前提。"""
    return keyword_in_text(kw, text)


def _row_search_text(row: sqlite3.Row) -> str:
    """1 row の全文検索対象 (title + summary + body) を小文字で組み立てる。"""
    return " ".join(
        filter(
            None,
            [
                row["title"] or "",
                row["summary"] or "",
                row["body"] or "",
            ],
        ),
    ).lower()


def _row_match_signals(
    row: sqlite3.Row,
    actor_values: set[str],
    strong: StrongSignals,
    derived_signals: dict[str, bool] | None = None,
    *,
    text: str | None = None,
) -> tuple[str, ...]:
    """1 row が strong_signals にマッチする signal type を返す。

    ``derived_signals`` (B2): routing 時に供給される派生 signal の真偽
    ({"kev": bool, "zero_day": bool, ...})。strong.signals 条件の OR 評価に使う。
    None (DB 経路: KPI/preview) では signals 条件は評価されない (graceful skip)。

    (exclude_signals は 2026-07-23 撤去 — 利用 0 件、除外は match ツリーの not 節で表現。)
    """
    matched: list[str] = []

    # text 検索用に title + summary + body を結合。複数 PIR を続けて評価する場合は
    # 呼び出し側が 1 度だけ組み立てて渡す (evaluate_pirs_batch)。
    if text is None:
        text = _row_search_text(row)

    # 主題アクター層 (docs/subject_actor_attribution_design.md): 評価済み行
    # (subject_actor_source 非 NULL) は actors/actor_nations を **主題 id** で照合し、
    # 本文の比較言及 (「Salt Typhoon の手法とも類似」等) での誤分類を排除する。
    # 未評価 (legacy) 行は従来照合を bug-for-bug で温存 (id 修復を legacy に適用すると
    # 言及 entity 経由で同じ誤分類が再発するため)。
    subject_source = _safe_row_get(row, "subject_actor_source")
    subject_gated = bool(subject_source) and _subject_gate_enabled()
    subject_ids: frozenset[str] = frozenset()
    if subject_gated:
        subject_ids = frozenset(
            s.strip().lower()
            for s in _safe_row_get(row, "subject_actor_ids").split(",")
            if s.strip()
        )
    subject_contributed = False

    # keywords (ASCII は語境界必須 — 監査 2026-07-05 P3)
    if strong.keywords:
        for kw in strong.keywords:
            if kw and _keyword_in_text(kw, text):
                matched.append("keywords")
                break

    # actors: 主題ゲート (評価済み行) / text 検索 + article_entities (legacy 行)
    if strong.actors:
        if subject_gated:
            pir_ids, unresolved_names = _resolve_pir_actor_names(
                tuple(a for a in strong.actors if a)
            )
            hit = bool(subject_ids & pir_ids)
            if not hit and unresolved_names:
                # 辞書外の PIR アクター名は主題判定が持てない — タイトル語境界照合
                # のみに fallback (タイトル = 主題の決定論近似)
                from src.cti.actor_normalizer import _alias_pattern

                title_low = str(row["title"] or "").lower()
                hit = any(
                    n in title_low and _alias_pattern(n).search(title_low) for n in unresolved_names
                )
            if hit:
                matched.append("actors")
                subject_contributed = True
        else:
            ss_actors_lower = {a.lower() for a in strong.actors if a}
            if any(a in text for a in ss_actors_lower) or actor_values & ss_actors_lower:
                matched.append("actors")

    # sectors: victim_sector_canonical
    if strong.sectors:
        sector = (row["victim_sector_canonical"] or "").lower()
        if sector and any(s.lower() == sector for s in strong.sectors):
            matched.append("sectors")

    # countries: victim_country_iso (**被害/標的国**)
    if strong.countries:
        country = (row["victim_country_iso"] or "").lower()
        if country and any(c.lower() == country for c in strong.countries):
            matched.append("countries")

    # actor_nations: 記事の actor を辞書の nation で解決して照合 (**アクター国籍**)。
    # 監査 backlog 2026-07-05: APT 系 PIR の countries が victim_country 照合で
    # 「中国=被害者」の記事を中国 APT 動向に計上する意味反転を起こしていた。
    if strong.actor_nations:
        targets = {n.lower() for n in strong.actor_nations if n}
        if subject_gated:
            # 主題ゲート: 主題 id → nation (言及 entity は使わない)
            if subject_ids:
                nation_by_id = _actor_nation_by_id()
                if any(nation_by_id.get(s) in targets for s in subject_ids):
                    matched.append("actor_nations")
                    subject_contributed = True
        elif actor_values:
            nation_by_name = _actor_nation_by_name()
            if any(nation_by_name.get(v) in targets for v in actor_values):
                matched.append("actor_nations")

    # feed_titles
    if strong.feed_titles:
        ft = (row["feed_title"] or "").lower()
        if ft and any(target.lower() in ft for target in strong.feed_titles):
            matched.append("feed_titles")

    # signals (B2): 派生 signal 条件 (OR)。routing 時のみ derived_signals が供給される。
    if strong.signals and derived_signals and any(derived_signals.get(s) for s in strong.signals):
        matched.append("signals")

    # 主題ゲートが actors/actor_nations の判定に寄与した場合は provenance を併記
    # (matched_via に "subject:title" / "subject:llm" — なぜこの節に出たかの透明性)
    if subject_contributed:
        matched.append(f"subject:{subject_source}")

    return tuple(matched)


_SIGNAL_FIRST_ENV = "PIR_SIGNAL_FIRST"
_LLM_JUDGE_ENV = "PIR_LLM_JUDGE"


def _signal_first_enabled() -> bool:
    """signal-first 照合の有効判定。既定 ON (2026-07-23 Phase E)。

    PIR_SIGNAL_FIRST=0 で旧 strong_signals (生キーワード OR) に完全 fallback
    (緊急 rollback 用)。OFF の間は match ツリーが設定されていても旧照合のまま。
    """
    raw = os.environ.get(_SIGNAL_FIRST_ENV, "1").strip().lower()
    return raw not in ("0", "false", "no", "off", "")


def _llm_judge_enabled() -> bool:
    """LLM 主題判定の合成を有効にするか。既定 ON。

    PIR_LLM_JUDGE=0 で verdict 要求をスキップし候補ゲート (match ツリー) のみで
    照合する = 判定層導入前の挙動に degrade (recall 側に倒れる rollback)。
    """
    raw = os.environ.get(_LLM_JUDGE_ENV, "1").strip().lower()
    return raw not in ("0", "false", "no", "off", "")


def _llm_judge_required(pir: Pir) -> bool:
    """この PIR の match に LLM 主題確定が要るか (signal-first 経路でのみ意味を持つ)。"""
    return bool(pir.llm_judge.enabled) and _llm_judge_enabled()


def pir_match_signals(
    pir: Pir,
    row: sqlite3.Row,
    actor_values: set[str],
    derived_signals: dict[str, bool] | None = None,
    *,
    text: str | None = None,
    llm_confirmed: frozenset[str] | None = None,
    require_llm: bool = True,
) -> tuple[str, ...]:
    """PIR × row の照合を dispatch する (signal-first / legacy keyword)。

    ``PIR_SIGNAL_FIRST=1`` かつ ``pir.match`` が設定されていれば signal-first
    (ArticleFacts への述語ツリー評価)、それ以外は従来の ``_row_match_signals``
    (生キーワード OR)。

    ``llm_confirmed`` = この記事に適合 verdict がある pir_id 集合
    (pir_llm_judgments 由来、呼び出し側が prefetch)。``pir.llm_judge.enabled`` の
    PIR は match ツリー (候補ゲート) 通過に加え verdict を要求する。未判定は
    不適合扱い (夜間バッチ判定後の rebuild で昇格する eventual consistency)。
    ``require_llm=False`` は候補ゲートのみで評価 (authoring preview の
    「LLM 未確定候補」算出用)。
    """
    match_tree = getattr(pir, "match", None)
    if match_tree and _signal_first_enabled():
        facts = ArticleFacts.from_db_row(row, text=text, actor_values=actor_values)
        ok, fired = evaluate_match(match_tree, facts)
        if not ok:
            return ()
        via = tuple(f"signal:{p}" for p in sorted(fired)) or ("signal",)
        if require_llm and _llm_judge_required(pir):
            if pir.id not in (llm_confirmed or frozenset()):
                return ()
            via = (*via, "llm:subject")
        return via
    return _row_match_signals(
        row,
        actor_values,
        pir.strong_signals,
        derived_signals,
        text=text,
    )


def _fetch_article_actor_values(
    conn: sqlite3.Connection,
    article_ids: list[str],
) -> dict[str, set[str]]:
    """article_id ごとに actor entity value の set を返す。"""
    if not article_ids:
        return {}
    placeholders = ",".join("?" for _ in article_ids)
    rows = conn.execute(
        f"SELECT article_id, value FROM article_entities "  # noqa: S608
        f"WHERE entity_type='actor' AND article_id IN ({placeholders})",
        article_ids,
    ).fetchall()
    out: dict[str, set[str]] = {}
    for r in rows:
        out.setdefault(r["article_id"], set()).add((r["value"] or "").lower())
    return out


def _fetch_llm_confirmed_map(
    repo: RunHistoryRepository | None,
    db_path: Path | None,
) -> dict[str, frozenset[str]]:
    """LLM 主題判定の適合 verdict map (article_id → pir_id 集合) をロードする。

    llm_judge を要する PIR が対象に含まれる場合のみ呼ぶ (それ以外は空 dict で足り、
    余計な query を打たない)。DB 障害時は空 map = judge PIR が不適合側に倒れる
    (fail-safe、他 PIR は生きる)。
    """
    try:
        if repo is not None:
            repo_inst = repo
        elif db_path is not None:
            repo_inst = RunHistoryRepository(db_path=db_path)
        else:
            repo_inst = RunHistoryRepository()
        return repo_inst.fetch_pir_llm_confirmed()
    except Exception as e:  # noqa: BLE001 — verdict 読取失敗で照合全体を殺さない
        _log.warning("pir_llm_confirmed_load_failed", error=str(e))
        return {}


def _load_posted_rows(
    *,
    repo: RunHistoryRepository | None = None,
    db_path: Path | None = None,
    lookback_hours: int = 168,
    limit: int = 4000,
) -> tuple[list[sqlite3.Row], dict[str, set[str]]]:
    """posted articles の評価対象 rows + actor map を 1 query でロードする。"""
    if repo is not None:
        repo_inst = repo
    elif db_path is not None:
        repo_inst = RunHistoryRepository(db_path=db_path)
    else:
        repo_inst = RunHistoryRepository()
    since = datetime.now(UTC) - timedelta(hours=lookback_hours)

    with repo_inst._connect() as conn:  # noqa: SLF001 (intra-package)
        rows = conn.execute(
            """
            SELECT article_id, title, url, feed_title, feed_url, importance,
                   posted_channel, created_at, summary, body,
                   victim_sector_canonical, victim_country_iso,
                   subject_actor_ids, subject_actor_source,
                   category, socio_political_intent, is_ransomware
              FROM articles
             WHERE status='posted'
               AND datetime(created_at) >= datetime(?)
             ORDER BY datetime(created_at) DESC
             LIMIT ?
            """,
            (since.isoformat(), max(limit, 1)),
        ).fetchall()

        article_ids = [r["article_id"] for r in rows]
        actor_map = _fetch_article_actor_values(conn, article_ids)
    if len(rows) >= max(limit, 1):
        # no-silent-caps: 窓内の行が limit を超えている = 古い側が黙って落ちて
        # KPI/match が過小計上される (監査 2026-07-05 P3)。
        _log.warning(
            "pir_posted_rows_limit_reached",
            limit=limit,
            lookback_hours=lookback_hours,
        )
    return rows, actor_map


def evaluate_pir_matches(
    pir: Pir,
    *,
    repo: RunHistoryRepository | None = None,
    db_path: Path | None = None,
    lookback_hours: int = 168,
    limit: int = 4000,
) -> list[PirMatch]:
    """PIR に match する article を返す (sample 用途、最大 limit 件)。

    照合基準 (strong_signals / match) の無い PIR はノーマッチ (空リスト) を返す。
    """
    if not has_match_criteria(pir):
        return []

    rows, actor_map = _load_posted_rows(
        repo=repo,
        db_path=db_path,
        lookback_hours=lookback_hours,
        limit=limit,
    )
    confirmed_map = _fetch_llm_confirmed_map(repo, db_path) if _llm_judge_required(pir) else {}

    matches: list[PirMatch] = []
    for r in rows:
        actor_values = actor_map.get(r["article_id"], set())
        matched_via = pir_match_signals(
            pir,
            r,
            actor_values,
            llm_confirmed=confirmed_map.get(str(r["article_id"])),
        )
        if not matched_via:
            continue
        matches.append(_to_pir_match(r, matched_via))
    return matches


def _to_pir_match(row: sqlite3.Row, matched_via: tuple[str, ...]) -> PirMatch:
    return PirMatch(
        article_id=row["article_id"],
        title=row["title"] or "",
        url=row["url"] or "",
        feed_title=row["feed_title"] or "",
        feed_url=row["feed_url"] or "",
        importance=row["importance"] or "",
        posted_channel=row["posted_channel"],
        # PG datetime → ISO str に統一 (PirMatch.created_at は str 型)
        created_at=(
            row["created_at"].isoformat()
            if isinstance(row["created_at"], datetime)
            else (row["created_at"] or "")
        ),
        matched_via=matched_via,
        summary=row["summary"] or "",
    )


def evaluate_pirs_batch(
    pirs: Sequence[Pir],
    *,
    repo: RunHistoryRepository | None = None,
    db_path: Path | None = None,
    lookback_hours: int = 168,
    limit: int = 4000,
) -> dict[str, list[PirMatch]]:
    """複数 PIR を **1 回の読込 + 1 回の本文正規化** で評価する。

    PIR ごとに :func:`evaluate_pir_matches` を呼ぶと、同じ期間の記事の読込と
    title+summary+body の小文字化を PIR の数だけ繰り返す (21 PIR / 30 日で実測 18 秒)。
    照合条件は PIR ごとに違うが前処理は共通なので、そこだけ畳み込む。
    照合ロジックは共有しているため、結果は 1 件ずつ評価した場合と一致する。

    戻り値は ``pir.id`` → マッチ一覧。照合基準を持たない PIR は空リスト。
    """
    result: dict[str, list[PirMatch]] = {p.id: [] for p in pirs}
    targets = [p for p in pirs if has_match_criteria(p)]
    if not targets:
        return result

    rows, actor_map = _load_posted_rows(
        repo=repo,
        db_path=db_path,
        lookback_hours=lookback_hours,
        limit=limit,
    )
    prepared = [(r, actor_map.get(r["article_id"], set()), _row_search_text(r)) for r in rows]
    confirmed_map = (
        _fetch_llm_confirmed_map(repo, db_path)
        if any(_llm_judge_required(p) for p in targets)
        else {}
    )

    for pir in targets:
        matches: list[PirMatch] = []
        for r, actor_values, text in prepared:
            matched_via = pir_match_signals(
                pir,
                r,
                actor_values,
                text=text,
                llm_confirmed=confirmed_map.get(str(r["article_id"])),
            )
            if matched_via:
                matches.append(_to_pir_match(r, matched_via))
        result[pir.id] = matches
    return result


# ---------- 情報フロー集計 (PIR → importance → channel の背骨をエッジ実数で描く) ----------


@dataclass(frozen=True)
class PirFlowStat:
    """1 PIR の期間内マッチ実数 (importance / channel 内訳付き)。"""

    pir_id: str
    matched_total: int
    by_importance: dict[str, int]
    by_channel: dict[str, int]


@dataclass(frozen=True)
class FlowSnapshot:
    """情報フロー画面用の集計 snapshot (期間内の posted articles が母集合)。"""

    total_articles: int
    importance_totals: dict[str, int]
    # (importance, channel, count) — importance→channel エッジの実数
    importance_channel: list[tuple[str, str, int]]
    pir_stats: list[PirFlowStat]


def _bucket(value: str | None) -> str:
    return (value or "").strip().lower() or "unknown"


def flow_snapshot_from_rows(
    pirs: list[Pir],
    rows: list[sqlite3.Row],
    actor_map: dict[str, set[str]],
    llm_confirmed_map: dict[str, frozenset[str]] | None = None,
) -> FlowSnapshot:
    """rows (posted articles) から FlowSnapshot を集計する純粋関数。

    pir_stats は ``pirs`` と同順。照合基準の無い PIR は 0 件扱い
    (evaluate_pir_matches と同じ規約)。``llm_confirmed_map`` は LLM 主題判定の
    適合 verdict (article_id → pir_id 集合、compute_flow_snapshot が供給)。
    """
    importance_totals: dict[str, int] = {}
    edge_counts: dict[tuple[str, str], int] = {}
    for r in rows:
        imp = _bucket(r["importance"])
        ch = _bucket(r["posted_channel"])
        importance_totals[imp] = importance_totals.get(imp, 0) + 1
        edge_counts[(imp, ch)] = edge_counts.get((imp, ch), 0) + 1

    confirmed = llm_confirmed_map or {}
    pir_stats: list[PirFlowStat] = []
    for pir in pirs:
        if not has_match_criteria(pir):
            pir_stats.append(PirFlowStat(pir.id, 0, {}, {}))
            continue
        total = 0
        by_importance: dict[str, int] = {}
        by_channel: dict[str, int] = {}
        for r in rows:
            actor_values = actor_map.get(r["article_id"], set())
            if not pir_match_signals(
                pir,
                r,
                actor_values,
                llm_confirmed=confirmed.get(str(r["article_id"])),
            ):
                continue
            total += 1
            imp = _bucket(r["importance"])
            ch = _bucket(r["posted_channel"])
            by_importance[imp] = by_importance.get(imp, 0) + 1
            by_channel[ch] = by_channel.get(ch, 0) + 1
        pir_stats.append(PirFlowStat(pir.id, total, by_importance, by_channel))

    return FlowSnapshot(
        total_articles=len(rows),
        importance_totals=importance_totals,
        importance_channel=[(i, c, n) for (i, c), n in sorted(edge_counts.items())],
        pir_stats=pir_stats,
    )


def compute_flow_snapshot(
    pirs: list[Pir],
    *,
    repo: RunHistoryRepository | None = None,
    db_path: Path | None = None,
    lookback_hours: int = 168,
    limit: int = 5000,
) -> FlowSnapshot:
    """DB から期間内 posted articles をロードして FlowSnapshot を集計する。"""
    rows, actor_map = _load_posted_rows(
        repo=repo,
        db_path=db_path,
        lookback_hours=lookback_hours,
        limit=limit,
    )
    confirmed_map = (
        _fetch_llm_confirmed_map(repo, db_path) if any(_llm_judge_required(p) for p in pirs) else {}
    )
    return flow_snapshot_from_rows(pirs, rows, actor_map, confirmed_map)


def compute_pir_kpi(
    pir: Pir,
    *,
    repo: RunHistoryRepository | None = None,
    db_path: Path | None = None,
    sample_limit: int = 10,
) -> PirKpi:
    """PIR の KPI snapshot を計算 (必須 5 + 詳細 5)。"""
    # 監査 2026-07-05 P3: 5000 は 30d の posted 実流量 (~9000) を下回り、古い ~45% が
    # 黙って落ちて match_count_30d / top_feeds / trend が系統的に過小計上されていた。
    matches_30d = evaluate_pir_matches(
        pir,
        repo=repo,
        db_path=db_path,
        lookback_hours=24 * 30,
        limit=15000,
    )

    # 7d / 30d 分割 — m.created_at は SQLite では ISO str、 PG では datetime。
    # 比較用に両形式を datetime に正規化。
    cutoff_7d = datetime.now(UTC) - timedelta(days=7)

    def _to_dt(v: object) -> datetime | None:
        if isinstance(v, datetime):
            return v
        if isinstance(v, str) and v:
            try:
                return datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                return None
        return None

    matches_7d = [m for m in matches_30d if (_to_dt(m.created_at) or cutoff_7d) >= cutoff_7d]

    # last match — PG だと datetime、 SQLite だと str。 表示用に str 統一。
    if matches_30d:
        raw = matches_30d[0].created_at
        last_match_at = raw.isoformat() if isinstance(raw, datetime) else str(raw)
    else:
        last_match_at = None

    # top feeds: 安定キー feed_url で集計し改名で割れないように (表示は feed_title)。
    feed_counts: dict[str, int] = {}
    feed_title_for_key: dict[str, str] = {}
    for m in matches_30d:
        key = m.feed_url or m.feed_title
        feed_counts[key] = feed_counts.get(key, 0) + 1
        feed_title_for_key.setdefault(key, m.feed_title or key)
    top_feeds = [
        (feed_title_for_key.get(k, k), c)
        for k, c in sorted(feed_counts.items(), key=lambda x: x[1], reverse=True)[:5]
    ]

    # top actors: article_entities を 30d 範囲で集計
    effective_db_path = db_path or (
        repo._db_path if repo is not None else RunHistoryRepository()._db_path
    )  # noqa: SLF001
    top_actors = _top_actors_for_matches(matches_30d, effective_db_path)

    # channel distribution
    channel_dist: dict[str, int] = {}
    for m in matches_30d:
        if m.posted_channel:
            channel_dist[m.posted_channel] = channel_dist.get(m.posted_channel, 0) + 1

    return PirKpi(
        pir_id=pir.id,
        match_count_7d=len(matches_7d),
        match_count_30d=len(matches_30d),
        last_match_at=last_match_at,
        top_feeds=top_feeds,
        top_actors=top_actors,
        channel_distribution=channel_dist,
        samples=matches_30d[:sample_limit],
    )


def _top_actors_for_matches(
    matches: list[PirMatch],
    db_path: Path | None,
) -> list[tuple[str, int]]:
    if not matches or not db_path:
        return []
    article_ids = [m.article_id for m in matches]
    placeholders = ",".join("?" for _ in article_ids)
    from src.cti.subject_gate import subject_gate_clause
    from src.storage.db_backend import connect as _backend_connect

    # subject-gate (2026-07-29): matched 記事の全 mention でなく主題 actor のみ集計する
    # (評価済みは主題、legacy は mention fallback)。JOIN で article_id が run 横断複数行に
    # 膨張するため COUNT(DISTINCT ae.article_id) で記事単位に数える。
    gate = subject_gate_clause(
        mention_col="ae.value",
        subject_ids_col="a.subject_actor_ids",
        subject_source_col="a.subject_actor_source",
    )
    with _backend_connect(db_path) as conn:
        if hasattr(conn, "row_factory"):
            conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT ae.value AS value, COUNT(DISTINCT ae.article_id) AS n "  # noqa: S608
            f"FROM article_entities ae JOIN articles a ON a.article_id = ae.article_id "
            f"WHERE ae.entity_type='actor' AND ae.article_id IN ({placeholders}) AND {gate} "
            f"GROUP BY ae.value ORDER BY n DESC LIMIT 5",
            article_ids,
        ).fetchall()
    return [(r["value"], int(r["n"])) for r in rows]


def evaluate_pir_for_article(
    pir: Pir,
    article_data: dict[str, Any],
    actor_values: set[str] | None = None,
    derived_signals: dict[str, bool] | None = None,
) -> tuple[str, ...]:
    """単一 article (dict-like) を PIR にマッチするか判定。

    triage / routing / 取込時 inline タグで 1 件ずつ判定する用途。
    article_data は title / summary / body / feed_title / victim_sector_canonical /
    victim_country_iso / category / socio_political_intent / is_ransomware を含む
    dict (or Row-like)。
    ``derived_signals`` (B2): routing 時に供給する派生 signal の真偽
    ({"kev": bool, ...})。strong_signals.signals 条件の評価に使う (routing 専用)。

    signal-first (match ツリー) の PIR も同じ dispatch (pir_match_signals) を通す —
    2026-07-23 以前はここだけ legacy keyword 直呼びで、rebuild と inline の挙動が
    黙って乖離していた (docs/pir_concept_llm_judge_design.md §4.5)。llm_judge PIR は
    verdict 未取得の inline では不適合 → 夜間バッチ + rebuild で昇格する。
    """
    if not has_match_criteria(pir):
        return ()

    # Row-like obj に統一
    class _RowAdapter:
        def __init__(self, d: dict[str, Any]) -> None:
            self._d = d

        def __getitem__(self, k: str) -> Any:
            return self._d.get(k)

    return pir_match_signals(
        pir,
        _RowAdapter(article_data),  # type: ignore[arg-type]
        actor_values or set(),
        derived_signals,
    )
