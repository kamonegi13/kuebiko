"""ransomware.live 構造化被害者 → articles 取込 (被害状況コレクタの配線)。

ポリシー (ユーザー決定 2026-06-19):
  - **JP 被害者** → japan_watch に Discord 投稿し ``status='posted'`` (mission 直撃・少量)。
  - **非 JP (global)** → ``status='collected'`` で DB 取込のみ (地図/Intel 用、Discord 非投稿)。
    地図クエリは ``status IN ('posted','collected')`` で拾う (geo_cyber_map)。他 view は 'posted'
    のみ参照するため collected は KPI/履歴を汚さない。

LLM/本文抽出を経由せず構造化フィールドを直接永続化する (決定論的・低コスト)。``dedup_key`` で
冪等 (既存はスキップ)。詳細・経緯: [[geo_map_design_discussion]]。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from src.config_loader import load_app_config
from src.cti.actor_normalizer import ActorAliasRegistry, load_actor_aliases
from src.cti.ransom_groups import distinctive_names, find_mention, has_ransom_context
from src.cti.subject_actor import SOURCE_FEED
from src.cti.taxonomy_normalizer import load_normalizer
from src.logging_config import get_logger
from src.sources.ransomware_live import (
    RansomwareIncident,
    dedup_key,
    fetch_country,
    fetch_recent,
    parse_incidents,
)
from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord
from src.tools.discord_publisher import BriefingMessage, DiscordPublisher, Source

_log = get_logger(__name__)

# ランサムリーク = その被害組織の breach。地図の cyber カテゴリ集合に含まれる既存値を使う
# (geo_cyber_map._CYBER_ATTACK_CATEGORIES = {breach,incident,apt,apt_leak,malware,phishing})。
_CATEGORY = "breach"
_FEED_TITLE = "ransomware.live"
_FEED_URL = "https://api.ransomware.live/v2"
_ROUTING_RULE_ID = "RANSOMWARE_LIVE"
# クロスソース重複の時間窓: 同一被害組織でもこの日数を超えて離れた事案は別被害とみなす
# (同じ組織が別時期に再被害したケースを誤って重複扱いしないため)。
_DUP_WINDOW_DAYS = 60
# 自動ランサム辞書でニュースを後付けタグする対象窓 (地図の最大窓 365d を含む)。
_NEWS_TAG_WINDOW_DAYS = 400


def _parse_dt(s: str) -> datetime | None:
    """ISO8601 文字列 → datetime (naive、失敗時 None)。published_at 用。"""
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _to_dt(v: object) -> datetime | None:
    """DB 値 (datetime or ISO 文字列) → naive datetime。tz は揃えるため落とす。"""
    if isinstance(v, datetime):
        return v.replace(tzinfo=None)
    if isinstance(v, str):
        return _parse_dt(v)
    return None


def _build_news_index(
    rows: list[tuple[str, Any]],
) -> dict[str, list[datetime]]:
    """(victim_org小文字, 日付) の list を org→日付list の index にする (日付不明は除外)。"""
    idx: dict[str, list[datetime]] = {}
    for org, d in rows:
        dt = _to_dt(d)
        if dt is not None:
            idx.setdefault(org, []).append(dt)
    return idx


def _covered_by_news(
    news_index: dict[str, list[datetime]], victim: str, when: datetime | None
) -> bool:
    """victim_org がニュースに ±窓内で既出か (= ransomware.live 側は重複)。"""
    dates = news_index.get(victim.strip().lower())
    if not dates:
        return False
    if when is None:
        return True  # rl 側の日付不明 → org 一致のみでニュース優先 (保守的に重複扱い)
    return any(abs((d - when).days) <= _DUP_WINDOW_DAYS for d in dates)


def _resolve_group(group: str, registry: ActorAliasRegistry) -> tuple[str, bool]:
    """攻撃グループ名 → (actor id, 辞書解決できたか)。

    R2 (2026-07-26): source slug 名前空間写像 (resolve_source_slug) を最優先 —
    "thegentlemen"→the_gentlemen 等の slug 不一致を吸収する。次に散文 find に fallback。
    どちらも外れたら (小文字 raw, False) = 未知グループ (呼び出し側が provisional 化)。
    """
    g = (group or "").strip()
    if not g:
        return "", False
    hit = registry.resolve_source_slug(g) or registry.find(g)
    return (hit.id, True) if hit is not None else (g.lower(), False)


def _title(inc: RansomwareIncident, *, display_group: str | None = None) -> str:
    """見出し: 攻撃グループ + 被害組織 (+ 国)。

    ``display_group`` = 辞書解決済みの canonical 表示名。生 slug をタイトルに残すと
    LLM がその綴りを primary_actor として再生産し、新興提案経路で綴り違いの
    二重登録 (2026-08-01 thegentlemen 事故) を誘発するため、解決済みは canonical を使う。
    """
    g = display_group or inc.group or "ランサムウェア"
    loc = f" ({inc.country_iso})" if inc.country_iso else ""
    return f"{g}: {inc.victim}{loc}"


def _summary(inc: RansomwareIncident) -> str:
    """要約: 構造化フィールド + DLS 説明文 (長すぎる説明は切詰め)。"""
    parts = [f"ランサムウェアグループ **{inc.group or '不明'}** が **{inc.victim}** の被害を主張。"]
    meta: list[str] = []
    if inc.country_iso or inc.country_raw:
        meta.append(f"国: {inc.country_iso or inc.country_raw}")
    if inc.sector_raw:
        meta.append(f"業種: {inc.sector_raw}")
    if inc.discovered:
        meta.append(f"発覚: {inc.discovered[:10]}")
    if meta:
        parts.append(" / ".join(meta))
    desc = (inc.description or "").strip()
    if desc:
        parts.append(desc[:600] + (" …" if len(desc) > 600 else ""))
    parts.append("出典: ransomware.live (DLS 集約)")
    return "\n\n".join(parts)


def incident_to_article_record(
    inc: RansomwareIncident,
    *,
    run_id: int,
    status: str,
    importance: str,
    posted_channel: str | None = None,
    discord_message_id: str | None = None,
    discord_channel_id: str | None = None,
    subject_actor_id: str | None = None,
    display_group: str | None = None,
) -> ArticleRecord:
    """1 incident → ArticleRecord (victim 構造化フィールドを直接セット)。

    victim_org / actor は ``add_article_entities`` で別途永続化する (この record には載らない)。
    R2: 辞書解決できたグループは **source 断言** として主題を直書きする (被害者掲載 =
    定義上その集団の主題記事)。subject_actor_id=None (未知グループ) は主題を書かない
    (provisional 経由で人承認へ回す)。
    """
    key = dedup_key(inc)
    return ArticleRecord(
        run_id=run_id,
        article_id=key,  # dedup_key は incident 一意 → そのまま article_id に使う
        title=_title(inc, display_group=display_group),
        url=inc.url or _FEED_URL,
        feed_title=_FEED_TITLE,
        feed_url=_FEED_URL,
        importance=importance,
        category=_CATEGORY,
        status=status,  # type: ignore[arg-type]
        posted_channel=posted_channel,
        dedup_key=key,
        discord_message_id=discord_message_id,
        discord_channel_id=discord_channel_id,
        summary=_summary(inc),
        # 地図が読む構造化フィールド (LLM 抽出を経由しない決定論的値)。
        victim_sector_canonical=inc.sector_canonical,
        victim_sector_raw=inc.sector_raw or None,
        victim_country_iso=inc.country_iso,
        victim_country_raw=inc.country_raw or None,
        is_ransomware=True,  # ransomware.live 由来は定義上ランサム
        pmesii_e=True,
        pmesii_i_cyber=True,
        published_at=_parse_dt(inc.discovered),
        # R2: source 断言による主題直書き (辞書解決グループのみ、証拠等級 'feed')
        subject_actor_ids=subject_actor_id or None,
        subject_actor_source=SOURCE_FEED if subject_actor_id else None,
    )


def _to_briefing(
    inc: RansomwareIncident, *, actor_id: str, display_group: str | None = None
) -> BriefingMessage:
    """JP 被害者の japan_watch 投稿用 BriefingMessage。"""
    return BriefingMessage(
        title=_title(inc, display_group=display_group),
        importance="medium",
        category=_CATEGORY,
        summary=_summary(inc),
        sources=[Source(title=_FEED_TITLE, url=inc.url or _FEED_URL, language="auto")],
        metadata={
            "target_channel": "japan_watch",
            "routing_rule_id": _ROUTING_RULE_ID,
            "routing_reason": "ransomware.live:JP victim",
            "dedup_key": dedup_key(inc),
            "victim_country_iso": inc.country_iso,
            "victim_orgs": [inc.victim],
            "detected_actor_ids": [actor_id] if actor_id else [],
        },
    )


async def run_ingest(
    *,
    config: Any = None,
    repo: RunHistoryRepository | None = None,
    backfill_country: str | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """ransomware.live を取得し、JP=投稿 / global=collected で取り込む。

    Args:
        backfill_country: 指定 ISO2 (例 'JP') の履歴も追加取得する (初回 backfill 用)。
        dry_run: True なら DB 書き込み・Discord 投稿をせず件数だけ集計。

    Returns:
        {"fetched","new","posted_jp","collected","skipped"} の集計。
    """
    cfg = config or load_app_config()
    repository = repo or RunHistoryRepository()
    normalizer = load_normalizer()
    registry = load_actor_aliases()

    # recent (直近 global) = Discord 投稿の対象。国別 backfill (履歴) は collected 限定
    # (304 件規模の履歴を japan_watch に投げると氾濫するため、地図にだけ載せる)。
    recent = parse_incidents(fetch_recent(), normalizer)
    backfill = (
        parse_incidents(fetch_country(backfill_country), normalizer) if backfill_country else []
    )

    # (key, incident, postable) を組む。recent 優先 (postable=True)、backfill は未出のみ collected。
    items: list[tuple[str, RansomwareIncident, bool]] = []
    seen_key: set[str] = set()
    for inc in recent:
        k = dedup_key(inc)
        if k not in seen_key:
            seen_key.add(k)
            items.append((k, inc, True))
    for inc in backfill:
        k = dedup_key(inc)
        if k not in seen_key:
            seen_key.add(k)
            items.append((k, inc, False))

    # クロスソース重複判定: ニュース等が同一被害組織を扱っていれば ransomware.live 側は重複。
    news_index = _build_news_index(repository.news_victim_org_dates())

    stats = {
        "fetched": len(items),
        "new": 0,
        "posted_jp": 0,
        "collected": 0,
        "duplicate": 0,
        "skipped": 0,
        "reconciled": 0,
        "news_tagged": 0,
    }

    if dry_run:
        for key, inc, postable in items:
            if repository.dedup_key_exists(key):
                stats["skipped"] += 1
            elif _covered_by_news(news_index, inc.victim, _parse_dt(inc.discovered)):
                stats["duplicate"] += 1
                stats["new"] += 1
            elif postable and inc.country_iso == "JP":
                stats["posted_jp"] += 1
                stats["new"] += 1
            else:
                stats["collected"] += 1
                stats["new"] += 1
        # 既存 collected のうちニュースに既出のものも重複候補として件数表示。
        stats["reconciled"] = sum(
            1
            for _aid, org, d in repository.collected_ransomware_for_reconcile()
            if _covered_by_news(news_index, org, _to_dt(d))
        )
        # 自動辞書でタグされ得るニュース件数 (dry-run 集計)。
        names = distinctive_names(repository.ransomware_live_group_names())
        if names:
            since_iso = (datetime.now(UTC) - timedelta(days=_NEWS_TAG_WINDOW_DAYS)).isoformat()
            stats["news_tagged"] = sum(
                1
                for _aid, title, summary in repository.untagged_cyber_news(since_iso=since_iso)
                if find_mention(f"{title} {summary}", names)
                and has_ransom_context(f"{title} {summary}")
            )
        return stats

    run_id = repository.start_run(
        RunRecord(
            started_at=datetime.now(UTC),
            pipeline="ransomware-live-ingest",
            dry_run=False,
            triggered_by="scheduler",
        )
    )
    jp_webhook = cfg.discord_webhooks.get("japan_watch", "")
    publisher = DiscordPublisher(jp_webhook) if jp_webhook else None

    for key, inc, postable in items:
        if repository.dedup_key_exists(key):
            stats["skipped"] += 1
            continue
        actor, is_known = _resolve_group(inc.group, registry)
        # 表示は canonical (生 slug を LLM に見せない — thegentlemen 事故対策)
        _entry = registry.by_id(actor) if is_known else None
        display = _entry.canonical if _entry is not None else None
        # ニュースが同一被害組織を既に扱っていれば重複 → 投稿せず collected_duplicate で記録。
        is_dup = _covered_by_news(news_index, inc.victim, _parse_dt(inc.discovered))
        is_jp = postable and inc.country_iso == "JP" and not is_dup
        msg_id: str | None = None
        ch_id: str | None = None
        if is_jp and publisher is not None:
            try:
                meta = await publisher.post(
                    _to_briefing(inc, actor_id=actor, display_group=display)
                )
                msg_id, ch_id = meta.message_id, meta.channel_id
            except Exception as e:  # noqa: BLE001 — 投稿失敗でも DB には残す (再投稿はしない)
                _log.warning("ransomware_ingest_post_failed", victim=inc.victim, error=str(e))
        status = "collected_duplicate" if is_dup else ("posted" if is_jp else "collected")
        record = incident_to_article_record(
            inc,
            run_id=run_id,
            status=status,
            importance="medium" if is_jp else "low",
            posted_channel="japan_watch" if is_jp else None,
            discord_message_id=msg_id,
            discord_channel_id=ch_id,
            subject_actor_id=actor if is_known else None,
            display_group=display,
        )
        try:
            repository.add_article(record)
            entities: list[tuple[str, str]] = [("victim_org", inc.victim)]
            if actor:
                # 辞書解決済み = 確定 actor 言及 / 未知 = actor_provisional (人承認へ回す。
                # raw を 'actor' に直書きすると recall layer の収穫網を迂回するため — 確定
                # 帰属は辞書ゲート・人承認のみ、の原則を source 経路でも守る)
                entities.append(("actor" if is_known else "actor_provisional", actor))
            repository.add_article_entities(key, entities)
        except Exception as e:  # noqa: BLE001 — 1 件失敗は skip して継続
            _log.warning("ransomware_ingest_persist_failed", victim=inc.victim, error=str(e))
            continue
        stats["new"] += 1
        if is_dup:
            stats["duplicate"] += 1
            continue
        stats["posted_jp" if is_jp else "collected"] += 1

    # reconcile: 既存 collected のうち、その後ニュースが同一被害組織を扱ったものを重複化。
    # (ransomware.live 先行 → 後からニュース掲載、のケースを次回 run で拾う)
    to_mark = [
        aid
        for aid, org, d in repository.collected_ransomware_for_reconcile()
        if _covered_by_news(news_index, org, _to_dt(d))
    ]
    stats["reconciled"] = repository.set_article_status(to_mark, "collected_duplicate")

    # 自動ランサム辞書: ransomware.live の group 名 (distinctive) でニュースを後付けタグ。
    # registry family 判定の補完で、registry 未登録の新グループも self-updating に網羅する。
    names = distinctive_names(repository.ransomware_live_group_names())
    if names:
        since_iso = (datetime.now(UTC) - timedelta(days=_NEWS_TAG_WINDOW_DAYS)).isoformat()
        tag_ids = [
            aid
            for aid, title, summary in repository.untagged_cyber_news(since_iso=since_iso)
            if find_mention(f"{title} {summary}", names)
            and has_ransom_context(f"{title} {summary}")
        ]
        stats["news_tagged"] = repository.set_is_ransomware(tag_ids)

    repository.finish_run(
        run_id,
        status="succeeded",
        finished_at=datetime.now(UTC),
        total_fetched=stats["fetched"],
        summarized=stats["new"],
        posted=stats["posted_jp"],
        marked_read=0,
        error_count=0,
        note=(
            f"collected={stats['collected']} dup={stats['duplicate']} "
            f"reconciled={stats['reconciled']} news_tagged={stats['news_tagged']} "
            f"skipped={stats['skipped']}"
        ),
    )
    _log.info("ransomware_ingest_done", **stats)
    return stats
