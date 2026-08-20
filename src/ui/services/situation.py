"""国家中心 情勢ビュー (National Situation Board) の集計サービス (2026-06-22)。

サイバー↔地政学を **国家** で相関する。背骨＝国家、ドリル＝アクター。
- **サイバー面 (攻撃者として)**: その国の APT が **主題 (subject)** の脅威記事
  (actor.nation × articles.subject_actor_ids)。2026-08-20 に言及 (mention) 起点から
  反転 — 言及起点は全国家で水増し (実測 30日窓: ru 348→実態119 / cn 101→31 / us 32→0)。
- **地政学面**: その国が当事者の地政学事象 (entity_type='involved_country' 経由 ← 情勢ブリッジ)。
両面を intent (動機) で色付け、PMESII ドメインを足場 (内訳) に、時間軸で **テンポ** を出す。

LLM 不要・raw SQL。read-only。geo_cyber_map と同じ窓集計パターン。
"""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.cti.category_scopes import CYBER_THREAT_SCOPE
from src.ui.services.geo_cyber_map import _COUNTRIES_YAML, _yaml_display_map

DEFAULT_DB_PATH = Path("data/run_history.db")

# PMESII 7 軸 (足場=ナラティブ見出し)。(canonical id, 表示, 列名)。
# T (Time) 軸は 2026-07-16 に廃止 (時間次元は event_date 系が担う。列は履歴保全で残置)。
_PMESII_AXES: list[tuple[str, str, str]] = [
    ("M", "軍事", "pmesii_m"),
    ("E", "経済", "pmesii_e"),
    ("S", "社会/認知", "pmesii_s"),
    ("P", "政治", "pmesii_p"),
    ("I-infra", "重要インフラ", "pmesii_i_infra"),
    ("I-cyber", "サイバー/情報", "pmesii_i_cyber"),
    ("P-env", "サイバー地形", "pmesii_p_env"),
]
_RECENT_LIMIT = 8
_TOP_ACTORS = 8
_TOP_SECTORS = 8

# 「標的として」レンズが対象とする cyber 攻撃カテゴリ (victim_country で集計)。
# 地政学/政策は除外し、実サイバー事案のみを「その国が受けている脅威」とみなす。
# SSoT: category_scopes (地図の狭義レンズとは意図的に別分母 — 件数は直接比較不可)。
_CYBER_CATS: tuple[str, ...] = CYBER_THREAT_SCOPE

# セレクタの役割グループ: 敵対 (防衛 CTI 中核) / 同盟 (actor 辞書に居るが敵対でない) / その他。
_ADVERSARY_NATIONS = frozenset({"cn", "ru", "kp", "ir"})


def _nation_role(iso: str, actor_nations: set[str]) -> str:
    """国家を役割で分類: home(自国) / adversary / allied / other。"""
    low = iso.lower()
    if low == "jp":  # 自国 = 防衛 CTI の主体。常に最前面に置く
        return "home"
    if low in _ADVERSARY_NATIONS:
        return "adversary"
    if low in actor_nations:  # 脅威アクター辞書に居るが敵対でない = 同盟・関係国
        return "allied"
    return "other"


def _connect(db_path: Path) -> Any:
    from src.storage.db_backend import connect as backend_connect

    con = backend_connect(db_path)
    if hasattr(con, "row_factory"):
        con.row_factory = sqlite3.Row
    return con


def _nation_actors() -> dict[str, list[str]]:
    """nation(ISO 小文字) → その国の actor canonical id 群 (registry 由来)。"""
    from src.cti.actor_normalizer import load_actor_aliases

    out: dict[str, list[str]] = defaultdict(list)
    for a in load_actor_aliases().actors:
        if a.nation:
            out[a.nation.lower()].append(a.id)
    return out


def _article_cols() -> str:
    return (
        "article_id, title, url, importance, socio_political_intent AS intent, "
        "created_at, published_at, event_date, pmesii_m, pmesii_e, pmesii_s, pmesii_p, "
        "pmesii_i_infra, pmesii_i_cyber, pmesii_p_env, pmesii_t"
    )


def _face_rows(con: Any, entity_type: str, values: list[str], since: datetime | None) -> list[Any]:
    """指定 entity (actor 群 or involved_country) に該当する記事行 (DISTINCT) を返す。"""
    vals = [v.lower() for v in values if v]
    if not vals:
        return []
    ph = ",".join("?" for _ in vals)
    sql = (
        f"SELECT {_article_cols()} FROM articles "  # noqa: S608 — placeholder/列は内部固定
        f"WHERE status='posted' AND article_id IN ("
        f"  SELECT article_id FROM article_entities WHERE entity_type=? AND LOWER(value) IN ({ph}))"
    )
    params: list[Any] = [entity_type, *vals]
    if since is not None:
        sql += " AND COALESCE(datetime(published_at), datetime(created_at)) >= datetime(?)"
        params.append(since.isoformat())
    sql += " ORDER BY COALESCE(datetime(published_at), datetime(created_at)) DESC"
    return list(con.execute(sql, tuple(params)).fetchall())


