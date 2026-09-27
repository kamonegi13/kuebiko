"""事象単位ニュースの read-only API (Tier0 = 匿名で閲覧可)。

設計 SSoT: docs/event_news_design.md。読み手向けの唯一の出口で、以下を守る:

- **生成物と原ソースを構造で区別する** (§3)。本文は「kuebiko が生成」であることを
  レスポンスで明示し、構成記事は**必ず全件**返す (省略しない)。事実行は
  ``source_index`` を持ち、フロントが文末の出典番号として描く
- **裏取りは「独立媒体数 × tier」で返す** (§3-3)。記事数だけを裏取りとして出さない。
  国営・未分類は 3 値で別に返し、**未分類を 0 と見せない**
- GET のみ。readonly instance でも追加ガードなしで動く (Tier0)
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request, Response

from src.cti.source_basis import classify_source_tier
from src.eventnews.fidelity import draft_text, entity_gaps
from src.storage.run_history import RunHistoryRepository
from src.ui.api.articles_feed import RELATED_ENTITY_TYPE_ORDER

_log = structlog.get_logger(__name__)

eventnews_api = APIRouter(prefix="/api/v1/eventnews", tags=["eventnews"])

# 読み手に「これは生成物である」ことを常に伝える注記 (UI がそのまま表示する)
GENERATED_NOTE = "kuebiko が複数媒体の記事から生成した要約であり、原記事そのものではない"

_LIST_LIMIT_MAX = 200


def _repo() -> RunHistoryRepository:
    return RunHistoryRepository()


def _version_payload(repo: RunHistoryRepository, item_id: str) -> dict[str, Any] | None:
    versions = repo.list_event_versions(item_id)
    if not versions:
        return None
    latest = versions[0]  # version DESC で返るので先頭が最新
    body = json.loads(latest.body_json) if latest.body_json else {}
    return {
        "version": latest.version,
        "generated_at": latest.generated_at.isoformat(),
        "model": latest.model,
        "headline": latest.headline,
        "bluf": body.get("bluf", ""),
        # 要点は公開面と同じものを返す (同一の版を読んでいるので中身は共通)。
        # 2026-08-26 より前の版には無いので、既定は空配列。
        "key_points": body.get("key_points", []),
        "facts": body.get("facts", []),
        "discrepancies": body.get("discrepancies", []),
        "caveats": body.get("caveats", []),
        "unknowns": body.get("unknowns", []),
        # 関門が黙って落とした量を読み手にも見せる (落下率の常設監視、§9)
        "dropped_lines": latest.dropped_lines,
        # 元記事の固有情報 (CVE・版・数値・ラテン文字の固有名) のうち要約に無いもの (2026-09-26)。
        # 点数ではなく **欠けたものの一覧** を見せる — 1 本の網羅率は分母で揺れるため
        "fidelity": _fidelity_payload(repo, item_id, latest),
        "resolved_ids": latest.repaired_ids,
        "history": [
            {"version": v.version, "generated_at": v.generated_at.isoformat()} for v in versions
        ],
    }


def _fidelity_payload(
    repo: RunHistoryRepository, item_id: str, latest: Any
) -> dict[str, Any] | None:
    """構成記事から抽出済みの固有情報 (CVE・アクター・マルウェア・被害組織・製品・ベンダ) のうち、
    要約に書かれていないもの。点数でなく **欠けたものの一覧** を返す (2026-09-26)。
    """
    try:
        ids = [str(m.article_id) for m in repo.list_event_members(item_id)]
        raw = repo.count_entities_for_articles(ids) if ids else {}
        body = json.loads(latest.body_json) if latest.body_json else {}
        checked, missing = entity_gaps(
            {t: list(v) for t, v in raw.items()}, draft_text(body, latest.headline or "")
        )
    except Exception as e:  # noqa: BLE001 — 補助情報の失敗で詳細を落とさない
        _log.warning("eventnews_fidelity_failed", item_id=item_id, error=str(e)[:160])
        return None
    if not checked:
        return None
    return {
        "checked": checked,
        "missing": [{"type": t, "value": v} for t, v in missing],
    }


_PREVIEW_CHARS = 160


def _clip(text: str) -> str:
    """一覧の冒頭 1 行に畳む (本文は生テキストなので改行・連続空白が入る)。"""
    return " ".join(text.split())[:_PREVIEW_CHARS]


def _preview_text(art: Any) -> str:
    """単独報の冒頭。要約が無ければ **本文**から作る。

    事象は被覆のため ``skipped_duplicate`` の記事も構成記事に含むが、重複判定された
    記事は要約 LLM を通らないため ``summary`` が空になる。summary だけを見ていると
    一覧に**本文が一切出ないカード**が並ぶ (2026-08-25 に利用者が発見)。本文は
    取得済みなので、そこから冒頭を出す。
    """
    summary = (getattr(art, "summary", "") or "").strip()
    if summary:
        return _clip(summary)
    # ArticleRecord は body を持たない (一覧で本文を毎回運ぶと重い)。
    # 空を返した分は呼び手が ``get_article_bodies`` でまとめて埋める。
    return ""


def _headlines_and_previews(
    repo: RunHistoryRepository, records: Sequence[Any]
) -> dict[str, tuple[str, str]]:
    """一覧の見出しと冒頭を **一括** で解決する。

    アイテムごとに版と記事を引くと N+1 になる (1 日 ~127 件のペースで事象が増えるため、
    数日で一覧が目に見えて遅くなる)。版は ``latest_event_versions``、記事は
    ``get_articles_by_ids`` で **それぞれ 1 クエリ**にまとめる。
    """
    versions = repo.latest_event_versions([r.state.item_id for r in records])
    need_article = [r for r in records if r.state.item_id not in versions]
    articles = repo.get_articles_by_ids([aid for r in need_article for aid in r.state.member_ids])
    out: dict[str, tuple[str, str]] = {}
    # 要約が空だった単独報。本文で埋めるため article_id を控えておく
    # (``ArticleRecord`` は body を持たないので別途 1 クエリで引く)
    need_body: dict[str, str] = {}
    for r in records:
        latest = versions.get(r.state.item_id)
        if latest is not None:
            body = json.loads(latest.body_json) if latest.body_json else {}
            out[r.state.item_id] = (latest.headline, str(body.get("bluf", ""))[:160])
            continue
        for aid in r.state.member_ids:
            art = articles.get(aid)
            if art is not None:
                preview = _preview_text(art)
                out[r.state.item_id] = (art.title, preview)
                if not preview:
                    need_body[r.state.item_id] = aid
                break
        else:
            out[r.state.item_id] = ("(記事の取得に失敗)", "")

    if need_body:
        bodies = repo.get_article_bodies(list(need_body.values()))
        for item_id, aid in need_body.items():
            text = bodies.get(aid, "")
            if text:
                headline, _ = out[item_id]
                out[item_id] = (headline, _clip(text))
    return out


def _members_payload(repo: RunHistoryRepository, item_id: str) -> list[dict[str, Any]]:
    """構成記事を **全件** 返す (§3-1: 折りたたみは可・省略は不可)。"""
    members = repo.list_event_members(item_id)
    articles = repo.get_articles_by_ids([m.article_id for m in members])
    out: list[dict[str, Any]] = []
    for i, m in enumerate(members, start=1):
        art = articles.get(m.article_id)
        feed_title = art.feed_title if art else ""
        feed_url = getattr(art, "feed_url", "") or "" if art else ""
        account_class = (getattr(art, "account_class", "") or "") if art else ""
        out.append(
            {
                "index": i,
                "article_id": m.article_id,
                "title": art.title if art else "",
                "url": art.url if art else "",
                "feed_title": feed_title,
                # tier は発信者種別を加味する (X の著名研究者と転載 bot を同じにしない)
                "source_tier": classify_source_tier(
                    feed_title or "", feed_url, account_class=account_class
                ),
                "account_class": account_class,
                "published_at": art.published_at if art else None,
                "summary": (art.summary if art else "") or "",
                "joined_at": m.joined_at.isoformat(),
                "contributed_new_facts": bool(m.contributed_new_facts),
            }
        )
    return out


# メタデータの 1 種別あたり表示上限。ttp / ioc は 1 事象で数十件になりうるため、
# 全件返すと読み手が本文に辿り着けない。省いた数は必ず返す (黙って切らない)。
_METADATA_VALUE_CAP = 24

# facet キー → (表示名, 値のラベル解決に使う語彙名)。語彙は既存のものを使い回す
# (sector / country / intent / category は /api/v1/vocabularies が配信済み)。
_FACET_LABELS: dict[str, tuple[str, str]] = {
    "victim_sector": ("被害セクター", "sector"),
    "victim_country": ("被害国", "country"),
    "intent": ("意図", "intent"),
    "category": ("分類", "category"),
    "stance": ("論調", "stance"),
    "channel": ("配信先", "channel"),
}

# PMESII-PT 軸 (ArticleRecord の boolean 列 → frontend の軸キー)。
_PMESII_COLUMNS: tuple[tuple[str, str], ...] = (
    ("pmesii_p", "p"),
    ("pmesii_m", "m"),
    ("pmesii_e", "e"),
    ("pmesii_s", "s"),
    ("pmesii_i_infra", "i_infra"),
    ("pmesii_i_cyber", "i_cyber"),
    ("pmesii_p_env", "p_env"),
    ("pmesii_t", "t"),
)

# 自由記述の判定欄 (記事画面の「意図根拠 / 技術面 / 対処 / 所見」)。事象は複数記事を
# 束ねるため、**1 本にまとめず記事ごとに出典番号付きで並べる** — 要約すると
# 生成物になってしまい、決定論の集計という性質が崩れる。
_TEXT_FIELDS: tuple[tuple[str, str], ...] = (
    ("socio_political_rationale", "意図根拠"),
    ("technical_axis_summary", "技術面"),
    ("remediation", "対処"),
    ("analyst_note", "所見"),
)


def _metadata_payload(repo: RunHistoryRepository, members: Sequence[Any]) -> dict[str, Any]:
    """構成記事から **決定論で** 集約したメタデータ + 判定。

    生成本文とは別物として返す — ここは LLM を通らないので、値の正しさは抽出層の
    品質そのもの。記事画面 (ArticleReadView) の「Diamond / 判定」「エンティティ」
    カードと **同じ行・同じ語彙** を出せるように整えるのがこの関数の責務。

    ラベル自体は ``_FACET_LABELS`` が持つ (表示名の SSoT を frontend に複製しない)。
    """
    ids = [str(m.article_id) for m in members]
    empty: dict[str, Any] = {"entities": [], "subject_actors": [], "facets": [], "judgement": {}}
    if not ids:
        return empty

    raw = repo.count_entities_for_articles(ids)
    ordered = [*RELATED_ENTITY_TYPE_ORDER, *sorted(set(raw) - set(RELATED_ENTITY_TYPE_ORDER))]
    groups: list[dict[str, Any]] = []
    for etype in ordered:
        values = raw.get(etype)
        if not values:
            continue
        ranked = sorted(values.items(), key=lambda kv: (-kv[1], kv[0]))
        entry: dict[str, Any] = {
            "type": etype,
            "values": [{"value": v, "articles": n} for v, n in ranked[:_METADATA_VALUE_CAP]],
            "omitted": max(0, len(ranked) - _METADATA_VALUE_CAP),
        }
        if etype == "cve":
            from src.tools.nvd_client import get_affected, get_cvss

            scores: dict[str, Any] = {}
            affected: dict[str, Any] = {}
            for v, _ in ranked[:_METADATA_VALUE_CAP]:
                info = get_cvss(v)
                if info:
                    scores[v] = {"score": info[0], "severity": info[1]}
                vendors, products = get_affected(v)
                if vendors or products:
                    affected[v] = {"vendors": vendors, "products": products}
            if scores:
                entry["cvss"] = scores
            if affected:
                entry["affected"] = affected
        groups.append(entry)

    articles = repo.get_articles_by_ids(ids)
    subject_counts: dict[str, int] = {}
    facet_counts: dict[str, dict[str, int]] = {}
    confidence_counts: dict[str, int] = {}
    pmesii: dict[str, int] = {}
    texts: dict[str, list[dict[str, Any]]] = {}

    for index, aid in enumerate(ids, start=1):
        art = articles.get(aid)
        if art is None:
            continue
        for sid in (art.subject_actor_ids or "").split(","):
            if sid.strip():
                subject_counts[sid.strip()] = subject_counts.get(sid.strip(), 0) + 1
        for key, value in (
            ("victim_sector", art.victim_sector_canonical),
            ("victim_country", art.victim_country_iso),
            ("intent", art.socio_political_intent),
            ("category", art.category),
            ("stance", art.editorial_stance),
            ("channel", art.posted_channel),
        ):
            if value:
                # ⚠ 代入文は右辺が先に評価される。``setdefault(...)[v] = facet_counts[key]...``
                # と 1 行で書くと右辺の facet_counts[key] が KeyError になる。
                bucket = facet_counts.setdefault(key, {})
                bucket[value] = bucket.get(value, 0) + 1
        if art.socio_political_intent and art.intent_confidence:
            confidence_counts[art.intent_confidence] = (
                confidence_counts.get(art.intent_confidence, 0) + 1
            )
        for column, axis in _PMESII_COLUMNS:
            if getattr(art, column, False):
                pmesii[axis] = pmesii.get(axis, 0) + 1
        for column, label in _TEXT_FIELDS:
            text = (getattr(art, column, None) or "").strip()
            if text:
                texts.setdefault(label, []).append({"text": text, "source_index": index})

    from src.cti.actor_normalizer import load_actor_aliases

    registry = load_actor_aliases()
    subject_actors = [
        {
            "id": sid,
            "label": (
                entry_.canonical
                if (entry_ := registry.by_id(registry.resolve_actor_id(sid))) is not None
                else sid
            ),
            "articles": n,
        }
        for sid, n in sorted(subject_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]

    def _facet(key: str) -> dict[str, Any] | None:
        vals = facet_counts.get(key)
        if not vals:
            return None
        return {
            "key": key,
            # 表示名と、値のラベル解決に使う語彙名を **backend が指定する**
            # (表示名の SSoT を frontend に複製しない、ui_copy_policy)。
            "label": _FACET_LABELS[key][0],
            "vocab": _FACET_LABELS[key][1],
            "values": [
                {"value": v, "articles": n}
                for v, n in sorted(vals.items(), key=lambda kv: (-kv[1], kv[0]))
            ],
        }

    facets = [f for f in (_facet(k) for k in _FACET_LABELS) if f is not None]
    judgement = {
        # 記事画面の「Diamond / 判定」と同じ行を出すための材料。
        "intent": _facet("intent"),
        "intent_confidence": [
            {"value": v, "articles": n}
            for v, n in sorted(confidence_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "texts": [{"label": label, "items": items} for label, items in texts.items()],
        "stance": _facet("stance"),
        "victim_sector": _facet("victim_sector"),
        "victim_country": _facet("victim_country"),
        "channel": _facet("channel"),
        "category": _facet("category"),
        "pmesii": [
            {"axis": a, "articles": n}
            for a, n in sorted(pmesii.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
    }
    return {
        "entities": groups,
        "subject_actors": subject_actors,
        "facets": facets,
        "judgement": judgement,
    }


def _corroboration_payload(members: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """裏取りの内訳 = **媒体単位** の tier 分布。

    「独立 3 媒体」だけでは、ニュース媒体 3 社の裏取りなのか X の転載 bot 3 件なのかが
    区別できない。tier は発信者種別 (Grok の account_class) を加味して決まるので、
    著名研究者の一次情報は 'research'、アグリゲータは 'social' として現れる。

    記事数ではなく **媒体数** で数える (同一媒体の連投を裏取りに数えない、§3-3)。
    """
    by_media: dict[str, str] = {}
    for m in members:
        key = str(m.get("feed_title") or m.get("url") or m.get("article_id") or "")
        if key:
            by_media[key] = str(m.get("source_tier") or "unknown")
    counts: dict[str, int] = {}
    for tier in by_media.values():
        counts[tier] = counts.get(tier, 0) + 1
    return [
        {"tier": t, "media": n} for t, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


# 記事側フィルタを事象へ持ち上げるときに走査する記事の上限。ここを超える結果は
# 「該当が多すぎる」ので、利用者は絞り込みを足す (黙って切らず件数を返す)。
_ARTICLE_SCAN_CAP = 2000

# 意味検索で拾う記事の上限と類似度の下限。
# 実測 (2026-08-25、5 クエリ): 語句検索と重なるのは 0-4 件で、**16-20 件は意味検索に
# しか出ない**。「ランサムウェアによる製造業への攻撃」は語句 0 件 / 意味 20 件だった。
# 言い換えと多言語をまたぐため、事象ニュースの検索でも効く。
_SEMANTIC_TOP_K = 120
_SEMANTIC_MIN_SIMILARITY = 0.45


def _semantic_article_ids(request: Request, term: str, since_hours: int) -> list[str] | None:
    """クエリに意味的に近い記事の id。embedder 未設定なら None (語句検索のみで動く)。

    ⚠ **公開面 (Tier0) からは呼ばない**。匿名に embedding 計算を開放しないため、
    この経路は分析者向けの `/api/v1/eventnews` にだけ置く。
    """
    from src.ui.api.articles_feed import _resolve_embedder

    embedder = _resolve_embedder(request)
    if embedder is None or not term:
        return None

    # この endpoint は同期 (def) なので threadpool の worker で走る。worker には
    # event loop が無いので asyncio.run で完結させてよい (loop は塞がない)。
    import asyncio

    try:
        response = asyncio.run(embedder.embed(term, kind="query"))
    except Exception as e:  # noqa: BLE001 — 意味検索が落ちても語句検索は返す
        _log.warning("eventnews_semantic_embed_failed", error=str(e))
        return None

    repo = request.app.state.repo
    hits = repo.find_similar_embeddings(
        list(response.vector),
        model=embedder.model,
        top_k=_SEMANTIC_TOP_K,
        threshold=_SEMANTIC_MIN_SIMILARITY,
        window_hours=since_hours,
    )
    by_url = repo.get_articles_by_urls([url for _, url, _ in hits])
    return [a.article_id for _, url, _ in hits if (a := by_url.get(url)) is not None]


def _matching_article_ids(request: Request, **filters: Any) -> list[str] | None:
    """記事側フィルタに該当する article_id。フィルタ無指定なら None (絞らない)。

    **既存のニュース検索 (`_build_facets` + `repo.list_articles`) をそのまま使う**。
    同じ条件語で違う結果が出ないようにするため、ここで SQL を書き直さない。
    """
    entity_type = (filters.pop("entity_type", None) or "").strip().lower()
    entity_value = (filters.pop("entity_value", None) or "").strip()
    search = (filters.pop("search", None) or "").strip() or None
    since_hours = int(filters.pop("since_hours", 0) or 0)
    active = {k: v for k, v in filters.items() if v}
    if not (active or search or since_hours or (entity_type and entity_value)):
        return None

    repo = request.app.state.repo
    if entity_type and entity_value:
        # pivot は entity 完全一致 (articles_feed の /pivot と同じ経路)
        since = datetime.now(UTC) - timedelta(hours=since_hours) if since_hours > 0 else None
        articles = repo.list_articles(
            entity_type=entity_type,
            entity_value=entity_value.lower(),
            since=since,
            limit=_ARTICLE_SCAN_CAP,
        )
        return [a.article_id for a in articles]

    from src.ui.api.articles_feed import _build_facets

    facets = _build_facets(
        importance=None,
        category=active.get("category"),
        feed=active.get("feed"),
        channel=active.get("channel"),
        cve=active.get("cve"),
        malware=active.get("malware"),
        intent=active.get("intent"),
        pir=active.get("pir"),
        actor=active.get("actor"),
        affected_vendor=active.get("affected_vendor"),
        since_hours=since_hours,
        since_iso=None,
        # 事象は skipped_duplicate も構成記事に含むため status で絞らない
        # (SearchFacets は status=None を「絞らない」と解釈する。"all" は
        #  literal として渡ってしまい 0 件になる)
        status=None,
        body=None,
    )
    articles = repo.list_articles(
        **facets.to_query_kwargs(), search=search, limit=_ARTICLE_SCAN_CAP
    )
    return [a.article_id for a in articles]


@eventnews_api.get("")
def list_event_news(  # noqa: PLR0913
    request: Request,
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    importance: str | None = None,
    search: str | None = None,
    category: str | None = None,
    channel: str | None = None,
    feed: str | None = None,
    actor: str | None = None,
    cve: str | None = None,
    malware: str | None = None,
    intent: str | None = None,
    pir: str | None = None,
    affected_vendor: str | None = None,
    entity_type: str | None = None,
    entity_value: str | None = None,
    since_hours: int = 0,
    min_independent_sources: int = 0,
    has_news: bool | None = None,
    semantic: bool = False,
) -> dict[str, Any]:
    """事象一覧 (新着順)。origin='live' のみ — リプレイ行は返さない。

    **単独記事も返す** (docs/event_news_design.md §14b 案 A)。生成ニュースを持つのは
    複数媒体の事象だけだが、単独記事は原記事の見出し・要約をそのまま同じ枠で読ませる。
    ここを複数媒体に限ると読み手は記事一覧と 2 箇所を読むことになり、事象単位化の
    目的 (読む場所を 1 つにする) を果たさない。

    ``min_independent_sources`` / ``has_news`` は読み手が **自分で** 複数媒体報や
    統合済みだけに絞るための軸 (既定は絞らない)。記事側には無い事象固有の facet。
    """
    repo = _repo()
    statuses = [s.strip() for s in status.split(",")] if status else None
    wanted = [i.strip() for i in importance.split(",")] if importance else None
    # 記事側の絞り込みは **既存のニュース検索と同じ経路** で解決する
    # (意味論を二重化しない — 2026-08-24 の「評価と本番で取得が分かれると挙動が
    # 一致しない」の教訓)。該当記事を含む事象だけを返す。
    term = (search or "").strip()
    # 検索語だけは他フィルタと分けて解決する。「生成本文に含む」または
    # 「構成記事に含む」の **OR** で一致とするため (他フィルタは AND のまま)。
    search_item_ids = repo.search_event_versions(term) if term else None
    search_member_ids = (
        _matching_article_ids(request, search=term, since_hours=since_hours) if term else None
    )
    # 意味検索は語句検索と **OR** で足す (言い換え・多言語を拾うのが目的で、
    # 語句一致を狭めるためではない)。embedder 未設定なら黙って語句検索のみ。
    if semantic and term:
        semantic_ids = _semantic_article_ids(request, term, since_hours)
        if semantic_ids:
            search_member_ids = list(dict.fromkeys([*(search_member_ids or []), *semantic_ids]))
    member_ids = _matching_article_ids(
        request,
        category=category,
        channel=channel,
        feed=feed,
        actor=actor,
        cve=cve,
        malware=malware,
        intent=intent,
        pir=pir,
        affected_vendor=affected_vendor,
        entity_type=entity_type,
        entity_value=entity_value,
        since_hours=since_hours,
    )
    # 絞り込みは **LIMIT より前** に効かせる。取得後に filter すると「新着 N 件のうち
    # high のもの」になり、「high の新着 N 件」にならない (遡及構築で 2,000 件規模に
    # なって顕在化: high 絞り込みが数件しか出なくなる)。
    shown = repo.list_event_items(
        origin="live",
        statuses=statuses,
        importances=wanted,
        exclude_merged=True,
        min_independent_sources=max(0, min_independent_sources),
        has_news=has_news,
        member_article_ids=member_ids,
        search_item_ids=search_item_ids,
        search_member_article_ids=search_member_ids,
        limit=min(limit, _LIST_LIMIT_MAX),
        offset=max(0, offset),
    )
    resolved = _headlines_and_previews(repo, shown)
    items = []
    for r in shown:
        headline, preview = resolved[r.state.item_id]
        items.append(
            {
                "id": r.state.item_id,
                "headline": headline,
                "preview": preview,
                "status": r.state.status,
                "change_kind": r.change_kind,
                "importance": r.state.importance,
                "member_count": len(r.state.member_ids),
                # 裏取りは 3 値で返す。member_count を裏取りとして使わせない
                "independent_sources": r.independent_sources,
                "state_media_count": r.state_media_count,
                "unclassified_sources": r.unclassified_sources,
                "best_source_tier": r.best_source_tier,
                "first_reported_at": r.state.first_reported_at.isoformat(),
                "last_reported_at": r.state.last_reported_at.isoformat(),
                "current_version": r.state.current_version,
                "has_news": r.state.current_version > 0,
            }
        )
    return {
        "items": items,
        "note": GENERATED_NOTE,
        # 記事側の走査が上限に当たったか。黙って切ると「これで全部」と誤読される
        # (no silent caps)。UI は「該当が多いので絞り込みを足してください」と出す。
        "scan_capped": any(
            ids is not None and len(ids) >= _ARTICLE_SCAN_CAP
            for ids in (member_ids, search_member_ids)
        ),
    }


def related_payload(
    repo: RunHistoryRepository,
    record: Any,
    *,
    visible: Callable[[Any], bool] | None = None,
) -> dict[str, Any]:
    """「別事象だが関連」(related_to) の親と子。app / 公開面 / 静的書き出しで共通。

    ⭐ 分割は読み手から一覧性を奪う (SafePay の別被害者 11 件が別ページになる) ので、
    同一性を偽らずにここで返す (2026-09-03)。親子は分割の由来 = 決定論のみ。
    ``visible`` は公開面のゲート (非公開の子へのリンクは 404 になるので出さない)。
    """
    parent_rec = repo.get_event_item(record.related_to) if record.related_to else None
    if parent_rec is not None and parent_rec.merged_into:
        parent_rec = None
    children = repo.list_related_events(record.state.item_id)
    if visible is not None:
        parent_rec = parent_rec if parent_rec is not None and visible(parent_rec) else None
        children = [c for c in children if visible(c)]
    shown = ([parent_rec] if parent_rec else []) + children
    if not shown:
        return {"parent": None, "children": []}
    titles = _headlines_and_previews(repo, shown)

    def entry(r: Any) -> dict[str, Any]:
        headline, _ = titles.get(r.state.item_id, ("", ""))
        return {
            "id": r.state.item_id,
            "headline": headline,
            "member_count": len(r.state.member_ids),
            "last_reported_at": r.state.last_reported_at.isoformat(),
        }

    return {
        "parent": entry(parent_rec) if parent_rec else None,
        "children": [entry(c) for c in children],
    }


_BASIS_PREFIX = {"victim": "被害組織", "cve": "CVE", "cap": "道具", "actor": "攻撃者"}


def _basis_label(basis: str) -> str:
    """根拠の内部表記 (``victim:acme``) を画面の文言へ (生の enum を出さない)。"""
    kind, _, value = basis.partition(":")
    label = _BASIS_PREFIX.get(kind)
    if label is None:
        return basis
    if kind == "actor":  # 主題アクターは辞書の id — 正式名で出す
        from src.cti.actor_normalizer import load_actor_aliases

        actor = load_actor_aliases().by_id(value)
        value = actor.canonical if actor is not None else value
    return f"{label}: {value}"


def _first_member_title(repo: Any, item_id: str) -> str:
    """本文 (版) が未生成の事象の見出しの代わり = 構成記事の先頭の見出し。"""
    members = [m.article_id for m in repo.list_event_members(item_id)[:1]]
    arts = repo.get_articles_by_ids(members) if members else {}
    return next((str(a.title or "") for a in arts.values()), "")


@eventnews_api.get("/{item_id}/relations")
def event_news_relations(item_id: str) -> dict[str, Any]:
    """事象から導いた関係 (同じ出来事の関連 / 同じアクター、2026-09-27)。

    関係は表に持たず、その都度計算する (src/eventnews/relations.py)。同じ出来事の系統は分類器
    (relation_model)、同じアクターは信頼できる主題アクターの共有。⚠ 同期 def (初回は全事象を読む、
    以後 30 分キャッシュ)。
    """
    from src.eventnews.relations import RELATION_LABELS, relations_by_event

    repo = _repo()
    rels = relations_by_event(repo).get(item_id, [])
    others = [r.b if r.a == item_id else r.a for r in rels]
    versions = repo.latest_event_versions(others) if others else {}
    items = []
    for r, other in zip(rels, others, strict=True):
        v = versions.get(other)
        items.append(
            {
                "item_id": other,
                "headline": v.headline if v is not None else _first_member_title(repo, other),
                "rel_type": r.rel_type,
                "label": RELATION_LABELS.get(r.rel_type, r.rel_type),
                # 向き: この事象が先 (a) か後 (b) か。包含は a = まとめの側
                "role": "a" if r.a == item_id else "b",
                "basis": [_basis_label(b) for b in r.basis],
                # 同じ出来事の系統の確率 (分類器)。同じアクターは決定論なので出さない
                "confidence": float(r.extra["p"]) if r.rel_type != "same_actor" else None,
            }
        )
    return {"relations": items}


@eventnews_api.get("/{item_id}/stix")
def event_news_stix(item_id: str) -> Response:
    """1 事象の STIX 2.1 bundle (2026-09-27、docs/stix_export.md)。

    事象 = kuebiko が書いた report (見出し・BLUF・要点、事実は出典の記事 id つきで拡張へ) が
    構成記事の report を指す。⚠ 同期 def (記事ごとに DB を読むため)。
    """
    from src.cti.actor_normalizer import load_actor_aliases
    from src.cti.stix.event import build_event_bundle

    b = build_event_bundle(_repo(), load_actor_aliases(), item_id)
    if b is None:
        raise HTTPException(status_code=404, detail="not found")
    safe = "".join(c for c in item_id if c.isalnum() or c in "-_")[:40]
    filename = f"kuebiko_event_{safe}.stix.json"
    return Response(
        content=json.dumps(b, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@eventnews_api.get("/{item_id}")
def get_event_news(item_id: str) -> dict[str, Any]:
    """1 事象の詳細 — 生成本文 + 構成記事 (全件)。"""
    repo = _repo()
    record = repo.get_event_item(item_id)
    if record is None:
        raise HTTPException(status_code=404, detail="not found")
    if record.merged_into:
        raise HTTPException(status_code=404, detail=f"merged into {record.merged_into}")
    return {
        "id": record.state.item_id,
        "status": record.state.status,
        "change_kind": record.change_kind,
        "importance": record.state.importance,
        "independent_sources": record.independent_sources,
        "state_media_count": record.state_media_count,
        "unclassified_sources": record.unclassified_sources,
        "best_source_tier": record.best_source_tier,
        "first_reported_at": record.state.first_reported_at.isoformat(),
        "last_reported_at": record.state.last_reported_at.isoformat(),
        "news": _version_payload(repo, item_id),
        "members": (members_payload := _members_payload(repo, item_id)),
        # 裏取りの内訳 (媒体単位の tier 分布)。「独立 N 媒体」の中身を見せる
        "corroboration": _corroboration_payload(members_payload),
        # 原記事から抽出済みのメタデータ (決定論の集約。生成本文とは別枠で出す)。
        # **members と同じ並び** を渡す — 自由記述の出典番号 [N] を一致させるため。
        "metadata": _metadata_payload(repo, repo.list_event_members(item_id)),
        # 「別事象だが関連」 — 分割の由来リンク (親) と逆引き (子)
        "related": related_payload(repo, record),
        "note": GENERATED_NOTE,
    }
