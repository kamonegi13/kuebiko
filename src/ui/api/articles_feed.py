"""記事フィード API (ダッシュボード「記事フィード」widget 群の共通バックエンド)。

1 つの柔軟な read-only endpoint で、category / feed / channel / importance / search /
時間窓 によるフィルタを提供する。frontend 側は preset config (脆弱性 / 特定サイト /
ニュースサマリー / ヘッドライン 等) でこの endpoint を呼び分ける (DRY)。

書込みは一切しない → READ_ONLY instance でも安全に閲覧可能。
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Query, Request

from src.cti.diamond_model import SOCIO_POLITICAL_INTENTS
from src.logging_config import get_logger
from src.search.models import SearchFacets
from src.tools.embedding_client import EmbeddingClient

_log = get_logger(__name__)

articles_feed_api = APIRouter(prefix="/api/v1", tags=["articles"])

# embedder 未解決を表す sentinel (None=「解決済だが embedding 無効」と区別する)。
_EMBEDDER_UNSET = object()
# semantic search の結果整形で返す summary の最大長 (記事フィードと揃える)。
_SEMANTIC_SUMMARY_CHARS = 300

# K2 逆引き pivot で許可する entity_type (任意文字列を SQL に流さない)。
_PIVOT_ENTITY_TYPES = {
    "actor",
    "actor_provisional",  # Actor Recall Layer: 暗定 (未承認) 新興アクター — 即可視・確定と別 type
    "cve",
    "malware_family",
    "tool",
    "ttp",
    "ioc_ip",
    "ioc_domain",
    "ioc_sha256",
    "ioc_sha1",
    "ioc_md5",
    "ioc_url",
}
# related entity の表示順 (actor / malware を上位に)。
_PIVOT_RELATED_TYPE_ORDER = [
    "actor",
    "actor_provisional",
    "malware_family",
    "cve",
    "tool",
    "ttp",
    "ioc_domain",
    "ioc_ip",
    "ioc_url",
    "ioc_sha256",
    "ioc_sha1",
    "ioc_md5",
]
_PIVOT_RELATED_PER_TYPE = 20

# UI から受け取れる category は許可リストで縛る (任意文字列を SQL に流さない)。
_ALLOWED_CATEGORIES = {
    "vulnerability",
    "advisory",
    "breach",
    "incident",
    "malware",
    "apt",
    "apt_leak",
    "phishing",
    "policy",
    "geopolitical",
    "research",
    "recap",
    "other",
}
# grok_daily は廃止済 ch だが、過去記事の履歴フィルタ用に許容し続ける
_ALLOWED_CHANNELS = {"alert", "brief", "watch", "japan_watch", "grok_daily"}
_ALLOWED_IMPORTANCE = {"high", "medium", "low"}
# 「脆弱性」preset 用の合成カテゴリ (複数 category をまとめる)。
_CATEGORY_GROUPS: dict[str, list[str]] = {
    "vuln": ["vulnerability", "advisory"],
    "threat": ["malware", "apt", "apt_leak", "phishing"],
    "incident_breach": ["incident", "breach"],
}
_MAX_SUMMARY_CHARS = 600


def _parse_since_iso(raw: str | None) -> datetime | None:
    """W2: 「前回確認以降」カーソル (ISO 絶対時刻) を tz-aware datetime に。不正値は無視。"""
    if not raw or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _build_facets(  # noqa: PLR0913
    *,
    importance: str | None,
    category: str | None,
    feed: str | None,
    channel: str | None,
    cve: str | None,
    malware: str | None,
    intent: str | None,
    since_hours: int,
    status: str | None,
    pir: str | None = None,
    actor: str | None = None,
    affected_vendor: str | None = None,
    since_iso: str | None = None,
    body: str | None = None,
) -> SearchFacets:
    """UI facet (生の query param) を正規化して ``SearchFacets`` にまとめる。

    browse (``/articles``) と検索 (``/search``) の両方がこの 1 箇所で facet を
    解決する → 同じ語彙・同じ正規化 (importance「medium=以上」/ category グループ /
    cve・malware・pir・actor の entity 写像 / affected_vendor の CVE 逆引き(vendor/product) /
    不正値の無視) を共有し、browse と search でまったく同じ絞り込みが効く。
    """
    cat_single: str | None = None
    cat_in: tuple[str, ...] = ()
    if category:
        if category in _CATEGORY_GROUPS:
            cat_in = tuple(_CATEGORY_GROUPS[category])
        elif category in _ALLOWED_CATEGORIES:
            cat_single = category
        # 不正値は無視 (フィルタ無効化、fail-open ではない)

    # importance: "high"=high のみ / "medium"=medium 以上 (medium+high) / "low"=low のみ。
    imp_single: str | None = None
    imp_in: tuple[str, ...] = ()
    if importance == "medium":
        imp_in = ("medium", "high")
    elif importance in _ALLOWED_IMPORTANCE:
        imp_single = importance

    chan = channel if channel in _ALLOWED_CHANNELS else None

    # entity タグ絞り込み (article_entities)。各 facet を AND 合成 (filter 間 AND / 値内 OR)。
    entity_filters: list[tuple[str, tuple[str, ...]]] = []
    if cve and cve.strip():
        entity_filters.append(("cve", (cve.strip().upper(),)))
    if malware and malware.strip():
        entity_filters.append(("malware_family", (malware.strip(),)))
    if pir and pir.strip():
        entity_filters.append(("pir", (pir.strip(),)))
    if actor and actor.strip():
        entity_filters.append(("actor", (actor.strip(),)))
    if affected_vendor and affected_vendor.strip():
        # affected (vendor または product) → 該当 CVE 群 → ('cve', [...]) として entity 合成。
        # 1 入力で fortinet (vendor) / fortios (product) どちらでも exposure 絞り込みが効く。
        from src.tools import nvd_client

        cves = nvd_client.cves_for_affected(affected_vendor.strip())
        # 指定したが該当 CVE 無し → 空 IN は全件化するため番兵で 0 件にする。
        entity_filters.append(("cve", tuple(cves) if cves else ("__no_cve_for_vendor__",)))

    intent_filter = intent if intent in SOCIO_POLITICAL_INTENTS and intent != "unknown" else None
    # 本文由来の facet: "stump"(切り株=全文未取得) / "full"(全文取得済) のみ許可 (不正値は無視)。
    body_filter = body if body in ("stump", "full") else None
    # W2: 絶対 since (「前回確認」カーソル) があれば優先。無ければ since_hours の相対窓。
    since = _parse_since_iso(since_iso)
    if since is None and since_hours > 0:
        since = datetime.now(UTC) - timedelta(hours=since_hours)

    return SearchFacets(
        importance=imp_single,
        importance_in=imp_in,
        category=cat_single,
        category_in=cat_in,
        feed_title=feed or None,
        posted_channel=chan,
        socio_political_intent=intent_filter,
        entity_filters=tuple(entity_filters),
        since=since,
        status=status or None,
        body_source=body_filter,
    )


@articles_feed_api.get("/articles")
async def list_articles_feed(  # noqa: PLR0913
    request: Request,
    importance: str | None = Query(default=None),
    category: str | None = Query(default=None),
    feed: str | None = Query(default=None),
    channel: str | None = Query(default=None),
    search: str | None = Query(default=None),
    malware: str | None = Query(default=None),
    cve: str | None = Query(default=None),
    intent: str | None = Query(default=None),
    pir: str | None = Query(default=None),
    actor: str | None = Query(default=None),
    affected_vendor: str | None = Query(default=None),
    body: str | None = Query(default=None),
    status: str = Query(default="posted"),
    since_hours: int = Query(default=0, ge=0, le=24 * 90),
    since: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=200),
    include_summary: bool = Query(default=False),
) -> dict[str, Any]:
    """記事を柔軟なフィルタで返す (新しい順)。

    - ``category`` は単一 category か合成グループ (vuln / threat / incident_breach)。
    - ``pir`` は PIR-persist、``actor`` は脅威アクター (canonical id) による絞り込み。
      ``affected_vendor`` は NVD CPE 逆引きで「その vendor/product の脆弱性に言及する記事」を
      絞り込む (exposure)。
    - ``since_hours`` > 0 で時間窓を限定 (0=全期間)。``since`` (ISO 絶対時刻) があれば優先し、
      「前回確認以降の新着」(W2) を created_at で絞り込む。
    - ``include_summary`` で LLM 要約も返す (重いので既定 off)。
    """
    repo = request.app.state.repo

    facets = _build_facets(
        importance=importance,
        category=category,
        feed=feed,
        channel=channel,
        cve=cve,
        malware=malware,
        intent=intent,
        pir=pir,
        actor=actor,
        affected_vendor=affected_vendor,
        since_hours=since_hours,
        since_iso=since,
        status=status,
        body=body,
    )
    term = search.strip() if search and search.strip() else None

    articles = repo.list_articles(
        **facets.to_query_kwargs(),
        search=term,
        limit=limit,
    )

    # malware チップ表示用に entity を batch fetch (N+1 回避)
    malware_map = repo.entity_values_by_article([a.article_id for a in articles], "malware_family")

    return {
        "articles": [
            {
                "id": a.id,
                "article_id": a.article_id,
                "title": a.title,
                "url": a.url,
                "feed_title": a.feed_title,
                "importance": a.importance,
                "category": a.category,
                "posted_channel": a.posted_channel,
                "victim_sector": a.victim_sector_canonical,
                "victim_country": a.victim_country_iso,
                "socio_political_intent": a.socio_political_intent,
                "intent_confidence": a.intent_confidence,
                "technical_axis_summary": a.technical_axis_summary,
                "malware_families": malware_map.get(a.article_id, []),
                "summary": (a.summary or "")[:_MAX_SUMMARY_CHARS] if include_summary else None,
                "published_at": a.published_at.isoformat() if a.published_at else None,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in articles
        ],
        "count": len(articles),
    }


@articles_feed_api.get("/affected-vendors")
async def affected_vendor_options(request: Request) -> dict[str, Any]:  # noqa: ARG001
    """affected facet の autocomplete 候補 (NVD cache 由来の全 vendor + product)。read-only。

    cache に出現する vendor/product のみ = 自コーパスの CVE に紐づくもの (warming で増える)。
    """
    from src.tools import nvd_client

    return {"vendors": nvd_client.all_affected()}


@articles_feed_api.get("/actor-options")
async def actor_options(request: Request) -> dict[str, Any]:  # noqa: ARG001
    """actor facet の選択肢 (canonical id → 表示名)。read-only。

    actor registry の curated 語彙。記事の entity_type='actor' は canonical id で保存される
    ため value=id / label=name。
    """
    from src.ui.services.threat_operations import actor_canonical_lookup

    lookup = actor_canonical_lookup()
    actors = sorted(
        ({"id": aid, "name": name} for aid, name in lookup.items()),
        key=lambda a: a["name"].lower(),
    )
    return {"actors": actors}


@articles_feed_api.get("/feed-options")
async def feed_options(request: Request, days: int = 90) -> dict[str, Any]:  # noqa: ARG001
    """情報源 (feed) facet の選択肢を**実データから**返す。read-only。

    購読ソース一覧 (subscriptions) 由来だと Grok のような購読外の取込経路
    (grok_email 等) が選択肢に現れず絞り込めなかった (2026-08-15 利用者指摘)。
    記事に実在する feed_title を数え、多い順に返す。
    """
    from src.storage.db_backend import connect, translate_sql

    window_days = max(1, min(int(days), 365))
    conn = connect()
    rows = conn.execute(
        translate_sql(
            "SELECT feed_title, COUNT(*) AS n FROM articles "
            "WHERE feed_title IS NOT NULL AND feed_title <> '' "
            "AND created_at > datetime('now', ?) "
            "GROUP BY feed_title ORDER BY n DESC, feed_title ASC"
        ),
        (f"-{window_days} days",),
    ).fetchall()
    return {
        "feeds": [{"title": str(r[0]), "count": int(r[1])} for r in rows],
        "window_days": window_days,
    }


@articles_feed_api.get("/pivot")
async def entity_pivot(
    request: Request,
    entity_type: str = Query(...),
    value: str = Query(...),
    since_hours: int = Query(default=0, ge=0, le=24 * 365),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """逆引き pivot (Phase 2 K2)。

    1 つの entity (IOC / CVE / actor / malware 等) を参照する記事と、それらに **共起する
    entity** (特に actor) を返す。IOC→actor 帰属や infrastructure 相関の起点になる。
    既存 ``list_articles`` (entity filter) + ``count_entities_for_articles`` の組合せ。read-only。
    """
    et = entity_type.strip().lower()
    if et not in _PIVOT_ENTITY_TYPES:
        raise HTTPException(
            status_code=400, detail=f"対応していない entity_type です: {entity_type}"
        )
    val = value.strip()
    if not val:
        raise HTTPException(status_code=400, detail="value は必須")

    repo = request.app.state.repo
    since = datetime.now(UTC) - timedelta(hours=since_hours) if since_hours > 0 else None
    # value は list_articles 内で LOWER 完全一致
    articles = repo.list_articles(
        entity_type=et,
        entity_value=val.lower(),
        since=since,
        limit=limit,
    )
    article_ids = [a.article_id for a in articles]
    related_raw = repo.count_entities_for_articles(article_ids) if article_ids else {}

    related: list[dict[str, Any]] = []
    for t in _PIVOT_RELATED_TYPE_ORDER:
        vc = related_raw.get(t)
        if not vc:
            continue
        items = [
            {"value": v, "count": c}
            for v, c in sorted(vc.items(), key=lambda kv: (-kv[1], kv[0]))
            # query entity 自身は related から除外
            if not (t == et and v.lower() == val.lower())
        ]
        if items:
            related.append({"type": t, "items": items[:_PIVOT_RELATED_PER_TYPE]})

    return {
        "entity": {"type": et, "value": val},
        "article_count": len(articles),
        "articles": [
            {
                "article_id": a.article_id,
                "title": a.title,
                "url": a.url,
                "importance": a.importance,
                "category": a.category,
                "posted_channel": a.posted_channel,
                "socio_political_intent": a.socio_political_intent,
                "intent_confidence": a.intent_confidence,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in articles
        ],
        "related": related,
    }


def _discord_message_url(
    guild_id: str, channel_id: str | None, message_id: str | None
) -> str | None:
    """Discord メッセージへの deep-link を組み立てる (Phase 2 K4)。

    guild_id + channel_id + message_id が揃ったときのみ URL を返す。
    1 つでも欠ければ None (記事 URL へのフォールバックは呼び出し側が決める)。
    """
    if guild_id and channel_id and message_id:
        return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
    return None


def _days_between(later_iso: str | None, earlier_iso: str | None) -> int | None:
    """later - earlier の日数 (時間軸レイヤ b/c の報道ラグ / dwell 算出)。

    どちらかが無い / 解析不能なら None。負値も返す (UI 側で表示判断)。
    """
    if not later_iso or not earlier_iso:
        return None
    try:
        return (date.fromisoformat(later_iso[:10]) - date.fromisoformat(earlier_iso[:10])).days
    except ValueError:
        return None


@articles_feed_api.get("/articles/{article_id:path}")
async def get_article_detail(request: Request, article_id: str) -> dict[str, Any]:
    """1 記事の全 enrichment を返す deep-view (Phase 2 K4)。

    body / summary / Diamond (intent + technical) / victim / PMESII-PT 8 軸 /
    editorial_stance に加え、article_entities (actor / cve / malware / IOC / TTP) を
    type 別に束ねて返す。Discord 投稿済なら deep-link も付ける。read-only。
    """
    aid = article_id.strip()
    if not aid:
        raise HTTPException(status_code=400, detail="article_id は必須")

    repo = request.app.state.repo
    a = repo.get_article(aid)
    if a is None:
        raise HTTPException(status_code=404, detail=f"article が見つかりません: {aid}")
    # body は ArticleRecord に含めない設計 (list 経路を軽量に保つ) ため別取得。
    body = repo.get_article_body(aid)

    # 主題アクター (id → 表示名) を辞書解決 (UI は id でなく canonical を出す)。
    # 言及 (entity actor) との役割分離表示に使う (B1、NSA 混同の解消)。
    from src.cti.actor_normalizer import load_actor_aliases as _load_aliases

    _subj_ids = [s for s in (a.subject_actor_ids or "").split(",") if s.strip()]
    _registry = _load_aliases()
    subject_actors: list[dict[str, str]] = []
    for sid in _subj_ids:
        _entry = _registry.by_id(_registry.resolve_actor_id(sid))
        subject_actors.append({"id": sid, "label": _entry.canonical if _entry else sid})

    # entity を type 別に束ねる (count は単一記事なので捨て、表示順だけ整える)。
    ent_raw = repo.count_entities_for_articles([aid])
    entities: list[dict[str, Any]] = []
    seen_types: set[str] = set()
    ordered_types = [*_PIVOT_RELATED_TYPE_ORDER, *sorted(ent_raw.keys())]
    for t in ordered_types:
        if t in seen_types or t not in ent_raw:
            continue
        seen_types.add(t)
        values = sorted(ent_raw[t].keys())
        entry: dict[str, Any] = {"type": t, "values": values}
        # Phase 2.5 K5b: CVE には NVD CVSS を付与。2026-06-20: affected vendor/product (CPE)
        # も付与し「自スタックは影響を受けるか」(exposure) を記事から見えるようにする。
        if t == "cve":
            from src.tools.nvd_client import get_affected, get_cvss

            cvss_map = {}
            affected_map = {}
            for v in values:
                info = get_cvss(v)
                if info:
                    cvss_map[v] = {"score": info[0], "severity": info[1]}
                vendors, products = get_affected(v)
                if vendors or products:
                    affected_map[v] = {"vendors": vendors, "products": products}
            if cvss_map:
                entry["cvss"] = cvss_map
            if affected_map:
                entry["affected"] = affected_map
        entities.append(entry)

    from src.config_loader import load_app_config

    guild_id = (load_app_config().discord_guild_id or "").strip()
    discord_url = _discord_message_url(guild_id, a.discord_channel_id, a.discord_message_id)

    # Phase 5: 紐づく memo/bookmark を同梱 (note editor の初期値)。
    from src.ui.api.notes import note_to_dict

    note_rec = repo.get_article_note(aid)
    note = note_to_dict(note_rec) if note_rec else None

    return {
        "article": {
            "article_id": a.article_id,
            "title": a.title,
            "url": a.url,
            "feed_title": a.feed_title,
            "importance": a.importance,
            "category": a.category,
            "status": a.status,
            "posted_channel": a.posted_channel,
            # flow Phase 3: 「なぜこのチャンネルか」(投稿先決定の監査情報)。
            "routing_rule_id": a.routing_rule_id,
            "routing_reason": a.routing_reason,
            "summary": a.summary,
            "body": body,
            # 本文オンデマンド日本語訳キャッシュ (2026-07-25)。未訳なら null —
            # UI は「日本語訳」ボタンを出し、POST /articles/{id}/translate で生成する。
            "body_ja": repo.get_article_body_ja(aid),
            # 本文完全性 (2026-07-27): body の由来と全文取得失敗理由 (UI が「抜粋のみ」を明示)。
            "body_source": a.body_source,
            "extraction_failure_reason": a.extraction_failure_reason,
            # 主題アクター層 (2026-07-17): 「記事の主語」= 攻撃主体。言及 (entity actor) と分離し、
            # UI で役割三分割 (主題 / 言及された組織 / 技術指標) 表示する (NSA 混同の構造的解消)。
            # subject_actor_source = 'title'|'llm'|'feed'|'none' (None=未評価 / none=主題なし)
            "subject_actor_ids": _subj_ids,
            "subject_actors": subject_actors,
            "subject_actor_source": a.subject_actor_source,
            "subject_actor_confidence": a.subject_actor_confidence,
            # 主題判定の根拠文 (2026-08-13 可視化)。「正しい未帰属」を取りこぼしと
            # 区別できるようにする (ConsentFix v3 事案)。title 層確定時は表示しない
            # (根拠は LLM 層の判定説明のため) — 出し分けは frontend。
            "subject_actor_rationale": a.subject_actor_rationale,
            # 記事タイプ (breaking/advisory/recap/…、judgment_classifier 由来)。
            "article_type": a.article_type,
            "victim_sector": a.victim_sector_canonical,
            "victim_sector_raw": a.victim_sector_raw,
            "victim_country": a.victim_country_iso,
            "victim_country_raw": a.victim_country_raw,
            "socio_political_intent": a.socio_political_intent,
            "intent_confidence": a.intent_confidence,
            "socio_political_rationale": a.socio_political_rationale,
            "technical_axis_summary": a.technical_axis_summary,
            "remediation": a.remediation,
            "editorial_stance": a.editorial_stance,
            "pmesii": {
                "p": a.pmesii_p,
                "m": a.pmesii_m,
                "e": a.pmesii_e,
                "s": a.pmesii_s,
                "i_infra": a.pmesii_i_infra,
                "i_cyber": a.pmesii_i_cyber,
                "p_env": a.pmesii_p_env,
                "t": a.pmesii_t,
            },
            "discord_message_id": a.discord_message_id,
            "discord_channel_id": a.discord_channel_id,
            "published_at": a.published_at.isoformat() if a.published_at else None,
            "created_at": a.created_at.isoformat() if a.created_at else None,
            # 時間軸レイヤ b/c (2026-06-27): 事象の実発生(検知/公表)日を報道時刻と分離。
            # reporting_lag = 報道日 - 発生日 (大きいほど retrospective/続報)。
            # dwell = 発生(検知)日 - 侵害開始日 (滞留時間)。
            "event_date": a.event_date,
            "event_date_basis": a.event_date_basis,
            "compromise_date": a.compromise_date,
            "reporting_lag_days": _days_between(
                a.published_at.date().isoformat() if a.published_at else None, a.event_date
            ),
            "dwell_days": _days_between(a.event_date, a.compromise_date),
        },
        "discord_url": discord_url,
        "entities": entities,
        "note": note,
    }


def _resolve_embedder(request: Request) -> EmbeddingClient | None:
    """app.state にキャッシュした embedder を返す (Phase 2 K1)。

    - 初回呼出時に ``.env`` の ``OLLAMA_EMBED_MODEL`` から構築し app.state に cache。
    - 未設定なら None を cache (毎回 config を読み直さない)。
    - テストでは ``app.state.embedder`` に fake を直接 set して差し替え可能。
    """
    state = request.app.state
    cached = getattr(state, "embedder", _EMBEDDER_UNSET)
    if cached is not _EMBEDDER_UNSET:
        return cast("EmbeddingClient | None", cached)

    from src.config_loader import load_app_config
    from src.tools.embedding_client import OllamaEmbeddingClient
    from src.tools.model_tiers import resolve_embedding_model

    cfg = load_app_config()
    model = resolve_embedding_model().strip()
    embedder: EmbeddingClient | None = None
    if not model:
        _log.warning("semantic_search_disabled", reason="OLLAMA_EMBED_MODEL not set")
    else:
        try:
            embedder = OllamaEmbeddingClient(
                base_url=cfg.ollama_base_url,
                model=model,
                query_prefix=cfg.ollama_embed_query_prefix,
            )
        except Exception as e:  # noqa: BLE001
            _log.warning("semantic_search_embedder_init_failed", error=str(e))
            embedder = None
    state.embedder = embedder
    return embedder


@articles_feed_api.get("/semantic-search")
async def semantic_search(
    request: Request,
    query: str = Query(..., min_length=2),
    limit: int = Query(default=20, ge=1, le=100),
    since_hours: int = Query(default=0, ge=0, le=24 * 365),
    min_similarity: float = Query(default=0.4, ge=0.0, le=1.0),
) -> dict[str, Any]:
    """意味的類似度で過去記事を検索する (Phase 2 K1)。

    死蔵していた ``article_embeddings`` を read 経路で活用。クエリ文を同一
    embedding モデルで vector 化し、全 article embedding との cosine 上位 K 件を返す。
    substring 検索 (``/articles`` の ``search``) では拾えない言い換え・多言語の
    同一インシデントを過去参照できる。read-only (書込み無し)。
    """
    embedder = _resolve_embedder(request)
    if embedder is None:
        raise HTTPException(
            status_code=503,
            detail="意味検索は無効です (.env の OLLAMA_EMBED_MODEL を設定してください)",
        )

    term = query.strip()
    if not term:
        raise HTTPException(status_code=400, detail="query は必須")

    try:
        response = await embedder.embed(term, kind="query")
    except Exception as e:  # noqa: BLE001
        _log.warning("semantic_search_embed_failed", error=str(e))
        raise HTTPException(
            status_code=502,
            detail=f"クエリの embedding 生成に失敗しました: {type(e).__name__}",
        ) from e

    repo = request.app.state.repo
    hits = repo.find_similar_embeddings(
        list(response.vector),
        model=embedder.model,
        top_k=limit,
        threshold=min_similarity,
        window_hours=since_hours,
    )
    # hit の url を article metadata に解決 (purge 済で article が無い hit は url のみ)。
    by_url = repo.get_articles_by_urls([url for _, url, _ in hits])

    results: list[dict[str, Any]] = []
    for _url_hash, url, sim in hits:
        a = by_url.get(url)
        if a is not None:
            results.append(
                {
                    "article_id": a.article_id,
                    "title": a.title,
                    "url": a.url,
                    "feed_title": a.feed_title,
                    "importance": a.importance,
                    "category": a.category,
                    "posted_channel": a.posted_channel,
                    "socio_political_intent": a.socio_political_intent,
                    "intent_confidence": a.intent_confidence,
                    "summary": (a.summary or "")[:_SEMANTIC_SUMMARY_CHARS] or None,
                    "published_at": a.published_at.isoformat() if a.published_at else None,
                    "created_at": a.created_at.isoformat() if a.created_at else None,
                    "similarity": round(sim, 4),
                }
            )
        else:
            # embedding は残るが article 行が purge された (or 別経路) ケース。
            results.append(
                {
                    "article_id": None,
                    "title": url,
                    "url": url,
                    "similarity": round(sim, 4),
                }
            )

    return {
        "query": term,
        "model": embedder.model,
        "count": len(results),
        "results": results,
    }


_READ_ONLY = os.environ.get("READ_ONLY", "0").strip() in ("1", "true", "yes", "on")


@articles_feed_api.get("/search")
async def unified_search(  # noqa: PLR0913
    request: Request,
    query: str = Query(..., min_length=2),
    mode: str = Query(default="precise"),
    limit: int = Query(default=20, ge=1, le=50),
    importance: str | None = Query(default=None),
    category: str | None = Query(default=None),
    feed: str | None = Query(default=None),
    channel: str | None = Query(default=None),
    cve: str | None = Query(default=None),
    malware: str | None = Query(default=None),
    intent: str | None = Query(default=None),
    pir: str | None = Query(default=None),
    actor: str | None = Query(default=None),
    affected_vendor: str | None = Query(default=None),
    since_hours: int = Query(default=0, ge=0, le=24 * 365),
) -> dict[str, Any]:
    """統合検索 (hybrid retrieval + LLM rerank/planning)。

    - ``mode=quick``: vector + keyword + structured を RRF 融合 (LLM 不使用、~1-2s)。
    - ``mode=precise``: さらに D(クエリ理解)+ C(LLM リランク) で精度を上げる (~25-35s)。
    facet 引数 (importance / category / feed / channel / cve / malware / intent /
    since_hours) を渡すと検索結果に **hard filter を AND 合成** する (browse facet と
    同一語彙)。facet 未指定なら従来通り query のみで検索。
    READ_ONLY instance (公開 mobile) では LLM 濫用防止のため **強制 quick**。read-only。
    """
    m = mode if mode in ("quick", "precise") else "precise"
    if _READ_ONLY:
        m = "quick"  # 公開 instance で外部から LLM を叩かせない

    from src.search.service import search as run_search

    repo = request.app.state.repo
    embedder = _resolve_embedder(request)

    facets = _build_facets(
        importance=importance,
        category=category,
        feed=feed,
        channel=channel,
        cve=cve,
        malware=malware,
        intent=intent,
        pir=pir,
        actor=actor,
        affected_vendor=affected_vendor,
        since_hours=since_hours,
        status="posted",
    )
    # facet 未選択 (status 以外が空) なら None を渡し、従来の挙動 (post-filter 無し) を維持。
    active_facets = None if facets.is_empty() else facets

    llm = None
    if m == "precise":
        from src.config_loader import load_app_config
        from src.tools.model_tiers import Step, build_llm_for

        cfg = load_app_config()
        try:
            llm = build_llm_for(Step.PRECISE_SEARCH, cfg)
        except Exception as e:  # noqa: BLE001
            _log.warning("search_llm_init_failed", error=str(e))
            llm = None

    result = await run_search(
        repo,
        query=query.strip(),
        mode=m,
        embedder=embedder,
        llm=llm,
        facets=active_facets,
        limit=limit,
    )
    return result.model_dump(mode="json")