def _attacker_face_rows(con: Any, actor_ids: list[str], since: datetime | None) -> list[Any]:
    """攻撃者面 (主題起点): actor_ids のいずれかが主題 (subject) の脅威記事を返す。

    2026-08-20 反転: 従来は article_entities の **言及 (mention)** を数えていたが、実測
    (30日窓) で全国家に言及混入の水増しが確認された (ru 348→実態119 / cn 101→31 /
    us 32→0)。母集団を articles.subject_actor_ids (主題評価済み) × 脅威スコープ
    (_CYBER_CATS) 起点に再定義し、標的面 (_victim_cyber_face) と対称にする。
    legacy 行 (subject_actor_source IS NULL) は subject_actor_ids も NULL のため
    membership 条件で自動的に落ちる (mention への fallback は意図的に廃止)。
    """
    # actor_ids / subject_actor_ids は同一 registry の canonical id (小文字 snake_case) 由来。
    # _face_rows の defensive LOWER は自由文の言及値向けで、ここでは両辺 registry のため不要。
    ids = [a for a in actor_ids if a]
    if not ids:
        return []
    cats_ph = ",".join("?" for _ in _CYBER_CATS)
    membership = " OR ".join(
        "INSTR(',' || COALESCE(subject_actor_ids,'') || ',', ',' || ? || ',') > 0" for _ in ids
    )
    sql = (
        f"SELECT {_article_cols()}, subject_actor_ids FROM articles "  # noqa: S608 — 列/分岐は内部固定
        f"WHERE status='posted' AND LOWER(category) IN ({cats_ph}) AND ({membership})"
    )
    params: list[Any] = [*(c.lower() for c in _CYBER_CATS), *ids]
    if since is not None:
        sql += " AND COALESCE(datetime(published_at), datetime(created_at)) >= datetime(?)"
        params.append(since.isoformat())
    sql += " ORDER BY COALESCE(datetime(published_at), datetime(created_at)) DESC"
    return list(con.execute(sql, tuple(params)).fetchall())


def _derive_actor_counts(rows: list[Any], actor_ids: frozenset[str]) -> Counter[str]:
    """攻撃者面の top actors を面と同一の母集団 (rows) から構成的に導出する。

    別クエリで数えると面と内訳が乖離しうる (2026-08-20 のバグは gate 有無の乖離が露呈させた)
    ため、必ず _attacker_face_rows の戻り値から導出する。
    """
    from src.cti.subject_gate import split_subject_ids

    counts: Counter[str] = Counter()
    for r in rows:
        for aid in split_subject_ids(r["subject_actor_ids"]):
            if aid in actor_ids:
                counts[aid] += 1
    return counts


def _cyber_mention_rows(con: Any, iso: str, since: datetime | None) -> list[Any]:
    """帰属なしサイバー言及レーン: i_cyber 点灯 × involved_country=iso × actor entity 無し。

    地政学/政策記事に埋もれた『その国に関わるサイバー言及』を、帰属済みサイバー作戦
    (actor.nation レーン) と分離して抽出する。検証 (2026-06-27) で本レーンは ~65% が
    政策・規範・態勢の言及、~35% が実事象。よって『作戦』でなく『サイバー姿勢/政策の
    言及』として扱い、actor 帰属レーンとは混同しない。actor 無し条件で帰属レーンと重複排除。
    """
    lower = iso.lower()
    sql = (
        f"SELECT {_article_cols()} FROM articles "  # noqa: S608 — 列は内部固定
        "WHERE status='posted' AND pmesii_i_cyber=1 "
        "AND article_id IN ("
        "  SELECT article_id FROM article_entities "
        "  WHERE entity_type='involved_country' AND LOWER(value)=?) "
        "AND article_id NOT IN ("
        "  SELECT article_id FROM article_entities WHERE entity_type='actor')"
    )
    params: list[Any] = [lower]
    if since is not None:
        sql += " AND COALESCE(datetime(published_at), datetime(created_at)) >= datetime(?)"
        params.append(since.isoformat())
    sql += " ORDER BY COALESCE(datetime(published_at), datetime(created_at)) DESC"
    return list(con.execute(sql, tuple(params)).fetchall())


