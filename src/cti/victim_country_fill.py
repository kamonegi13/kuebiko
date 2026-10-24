"""被害国の取りこぼしを埋める決定論の規則 (2026-10-22)。

背景: 常設の問いの根拠記事の 45% で被害国が空だった。内訳を測ると (直近 30 日、
``data/eval/victim_country_gap_2026_10_22.md``)、空の大半は意図どおり
(被害組織の無い勧告・研究・論説 = ``victim_country_gate``、被害者が未特定のリークサイト
投稿、法執行・政策の記事) で、**埋めるべきなのに空** なのは速報 (breaking) のサイバー記事に
残る一部だけだった。この関門は誤った国を付けるより欠落の方が害が小さいので、
**取りこぼしを埋める側だけ**、確実な根拠のある型に絞る:

1. ``jp_legal_form``: 被害組織・見出し・要約に日本の法人格 (株式会社 等) があり、他国への言及・
   子会社/海外の語が無い → JP。
2. ``jp_notice_title``: 見出しが日本の事業者の開示文の定型 (お詫びとお知らせ・第 N 報・最終報告)
   で、他国への言及が無い (breach / incident のみ) → JP。
3. ``org_legal_suffix``: 被害組織名の法人格が単一国に決まるもの (Pty Ltd=AU、S.p.A.=IT、
   Sp. z o.o.=PL)。GmbH / Co., Ltd / Inc / S.r.l. / Ltda 等は複数国で使われるので使わない。
4. ``org_history``: 同じ被害組織が過去に単独の被害者として複数回、同じ国で記録されている
   (全件一致のみ)。かつ組織名が見出しに出ている (見出しに無い組織は、事案の舞台として
   触れられただけの基盤サービスで、被害者とは限らない)。自前の過去データから導く組織→国の対応で、外部辞書を持たない。
   一覧記事 (被害組織が複数) は履歴に入れず、推論した行も履歴に入れない (循環強化の防止)。

規則どうしが食い違えば何も埋めない。LLM が値を出していた行 (raw が空でない) には触れない。

``VICTIM_COUNTRY_FILL``: ``shadow`` (既定、埋める値をログに出すだけ) / ``1`` / ``on`` で適用 /
``0`` / ``off`` で無効。不明な値は off に倒す (fail-safe)。
"""

from __future__ import annotations

import os
import re
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from src.cti.nation_gazetteer import nations_in_text
from src.logging_config import get_logger

_log = get_logger(__name__)

FILL_ENV = "VICTIM_COUNTRY_FILL"

#: 埋める対象の記事種別 (速報のみ。勧告・研究・論説の空は関門による意図どおりの空)
FILL_ARTICLE_TYPE = "breaking"
#: 埋める対象のカテゴリ (被害者のいるサイバー事案)
FILL_CATEGORIES: frozenset[str] = frozenset({"breach", "incident", "malware", "apt"})
#: 開示文の定型の見出しを JP とみなすカテゴリ
NOTICE_CATEGORIES: frozenset[str] = frozenset({"breach", "incident"})

#: 履歴の規則: 同じ組織が単独の被害者として記録された記事数の下限 (全件一致のみ採る)
ORG_HISTORY_MIN_ARTICLES = 2
#: 履歴を作る窓 (日)
ORG_HISTORY_WINDOW_DAYS = 180
#: 履歴の再読込間隔 (秒) / 読込失敗時に再試行するまでの間隔 (秒)
ORG_HISTORY_TTL_SECONDS = 3600.0
ORG_HISTORY_RETRY_SECONDS = 60.0

#: 推論した行の victim_country_raw に付ける接頭辞 (履歴からの除外・出所の識別に使う)
INFERRED_RAW_PREFIX = "inferred:"

_JP = "JP"
_JP_LEGAL_FORM = re.compile(r"株式会社|有限会社|合同会社|㈱|\(株\)|（株）")
_SUBSIDIARY_WORDS = re.compile(r"子会社|現地法人|海外|グループ会社|関連会社|支社|支店|拠点")
_NOTICE_TITLE = re.compile(r"お詫び|お知らせ|第\s*[0-9０-９一二三四五六七八九十]+\s*報|最終報告")

#: 法人格 → 国。単一国に決まるものだけ。GmbH・Co., Ltd・Inc・SARL に加え、S.r.l. (IT/RO/AR)・
#: Ltda (BR/CL/CO)・Pvt Ltd (IN/PK/NP)・B.V. (NL/CW)・(Pty) Ltd (ZA) は複数国で使われるため除外
#: (replay で Ltda が CL を BR と誤る例を確認)
_LEGAL_SUFFIX_COUNTRY: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?<!\()\bPty\.?\s*Ltd\b", re.IGNORECASE), "AU"),
    (re.compile(r"\bS\.p\.A\b|\bSpA\b"), "IT"),
    (re.compile(r"Sp\.\s*z\s*o\.\s*o\."), "PL"),
)


class FillMode(StrEnum):
    OFF = "off"
    SHADOW = "shadow"
    ON = "on"


@dataclass(frozen=True)
class CountryFill:
    """埋める値と、それを出した規則名。"""

    iso: str
    rule: str


def fill_mode() -> FillMode:
    """``VICTIM_COUNTRY_FILL`` を解釈する (既定 shadow、不明な値は off)。"""
    value = os.environ.get(FILL_ENV, "shadow").strip().lower()
    if value in ("1", "on"):
        return FillMode.ON
    if value == "shadow":
        return FillMode.SHADOW
    return FillMode.OFF


def _non_empty(orgs: Sequence[str]) -> list[str]:
    return [o.strip() for o in orgs if o and o.strip()]


def _has_foreign_nation(text: str) -> bool:
    return bool(nations_in_text(text) - {_JP})


