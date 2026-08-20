"""アクター行動史の月次蒸留 (アクター辞書 Phase1 F7)。

観測 (subject 記事) → 知識 (月次期間行) の**決定論射影** — LLM は使わない。
取込時判定 (subject_actor_ids / victim_sector / entities) の月次集計のみを行う。

設計原則 (2026-07-26 確定):
- **subject (主題) 記事のみ数える** — 言及混入による誤帰属の永久固定を防ぐ
- 月境界は **JST** (運用 TZ)。バケツ軸は created_at (収集網の観測時刻、常に非 NULL)
- 行は「収集網の観測の記録」であり世界の全体像ではない (観測≠世界)
- actor_id は書込時 canonical のまま — merge 後の合算は表示時 resolve_actor_id()
- 主題判定層は 2026-07-17 稼働のため **それ以前の月は原理的に遡及不能**
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict

from src.cti.japan_relevance import is_japan_targeted_row

# 行動史の開始月 = 収集開始月 (最古 created_at 2026-04-28)。
# title 層 (決定論) は title が永久メタのため全史に遡及適用できる — 2026-07-26 に
# scripts/backfill_subject_actors.py で全史 backfill 済み。LLM 補完層は生入力の保存開始
# (2026-07-26、D1) 以降のみ有効 (それ以前の出力は真に失われている)。
SUBJECT_EPOCH_MONTH = "2026-04"

# 月境界の TZ (運用 TZ=Asia/Tokyo に一致)。後から変えると行が割れるため不変。
JST = timezone(timedelta(hours=9))

# 蒸留に使う article_entities の entity_type (SSoT は persistence.py の書込側)
DISTILL_ENTITY_TYPES: tuple[str, ...] = ("malware_family", "ttp", "campaign", "cve")


class ActorMonthProfile(BaseModel):
    """1 アクター × 1 月の観測プロファイル (actor_observed_profile 1 行)。"""

    model_config = ConfigDict(frozen=True)

    actor_id: str
    month: str  # 'YYYY-MM' (JST 境界)
    subject_articles: int = 0
    distinct_sources: int = 0
    sectors: dict[str, int] = {}
    countries: dict[str, int] = {}
    malware: dict[str, int] = {}
    ttps: dict[str, int] = {}
    campaigns: dict[str, int] = {}
    japan_targeted: int = 0
    kev_hits: int = 0


def month_label(dt: datetime) -> str:
    """datetime (aware) の JST 月ラベル 'YYYY-MM'。"""
    return dt.astimezone(JST).strftime("%Y-%m")


def month_window_utc(month: str) -> tuple[datetime, datetime]:
    """'YYYY-MM' (JST) の [開始, 翌月開始) を UTC datetime で返す。"""
    year, mon = int(month[:4]), int(month[5:7])
    start = datetime(year, mon, 1, tzinfo=JST)
    if mon == 12:
        end = datetime(year + 1, 1, 1, tzinfo=JST)
    else:
        end = datetime(year, mon + 1, 1, tzinfo=JST)
    return start.astimezone(UTC), end.astimezone(UTC)


def months_between(start_month: str, end_month: str) -> list[str]:
    """start〜end (両端含む) の 'YYYY-MM' 一覧 (昇順)。start > end は空。"""
    out: list[str] = []
    y, m = int(start_month[:4]), int(start_month[5:7])
    ey, em = int(end_month[:4]), int(end_month[5:7])
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _dedupe_by_article(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """article_id で重複排除 (run 横断で同一記事が複数行になるため GROUP BY 相当が必須)。

    created_at 最新の行を採用する (再評価で列が更新されるのは新しい行)。
    """
    latest: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        aid = str(row.get("article_id") or "")
        if not aid:
            continue
        prev = latest.get(aid)
        if prev is None or str(row.get("created_at") or "") >= str(prev.get("created_at") or ""):
            latest[aid] = row
    return list(latest.values())


def _subject_ids(row: Mapping[str, Any]) -> list[str]:
    # split_subject_ids と挙動差 (list で順序/重複を保持 + strip() の空白耐性。本関数は
    # per_actor 集計のループ用で frozenset 化すると重複記事由来の二重集計を静かに畳んで
    # しまう) のため据置 (2026-08-21 監査)。
    raw = str(row.get("subject_actor_ids") or "")
    return [s.strip() for s in raw.split(",") if s.strip()]


class _FoldCounter:
    """表記ゆれ (大小文字違い) を 1 キーに畳む counter (P1-S3、2026-07-26)。

    entity 値は LLM/抽出由来で大小文字が揺れる (実データで ZOHOMURK / Zohomurk が
    別カウントされた)。casefold キーで集計し、表示形は最頻出の表記を採用する。
    月次行は永久記録のため、この正規化は蒸留段階で行う (表示側の後処理にしない)。
    """

    def __init__(self) -> None:
        self._data: dict[str, Counter[str]] = {}

    def add(self, value: str) -> None:
        cleaned = value.strip()
        if not cleaned:
            return
        self._data.setdefault(cleaned.casefold(), Counter())[cleaned] += 1

    def to_dict(self) -> dict[str, int]:
        return {forms.most_common(1)[0][0]: sum(forms.values()) for forms in self._data.values()}


def distill_month(
    month: str,
    article_rows: Sequence[Mapping[str, Any]],
    entities: Mapping[str, Collection[tuple[str, str]]],
    kev_cves: Collection[str],
) -> list[ActorMonthProfile]:
    """1 月分の subject 記事行から全アクターの月次プロファイルを決定論的に集計する。

    Args:
        month: 'YYYY-MM' (JST)。article_rows は既にこの月の窓で抽出済みであること。
        article_rows: articles の生行 (run 横断重複あり得る — 内部で article_id dedup)。
            必要キー: article_id, created_at, subject_actor_ids, victim_sector_canonical,
            victim_country_iso, posted_channel, feed_url
        entities: article_id → {(entity_type, value)} (DISTILL_ENTITY_TYPES のみで良い)
        kev_cves: KEV 掲載 CVE 集合 (kev_hits 判定用)
    """
    kev = {str(c).upper() for c in kev_cves}
    per_actor: dict[str, dict[str, Any]] = {}
    for row in _dedupe_by_article(article_rows):
        aid = str(row["article_id"])
        ents = entities.get(aid, ())
        cves = {v.upper() for t, v in ents if t == "cve"}
        has_kev = bool(cves & kev)
        sector = str(row.get("victim_sector_canonical") or "")
        country = str(row.get("victim_country_iso") or "")
        jp = is_japan_targeted_row(
            row.get("victim_country_iso"), str(row.get("posted_channel") or "")
        )
        feed = str(row.get("feed_url") or "")
        for actor_id in _subject_ids(row):
            agg = per_actor.setdefault(
                actor_id,
                {
                    "articles": 0,
                    "feeds": set(),
                    "sectors": Counter(),
                    "countries": Counter(),
                    # 自由表記系 (entity 値) は表記ゆれを畳む。sector/country は
                    # canonical/ISO 済みのため通常 Counter のまま
                    "malware": _FoldCounter(),
                    "ttps": _FoldCounter(),
                    "campaigns": _FoldCounter(),
                    "japan": 0,
                    "kev": 0,
                },
            )
            agg["articles"] += 1
            if feed:
                agg["feeds"].add(feed)
            if sector:
                agg["sectors"][sector] += 1
            if country:
                agg["countries"][country] += 1
            for etype, value in ents:
                if etype == "malware_family":
                    agg["malware"].add(value)
                elif etype == "ttp":
                    agg["ttps"].add(value)
                elif etype == "campaign":
                    agg["campaigns"].add(value)
            if jp:
                agg["japan"] += 1
            if has_kev:
                agg["kev"] += 1
    return [
        ActorMonthProfile(
            actor_id=actor_id,
            month=month,
            subject_articles=agg["articles"],
            distinct_sources=len(agg["feeds"]),
            sectors=dict(agg["sectors"]),
            countries=dict(agg["countries"]),
            malware=agg["malware"].to_dict(),
            ttps=agg["ttps"].to_dict(),
            campaigns=agg["campaigns"].to_dict(),
            japan_targeted=agg["japan"],
            kev_hits=agg["kev"],
        )
        for actor_id, agg in sorted(per_actor.items())
    ]
