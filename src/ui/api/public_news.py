"""公開ニュース API (Tier0 = 匿名で読める唯一の読み物)。

**内部の `/api/v1/eventnews` を公開面へ流用しない**。あちらは分析者向けで、原記事の
要約・本文冒頭・判定メタデータまで返す。公開面はそれらを出してはいけないので、
「何を返さないか」を型と関門で固定できるよう別 API として持つ。

守る契約 (`tests/unit/test_public_news_api.py` が固定する):

1. **出版社の本文を返さない** — ``articles.body`` / ``body_ja`` は trafilatura で
   抽出した**原記事そのもの**であり、公開すれば再配布になる。CLAUDE.md §10 が
   robots.txt を無視する根拠を「配信は要約 + 引用 URL のみ」に置いているため、
   ここを破るとその前提ごと崩れる。
   一方 ``articles.summary`` は **kuebiko の LLM が書いた要約**で、§9 が明示的に
   認めている「要約と引用 URL に留める」そのもの。こちらは返してよい
2. **出典を必ず付ける** — 生成本文の有無に関わらず、媒体名と原記事 URL を返す。
   出典の無い項目は返さない
3. **high のみ** — 公開するのは重要度 high の事象だけ (2026-08-25 利用者判断)
4. **自前の重複判定を尊重する** — 全メンバーが ``skipped_duplicate`` で生成本文も
   無い事象は公開しない。dedup が「既に収集済みの重複」と判定したものを単独記事と
   して出すと、同じ事案を二重掲載することになる (実測 43 事象)
5. GET のみ
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException

from src.cti.geocoder import Geocoder
from src.cti.source_basis import classify_source_tier
from src.storage.run_history import RunHistoryRepository

# カテゴリのグループ定義は記事側 facet と **同じものを使う** (グループの中身を
# 2 箇所に持つと必ずずれる)。公開面に出す 4 つだけを ``PUBLIC_CATEGORIES`` で選ぶ。
from src.ui.api.articles_feed import _CATEGORY_GROUPS  # noqa: PLC2701 — 分類の SSoT 共有
from src.ui.services.geo_cyber_map import _COUNTRIES_YAML, _yaml_display_map

public_news_api = APIRouter(prefix="/api/v1/public/news", tags=["public"])

# 公開するのは重要度 high の事象のみ
_PUBLIC_IMPORTANCES = ("high",)
_LIST_LIMIT_MAX = 60

# 公開サイトのカテゴリ。実データの分布 (2026-08-25、公開候補 high) に合わせて 4 つ。
#   地政学 212 / マルウェア・APT 247 / 侵害 290 / 脆弱性 132
# policy(1) や recap/other は件数が僅少なので独立ページを持たない (新着には出る)。
PUBLIC_CATEGORIES: tuple[str, ...] = ("vuln", "incident_breach", "threat", "geopolitical")

# 「注目」= **直近 72 時間で最も多くの独立媒体が報じた事案**。
#
# 実測 (過去 10 日を再現、2026-08-25):
#   新しい順 (旧)          その窓で最大の裏取りを拾えた 3/10 日・平均 4.4 媒体
#   72h 内を媒体数順 (現)  10/10 日・平均 10.0 媒体
# 旧規則は 10 日中 7 日で最大の話題を逃していた (11 媒体の事案がある日に 3 媒体を表示)。
#
# ⚠ これは **表示順**であって重要性の定義ではない。候補は既に PIR → importance で
# high に絞られており、媒体数はその中の並べ替えにしか使わない
# (収集量で重要性を上書きしない: tests/unit/test_burst_boundary.py と同じ趣旨)。
FEATURED_WINDOW_HOURS = 72
# 静かな日に注目が空にならないよう、足りなければ窓を広げる
FEATURED_FALLBACK_HOURS = 24 * 7
FEATURED_COUNT = 3


# 記事 category → 公開カテゴリ key の逆引き (グループ定義から機械的に作る)
_CATEGORY_OF: dict[str, str] = {
    article_category: public_key
    for public_key in PUBLIC_CATEGORIES
    for article_category in _CATEGORY_GROUPS.get(public_key, [public_key])
}


def _dominant_category(articles: dict[str, Any], member_ids: tuple[str, ...]) -> str:
    """事象の代表カテゴリ (構成記事の多数決)。該当が無ければ空文字。

    一覧のバッジに使う。同数のときは ``PUBLIC_CATEGORIES`` の並び順で決める
    (実行ごとに変わらないように)。
    """
    counts: dict[str, int] = {}
    for aid in member_ids:
        art = articles.get(aid)
        key = _CATEGORY_OF.get(str(getattr(art, "category", "") or "")) if art else None
        if key:
            counts[key] = counts.get(key, 0) + 1
    if not counts:
        return ""
    return max(counts, key=lambda k: (counts[k], -PUBLIC_CATEGORIES.index(k)))


def _categories_for(key: str) -> list[str] | None:
    """公開カテゴリ key → 記事 category の集合。未知の値は「絞らない」ではなく None。"""
    if key not in PUBLIC_CATEGORIES:
        return None
    return list(_CATEGORY_GROUPS.get(key, [key]))


GENERATED_NOTE = "kuebiko が複数媒体の報道から生成した要約であり、原記事そのものではない"


def _repo() -> RunHistoryRepository:
    return RunHistoryRepository()


def _is_duplicate_only(articles: dict[str, Any], member_ids: tuple[str, ...]) -> bool:
    """全メンバーが dedup で重複と判定されたか (契約 4)。

    ``skipped_duplicate`` は「既に収集済みの記事と同じ事案」という自前の判定。
    生成本文があるなら複数媒体をまとめた読み物になっているので公開してよい。
    """
    known = [articles[a] for a in member_ids if a in articles]
    if not known:
        return True
    return all(getattr(a, "status", "") == "skipped_duplicate" for a in known)


def _citations(repo: RunHistoryRepository, item_id: str) -> list[dict[str, Any]]:
    """出典 (媒体名 + 原記事 URL)。**原記事の本文・要約は含めない**。"""
    members = repo.list_event_members(item_id)
    articles = repo.get_articles_by_ids([m.article_id for m in members])
    out: list[dict[str, Any]] = []
    for i, m in enumerate(members, start=1):
        art = articles.get(m.article_id)
        if art is None or not art.url:
            continue
        feed_title = art.feed_title or ""
        out.append(
            {
                "index": i,
                "article_id": m.article_id,
                "title": art.title,
                "url": art.url,
                "source": feed_title,
                "source_tier": classify_source_tier(
                    feed_title,
                    getattr(art, "feed_url", "") or "",
                    account_class=(getattr(art, "account_class", "") or ""),
                ),
                "published_at": art.published_at,
            }
        )
    return out


def _featured_records(repo: RunHistoryRepository, common: dict[str, Any]) -> list[Any]:
    """注目枠。直近 72h の中から独立媒体数の多い順に取る。

    静かな日に空にならないよう、足りなければ窓を 7 日へ広げる (それでも
    「最も裏取りのある事案」であることは変わらない)。
    """
    now = datetime.now(UTC)
    latest: list[Any] = []
    for hours in (FEATURED_WINDOW_HOURS, FEATURED_FALLBACK_HOURS):
        latest = repo.list_event_items(
            **common,
            min_independent_sources=2,
            has_news=True,
            since=now - timedelta(hours=hours),
            order_by="corroboration",
            limit=FEATURED_COUNT,
        )
        if len(latest) >= FEATURED_COUNT:
            break
    return latest


def _public_citation(citation: dict[str, Any]) -> dict[str, Any]:
    """公開する出典の形 (内部 id は落とす)。"""
    return {k: v for k, v in citation.items() if k != "article_id"}


@public_news_api.get("")
def list_public_news(
    limit: int = 30,
    offset: int = 0,
    search: str | None = None,
    category: str | None = None,
    country: str | None = None,
    featured: bool = False,
) -> dict[str, Any]:
    """公開ニュース一覧 (high のみ、新しい順)。

    冒頭テキストは統合済みなら生成本文の BLUF、単独報なら **kuebiko が書いた要約**。
    出版社の本文 (``body`` / ``body_ja``) は決して返さない。

    ``country`` は被害国 ISO (地図からの遷移)。``category`` は ``PUBLIC_CATEGORIES``
    のいずれか。``featured`` は「注目」枠で、
    複数媒体が報じ かつ 統合本文を持つ事象に絞る。どちらも **LIMIT より前**に効く。
    """
    # 未知のカテゴリで **絞らない** にすると綴り違いが全件表示になり気付けない。
    # 明示的に弾く (2026-08-25: `?category=policy` が全件を返していた)。
    categories = _categories_for(category) if category else None
    if category and categories is None:
        raise HTTPException(status_code=404, detail="unknown category")

    repo = _repo()
    term = (search or "").strip()
    search_item_ids = repo.search_event_versions(term) if term else None
    common: dict[str, Any] = {
        "origin": "live",
        "importances": list(_PUBLIC_IMPORTANCES),
        "exclude_merged": True,
        # **LIMIT より前**に効かせる (取得後の間引きはページングを壊す)
        "exclude_duplicate_only": True,
        "member_categories": categories,
        # 地図から国を選んだときの絞り込み (被害国 ISO)
        "member_country": (country or "").strip().upper() or None,
        "search_item_ids": search_item_ids,
    }
    if featured:
        records = _featured_records(repo, common)
    else:
        records = repo.list_event_items(
            **common,
            limit=min(max(limit, 1), _LIST_LIMIT_MAX),
            offset=max(0, offset),
        )
    versions = repo.latest_event_versions([r.state.item_id for r in records])
    articles = repo.get_articles_by_ids([a for r in records for a in r.state.member_ids])
    items: list[dict[str, Any]] = []
    for r in records:
        citations = _citations(repo, r.state.item_id)
        if not citations:
            continue  # 出典を示せない項目は公開しない (契約 2)
        latest = versions.get(r.state.item_id)
        if latest is not None:
            body = json.loads(latest.body_json) if latest.body_json else {}
            headline = latest.headline
            summary = str(body.get("bluf", ""))
        else:
            # 単独報: 見出しは原題、要約は **kuebiko が書いたもの** (契約 1)。
            # ``ArticleRecord`` は body を持たないので、ここから本文が漏れることはない。
            headline = citations[0]["title"]
            first = articles.get(str(citations[0]["article_id"]))
            summary = (getattr(first, "summary", "") or "") if first else ""
        item = {
            "id": r.state.item_id,
            "headline": headline,
            "summary": summary,
            # 一覧のバッジ用。読み手が最初に見るのは「何の話か」であって媒体数ではない
            "category": _dominant_category(articles, r.state.member_ids),
            "generated": latest is not None,
            "sources": len(citations),
            "independent_sources": r.independent_sources,
            "published_at": r.state.last_reported_at.isoformat(),
            "citations": [_public_citation(c) for c in citations[:3]],
        }
        items.append(item)
    return {"items": items, "note": GENERATED_NOTE, "categories": list(PUBLIC_CATEGORIES)}


# 地図の集計はやや重い (公開対象の全事象を走査) ので短 TTL を挟む。
_MAP_TTL_SECONDS = 300.0
_map_cache: dict[int, tuple[float, dict[str, Any]]] = {}


@public_news_api.get("/map")
def get_public_map(days: int = 30) -> dict[str, Any]:
    """公開ニュースの **被害国** 分布。

    ⚠ 分析画面の脅威マップ (`/api/v1/geo/*`) を公開へ流用しない。あちらは
    アクター帰属と意図 (Diamond Model) の層を重ねており、**報道された事実ではなく
    kuebiko の分析判断**を含む。公開面は根拠の連鎖を示せないので出さない。

    ⚠ **母集団を記事一覧と一致させる**。別母集団だと「地図は 592 件なのに記事は
    30 件」と食い違い、読み手が混乱する。ここは公開対象 (high・重複判定のみを除く)
    と同じ集合だけを数える。

    ⚠ **地図は収集網の観測であって世界ではない**。国が特定できなかった件数を必ず
    併せて返す (実測では公開対象の 56% に国が付いていない)。読み手が「これが世界の
    実態」と読むのを防ぐのは、割合を隠さないことでしかできない。
    """
    window = max(1, min(365, days))
    now = time.monotonic()
    cached = _map_cache.get(window)
    if cached is not None and now - cached[0] < _MAP_TTL_SECONDS:
        return cached[1]

    repo = _repo()
    since = datetime.now(UTC) - timedelta(days=window)
    records = repo.list_event_items(
        origin="live",
        importances=list(_PUBLIC_IMPORTANCES),
        exclude_merged=True,
        exclude_duplicate_only=True,
        since=since,
        limit=5000,
    )
    articles = repo.get_articles_by_ids([a for r in records for a in r.state.member_ids])

    counts: dict[str, int] = {}
    unplaced = 0
    for r in records:
        iso = ""
        for aid in r.state.member_ids:
            art = articles.get(aid)
            value = (getattr(art, "victim_country_iso", "") or "") if art else ""
            if value:
                iso = value.upper()
                break
        if iso:
            counts[iso] = counts.get(iso, 0) + 1
        else:
            unplaced += 1

    geocoder = Geocoder()
    # 表示名の SSoT は config/cti/countries.yaml (分析画面と同じ解決経路を使う)
    labels = _yaml_display_map(str(_COUNTRIES_YAML))
    nodes: list[dict[str, Any]] = []
    for iso, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        point = geocoder.country(iso)
        if point is None:
            unplaced += count  # 座標が引けないものも「置けなかった」に数える
            continue
        nodes.append(
            {
                "iso": iso,
                "label": labels.get(iso, iso),
                "lat": point.lat,
                "lon": point.lon,
                "count": count,
            }
        )

    payload = {
        "nodes": nodes,
        "window_days": window,
        # 読み手に割合を示すための 3 値。placed / total を隠さない
        "placed": sum(n["count"] for n in nodes),
        "unplaced": unplaced,
        "total": len(records),
        "note": (
            "掲載記事のうち被害国を特定できたものだけを地図にしています。"
            "収集した報道の分布であり、世界全体の実態を示すものではありません。"
        ),
    }
    _map_cache[window] = (now, payload)
    return payload


@public_news_api.get("/{item_id}")
def get_public_news(item_id: str) -> dict[str, Any]:
    """公開ニュース 1 件。生成本文 (見出し / BLUF / 事実行) と **出典全件**。"""
    repo = _repo()
    record = repo.get_event_item(item_id)
    if record is None or record.merged_into or record.state.importance not in _PUBLIC_IMPORTANCES:
        raise HTTPException(status_code=404, detail="not found")
    citations = _citations(repo, item_id)
    if not citations:
        raise HTTPException(status_code=404, detail="not found")

    versions = repo.list_event_versions(item_id)
    if not versions:
        articles = repo.get_articles_by_ids(list(record.state.member_ids))
        if _is_duplicate_only(articles, record.state.member_ids):
            raise HTTPException(status_code=404, detail="not found")
    latest = versions[0] if versions else None
    body = json.loads(latest.body_json) if latest and latest.body_json else {}
    if latest is not None:
        bluf = str(body.get("bluf", ""))
    else:
        first = repo.get_articles_by_ids([str(citations[0]["article_id"])]).get(
            str(citations[0]["article_id"])
        )
        bluf = (getattr(first, "summary", "") or "") if first else ""
    return {
        "id": item_id,
        "headline": latest.headline if latest else citations[0]["title"],
        "category": _dominant_category(
            repo.get_articles_by_ids(list(record.state.member_ids)), record.state.member_ids
        ),
        "generated": latest is not None,
        "bluf": bluf,
        # 事実行は出典番号を持つ (どの媒体が報じたかを本文中で示す)
        "facts": body.get("facts", []),
        "discrepancies": body.get("discrepancies", []),
        "unknowns": body.get("unknowns", []),
        "published_at": record.state.last_reported_at.isoformat(),
        "first_reported_at": record.state.first_reported_at.isoformat(),
        "independent_sources": record.independent_sources,
        "citations": [_public_citation(c) for c in citations],
        "note": GENERATED_NOTE,
    }