def _rule_jp_legal_form(orgs: list[str], title: str, summary: str) -> str | None:
    haystack = "\n".join([*orgs, title, summary])
    if not _JP_LEGAL_FORM.search(haystack):
        return None
    prose = f"{title}\n{summary}"
    if _has_foreign_nation(prose) or _SUBSIDIARY_WORDS.search(prose):
        return None
    return _JP


def _rule_jp_notice_title(category: str, title: str, summary: str) -> str | None:
    if category not in NOTICE_CATEGORIES or not _NOTICE_TITLE.search(title):
        return None
    prose = f"{title}\n{summary}"
    if _has_foreign_nation(prose) or _SUBSIDIARY_WORDS.search(prose):
        return None
    return _JP


def _rule_org_legal_suffix(orgs: list[str]) -> str | None:
    if not orgs:
        return None
    isos: set[str] = set()
    for org in orgs:
        matched = {iso for pat, iso in _LEGAL_SUFFIX_COUNTRY if pat.search(org)}
        if len(matched) != 1:
            return None
        isos |= matched
    return next(iter(isos)) if len(isos) == 1 else None


def _rule_org_history(
    orgs: list[str], title: str, history: Mapping[str, Counter[str]] | None
) -> str | None:
    if not history or len(orgs) != 1 or orgs[0].lower() not in title.lower():
        return None
    counts = history.get(orgs[0].lower())
    if not counts:
        return None
    iso, top = counts.most_common(1)[0]
    if top < ORG_HISTORY_MIN_ARTICLES or top != sum(counts.values()):
        return None
    return iso


def infer_victim_country(
    *,
    article_type: str,
    category: str,
    victim_orgs: Sequence[str],
    title: str,
    summary: str,
    org_history: Mapping[str, Counter[str]] | None = None,
) -> CountryFill | None:
    """被害国が空の速報サイバー記事に、確実な根拠がある場合だけ国を返す。"""
    if article_type != FILL_ARTICLE_TYPE or category not in FILL_CATEGORIES:
        return None
    orgs = _non_empty(victim_orgs)
    candidates: list[CountryFill] = []
    rules: tuple[tuple[str, str | None], ...] = (
        ("jp_legal_form", _rule_jp_legal_form(orgs, title, summary)),
        ("jp_notice_title", _rule_jp_notice_title(category, title, summary)),
        ("org_legal_suffix", _rule_org_legal_suffix(orgs)),
        ("org_history", _rule_org_history(orgs, title, org_history)),
    )
    for name, iso in rules:
        if iso:
            candidates.append(CountryFill(iso=iso, rule=name))
    if not candidates or len({c.iso for c in candidates}) != 1:
        return None
    return candidates[0]


def build_org_history(
    rows: Iterable[tuple[str, str, str | None, int]],
) -> dict[str, Counter[str]]:
    """(article_id, 被害組織名, 被害国 ISO, その記事の被害組織数) の行から組織→国の履歴を作る。

    国が付いていて、被害組織が 1 件だけの記事だけを数える (一覧記事は組織と国の対応が
    曖昧)。同じ (article_id, 組織) は 1 回と数える。
    """
    seen: set[tuple[str, str]] = set()
    history: dict[str, Counter[str]] = {}
    for article_id, org, iso, org_count in rows:
        key = (article_id, org.strip().lower())
        if org_count != 1 or not (iso or "").strip() or not key[1] or key in seen:
            continue
        seen.add(key)
        history.setdefault(key[1], Counter())[str(iso).strip().upper()] += 1
    return history


_HISTORY_SQL = """
SELECT a.article_id AS article_id, e.value AS org, a.victim_country_iso AS iso,
       (SELECT COUNT(*) FROM article_entities e2
         WHERE e2.article_id = a.article_id AND e2.entity_type = 'victim_org') AS org_count
FROM articles a
JOIN article_entities e ON e.article_id = a.article_id AND e.entity_type = 'victim_org'
WHERE a.status = 'posted' AND a.article_type = 'breaking'
  AND a.victim_country_iso IS NOT NULL
  AND COALESCE(a.victim_country_raw, '') NOT LIKE 'inferred:%'
  AND a.created_at >= ?
"""

_cache: dict[str, Any] = {"history": {}, "expires_at": 0.0}


def load_org_history(*, window_days: int = ORG_HISTORY_WINDOW_DAYS) -> dict[str, Counter[str]]:
    """DB から組織→国の履歴を読む (推論行は除く)。"""
    from datetime import UTC, datetime, timedelta

    from src.storage.db_backend import connect

    since = (datetime.now(UTC) - timedelta(days=window_days)).isoformat()
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(_HISTORY_SQL, (since,))
        rows = [
            (str(r["article_id"]), str(r["org"]), r["iso"], int(r["org_count"]))
            for r in cur.fetchall()
        ]
    finally:
        conn.close()
    return build_org_history(rows)


def get_org_history() -> dict[str, Counter[str]]:
    """履歴を TTL 付きで返す。PG 未使用 (tests / dev) や読込失敗では空 (規則 4 が働かないだけ)。"""
    from src.storage.db_backend import is_pg_enabled

    now = time.monotonic()
    if now < float(_cache["expires_at"]):
        history: dict[str, Counter[str]] = _cache["history"]
        return history
    if not is_pg_enabled():
        return {}
    try:
        history = load_org_history()
        _cache.update(history=history, expires_at=now + ORG_HISTORY_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 — 履歴が無くても他の規則で動く (欠落側に倒す)
        _log.warning("victim_org_history_load_failed", error=str(exc))
        history = {}
        _cache.update(history=history, expires_at=now + ORG_HISTORY_RETRY_SECONDS)
    return history