def _day(row: Any) -> str | None:
    v = row["published_at"] or row["created_at"]
    if hasattr(v, "strftime"):
        return str(v.strftime("%Y-%m-%d"))
    s = str(v or "")
    return s[:10] if len(s) >= 10 else None


def _event_day(row: Any) -> str | None:
    """事象の実発生日 (event_date) を YYYY-MM-DD で返す。未抽出なら None。

    時間軸トグル (報道時刻↔発生時刻) の発生時刻系列に使う。event_date が無い記事
    (速報の大半) は発生時刻系列に出ない=「不明」(≠無し)。カバレッジで正直に示す。
    """
    try:
        v = row["event_date"]
    except (KeyError, IndexError):
        return None
    if not v:
        return None
    if hasattr(v, "strftime"):
        return str(v.strftime("%Y-%m-%d"))
    s = str(v)
    return s[:10] if len(s) >= 10 else None


def _summarize_face(rows: list[Any]) -> dict[str, Any]:
    """記事行群を intent / PMESII ドメイン / 直近 に集約 (両面共通)。"""
    intents: Counter[str] = Counter()
    domains: Counter[str] = Counter()
    for r in rows:
        it = r["intent"]
        if it and str(it) != "unknown":
            intents[str(it)] += 1
        for axis_id, _label, col in _PMESII_AXES:
            if r[col]:
                domains[axis_id] += 1
    recent = [
        {
            "article_id": r["article_id"],
            "title": r["title"],
            "url": r["url"],
            "importance": r["importance"],
            "intent": r["intent"] if r["intent"] and str(r["intent"]) != "unknown" else None,
            "date": _day(r),
        }
        for r in rows[:_RECENT_LIMIT]
    ]
    axis_label = {a: lbl for a, lbl, _ in _PMESII_AXES}
    return {
        "total": len(rows),
        "intents": [{"intent": k, "count": v} for k, v in intents.most_common()],
        "domains": [
            {"axis": a, "label": axis_label[a], "count": c}
            for a, c in sorted(domains.items(), key=lambda kv: -kv[1])
        ],
        "recent": recent,
    }


def _victim_window_clause(since: datetime | None, params: list[Any]) -> str:
    if since is None:
        return ""
    params.append(since.isoformat())
    return " AND COALESCE(datetime(a.published_at), datetime(a.created_at)) >= datetime(?)"


def _victim_cyber_face(con: Any, iso: str, since: datetime | None) -> dict[str, Any]:
    """標的レンズ: その国を victim とする cyber 攻撃事案 (= 受けている脅威)。

    攻撃元アクター (誰が攻撃しているか) と標的セクターを内訳に持つ。地政学でなく
    実サイバー事案 (_CYBER_CATS) のみ。攻撃者レンズ (actor.nation) と相補。
    """
    cats_ph = ",".join("?" for _ in _CYBER_CATS)
    lower_iso = iso.lower()

    # 1) 被害記事行 (intent/PMESII/直近 の足場)
    rparams: list[Any] = [lower_iso, *[c.lower() for c in _CYBER_CATS]]
    rsql = (
        f"SELECT {_article_cols()}, victim_sector_canonical FROM articles a "  # noqa: S608 — 列は内部固定
        f"WHERE status='posted' AND LOWER(victim_country_iso)=? "
        f"AND LOWER(category) IN ({cats_ph})"
    )
    rsql += _victim_window_clause(since, rparams)
    rsql += " ORDER BY COALESCE(datetime(a.published_at), datetime(a.created_at)) DESC"
    rows = list(con.execute(rsql, tuple(rparams)).fetchall())

    face = _summarize_face(rows)

    # 2) 攻撃元アクター (この国を攻撃している actor)
    # D1 (2026-07-27): 報告/防御機関を除外 + subject-gate (評価済みは主題のみ、legacy は fallback)。
    from src.cti.actor_roles import reporter_org_actor_ids
    from src.cti.subject_gate import subject_gate_clause

    reporter_ids = sorted(reporter_org_actor_ids())
    aparams: list[Any] = [lower_iso, *[c.lower() for c in _CYBER_CATS]]
    asql = (
        "SELECT LOWER(ae.value) AS actor, COUNT(DISTINCT a.article_id) AS n "
        "FROM article_entities ae JOIN articles a ON a.article_id=ae.article_id "
        "WHERE ae.entity_type='actor' AND a.status='posted' "
        f"AND LOWER(a.victim_country_iso)=? AND LOWER(a.category) IN ({cats_ph})"  # noqa: S608
    )
    if reporter_ids:
        reporter_ph = ",".join("?" for _ in reporter_ids)
        asql += f" AND LOWER(ae.value) NOT IN ({reporter_ph})"  # noqa: S608
        aparams.extend(reporter_ids)
    asql += " AND " + subject_gate_clause(
        mention_col="ae.value",
        subject_ids_col="a.subject_actor_ids",
        subject_source_col="a.subject_actor_source",
    )
    asql += _victim_window_clause(since, aparams)
    asql += " GROUP BY LOWER(ae.value) ORDER BY n DESC"
    attacker_counts = [(str(r["actor"]), int(r["n"])) for r in con.execute(asql, tuple(aparams))]

    from src.cti.actor_normalizer import load_actor_aliases

    actor_label = {a.id: a.canonical for a in load_actor_aliases().actors}
    face["actors"] = [
        {"actor_id": aid, "label": actor_label.get(aid, aid), "count": n}
        for aid, n in attacker_counts[:_TOP_ACTORS]
    ]

    # 3) 標的セクター (どの分野が狙われているか)
    sparams: list[Any] = [lower_iso, *[c.lower() for c in _CYBER_CATS]]
    ssql = (
        "SELECT victim_sector_canonical AS sector, COUNT(*) AS n FROM articles a "
        "WHERE status='posted' AND LOWER(victim_country_iso)=? "
        f"AND LOWER(category) IN ({cats_ph}) "  # noqa: S608 — cats は内部固定
        "AND victim_sector_canonical IS NOT NULL AND victim_sector_canonical!=''"
    )
    ssql += _victim_window_clause(since, sparams)
    ssql += " GROUP BY victim_sector_canonical ORDER BY n DESC"
    face["sectors"] = [
        {"sector": str(r["sector"]), "label": str(r["sector"]), "count": int(r["n"])}
        for r in list(con.execute(ssql, tuple(sparams)))[:_TOP_SECTORS]
    ]
    return face


def _nation_label(iso: str) -> str:
    labels = _yaml_display_map(str(_COUNTRIES_YAML))
    return labels.get(iso.upper(), iso.upper())


def list_nations(
    *, window_days: int | None = 90, db_path: Path = DEFAULT_DB_PATH
) -> list[dict[str, Any]]:
    """国家セレクタ用: サイバー(actor.nation) と地政学(involved_country) を持つ国の件数一覧。"""
    since = datetime.now(UTC) - timedelta(days=window_days) if window_days else None
    nat_actors = _nation_actors()
    con = _connect(db_path)
    try:
        cyber: Counter[str] = Counter()
        for nat, actors in nat_actors.items():
            rows = _attacker_face_rows(con, actors, since)
            if rows:
                cyber[nat.upper()] = len(rows)
        geo: Counter[str] = Counter()
        # involved_country は entity なので記事 join で窓を効かせる。
        gsql = (
            "SELECT UPPER(ae.value) AS iso, COUNT(DISTINCT a.article_id) AS n "
            "FROM article_entities ae JOIN articles a ON a.article_id=ae.article_id "
            "WHERE ae.entity_type='involved_country' AND a.status='posted'"
        )
        gparams: list[Any] = []
        if since is not None:
            gsql += " AND COALESCE(datetime(a.published_at), datetime(a.created_at)) >= datetime(?)"
            gparams.append(since.isoformat())
        gsql += " GROUP BY UPPER(ae.value)"
        for r in con.execute(gsql, tuple(gparams)).fetchall():
            geo[str(r["iso"])] = int(r["n"])
        # 標的レンズ: victim_country で cyber 被害件数 (同盟・自国を浮かび上がらせる)。
        target: Counter[str] = Counter()
        cats_ph = ",".join("?" for _ in _CYBER_CATS)
        tsql = (
            "SELECT UPPER(victim_country_iso) AS iso, COUNT(*) AS n FROM articles a "
            "WHERE status='posted' AND victim_country_iso IS NOT NULL "
            f"AND LOWER(category) IN ({cats_ph})"  # noqa: S608 — cats は内部固定
        )
        tparams: list[Any] = [c.lower() for c in _CYBER_CATS]
        tsql += _victim_window_clause(since, tparams)
        tsql += " GROUP BY UPPER(victim_country_iso)"
        for r in con.execute(tsql, tuple(tparams)).fetchall():
            if r["iso"]:
                target[str(r["iso"])] = int(r["n"])
    finally:
        con.close()

    isos = set(cyber) | set(geo) | set(target)
    actor_nations = set(nat_actors)
    out: list[dict[str, Any]] = [
        {
            "iso": iso,
            "label": _nation_label(iso),
            "role": _nation_role(iso, actor_nations),
            "cyber": cyber.get(iso, 0),
            "cyber_target": target.get(iso, 0),
            "geopol": geo.get(iso, 0),
            "total": cyber.get(iso, 0) + target.get(iso, 0) + geo.get(iso, 0),
        }
        for iso in isos
    ]
    out.sort(key=lambda d: (-int(d["total"]), str(d["label"])))
    return out


def situation_by_nation(
    nation: str, *, window_days: int | None = 90, db_path: Path = DEFAULT_DB_PATH
) -> dict[str, Any]:
    """1 国の情勢: サイバー面(APT) + 地政学面(当事国) + テンポ(日次)。"""
    iso = nation.strip().upper()
    since = datetime.now(UTC) - timedelta(days=window_days) if window_days else None
    actors = _nation_actors().get(iso.lower(), [])

    con = _connect(db_path)
    try:
        cyber_rows = _attacker_face_rows(con, actors, since)
        geo_rows = _face_rows(con, "involved_country", [iso], since)
        mention_rows = _cyber_mention_rows(con, iso, since)
        # cyber 面の top APT (件数): 面と同一母集団 (cyber_rows) から構成的に導出する。
        actor_counts = _derive_actor_counts(cyber_rows, frozenset(actors))
        # 標的レンズ: この国が受けている cyber 脅威 (victim_country)。
        cyber_target = _victim_cyber_face(con, iso, since)
    finally:
        con.close()

    from src.cti.actor_normalizer import load_actor_aliases

    actor_label = {a.id: a.canonical for a in load_actor_aliases().actors}

    cyber = _summarize_face(cyber_rows)
    cyber["actors"] = [
        {"actor_id": aid, "label": actor_label.get(aid, aid), "count": n}
        for aid, n in actor_counts.most_common(_TOP_ACTORS)
    ]
    geopol = _summarize_face(geo_rows)
    cyber_mention = _summarize_face(mention_rows)

    # ── テンポ: 日次の cyber vs geopol 件数 (地政学イベント×APT活動の相関) ──
    # 時間軸トグル (2026-06-27): 報道時刻 (published) 系列 と 発生時刻 (event_date) 系列を
    # **両方** 返し、frontend で切替える。発生時刻は dated subset のみ=過去事象に偏る
    # (中央値ラグ ~13ヶ月)。短窓ではほぼ空・長窓で作戦史が出る。カバレッジで「dated N/M」を
    # 正直に示し、非表示の未抽出を「無し」と誤読させない (地図の暗域=不明≠安全 と同型)。
    cyber_day: Counter[str] = Counter()
    geo_day: Counter[str] = Counter()
    cyber_ev_day: Counter[str] = Counter()
    geo_ev_day: Counter[str] = Counter()
    dated = 0
    total = len(cyber_rows) + len(geo_rows)
    for r in cyber_rows:
        d = _day(r)
        if d:
            cyber_day[d] += 1
        ed = _event_day(r)
        if ed:
            dated += 1
            cyber_ev_day[ed] += 1
    for r in geo_rows:
        d = _day(r)
        if d:
            geo_day[d] += 1
        ed = _event_day(r)
        if ed:
            dated += 1
            geo_ev_day[ed] += 1
    now = datetime.now(UTC)
    days_all = set(cyber_day) | set(geo_day)
    if since is not None:
        start = since.date()
    elif days_all:
        start = datetime.fromisoformat(min(days_all)).date()
    else:
        start = now.date()
    buckets: list[str] = []
    cur = start
    while cur <= now.date():
        buckets.append(cur.isoformat())
        cur = cur + timedelta(days=1)
    # 発生時刻系列の窓内合計 (発生日が窓外=古い retrospective は系列に出ない)。
    bucket_set = set(buckets)
    in_window_ev = sum(v for k, v in cyber_ev_day.items() if k in bucket_set) + sum(
        v for k, v in geo_ev_day.items() if k in bucket_set
    )
    tempo = {
        "buckets": buckets,
        "cyber": [cyber_day.get(b, 0) for b in buckets],
        "geopol": [geo_day.get(b, 0) for b in buckets],
        "cyber_event": [cyber_ev_day.get(b, 0) for b in buckets],
        "geopol_event": [geo_ev_day.get(b, 0) for b in buckets],
        # カバレッジ: 報道窓内の記事のうち発生日付き dated / total、うち窓内に着地した数。
        "coverage": {"dated": dated, "total": total, "event_in_window": in_window_ev},
    }

    return {
        "nation": iso,
        "label": _nation_label(iso),
        "window_days": window_days,
        "cyber": cyber,
        "cyber_mention": cyber_mention,
        "cyber_target": cyber_target,
        "geopolitical": geopol,
        "tempo": tempo,
    }
