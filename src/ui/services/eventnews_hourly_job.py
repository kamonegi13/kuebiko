"""事象ニュースの毎時ジョブ本体 (shadow 運用)。

収集サイクルの後に走り、**前回実行以降に取り込まれた記事のみ**を既存アイテムへ
合流させる。生成はメンバー 2 件以上のアイテムに限る (単独記事は per-article 要約を
そのまま読ませる — docs/event_news_design.md §14b の案 A)。

**この時点では読み手向けの出口を持たない** (UI/Discord 未配線)。目的は
①毎時運用が成立するかの実証 ②毎時の LLM 占有時間の実測 の 2 つ。
``EVENTNEWS_HOURLY=0`` で完全停止できる。
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

import numpy as np

from src.config_loader import AppConfig, load_app_config
from src.eventnews import pair_shadow
from src.eventnews.generator import select_members
from src.eventnews.grouping import build_join_entities, join_entity_key
from src.eventnews.hourly import hydrate_open_items, run_hourly
from src.eventnews.models import (
    ENTITY_FREQ_WINDOW_HOURS,
    JOIN_ENTITY_TYPES,
    ItemState,
    MemberArticle,
)
from src.logging_config import get_logger
from src.storage.event_time import DEDUP_ARTICLES, EVENT_TS_EXPR
from src.storage.run_history import RunHistoryRepository
from src.tools.llm_client import LLMClient
from src.tools.model_tiers import Step, build_llm_for

_log = get_logger(__name__)

_FLAG = "EVENTNEWS_HOURLY"
# 候補の取込窓。毎時実行なら 1 時間で足りるが、実行が飛んだ場合の取りこぼしを防ぐため
# 広めに取る (既にメンバーの記事は hourly.run_hourly が候補から外すので冪等)。
_CANDIDATE_LOOKBACK_HOURS = 6
_TS = EVENT_TS_EXPR.format(a="a")

# 候補の母集団 (2026-08-24 に拡張)。
#
# **low を含める理由は被覆**: 事象ニュースを主導線にする以上、high/medium だけだと
# 直近 14 日の記事の 68.5% しか到達できない。low を足して 87.6%、被害者レコードまで
# 入れて 98.7%。統合そのものへの寄与は low が +5 事象と僅少で、上流の意味的 dedup が
# 近接重複を既に落としているため (実測)。
#
# **被害者レコード (collected 系) を含める理由は統合**: ransomware.live の被害者掲載は
# 構造化データだが、同じ被害組織を報じたニュースと **entity (victim_org) を共有して
# 合流する**。実測で multi 事象 +42、うち **50 群が「リークサイト掲載 + 報道」の
# 混成**になった。掲載と報道が 1 本に束ねられるのは読み手にとって素直な形。
_SQL_CANDIDATES = f"""
SELECT a.article_id, {_TS} AS anchor_ts, x.importance, x.category, x.status,
       x.title, x.url, x.feed_title, x.feed_url, x.summary, x.body, x.account_class
FROM {DEDUP_ARTICLES} a
JOIN (
  SELECT article_id, MAX(importance) importance, MAX(category) category,
         MAX(status) status, MAX(title) title, MAX(url) url,
         MAX(COALESCE(feed_title,'')) feed_title, MAX(COALESCE(feed_url,'')) feed_url,
         MAX(COALESCE(summary,'')) summary, MAX(COALESCE(body,'')) body,
         MAX(COALESCE(account_class,'')) account_class
  FROM articles GROUP BY article_id
) x ON x.article_id = a.article_id
WHERE a.created_at >= ? AND x.importance IN ('high','medium','low')
  AND x.status IN ('posted','skipped_duplicate','collected','collected_duplicate')
ORDER BY 2
"""

# entity は **article_id で引く**。``article_entities.created_at`` は行を書いた時刻で
# あって事象時刻ではない (バックフィル・再抽出は過去記事へ当日の日付を書く)。
# ここを時刻で絞ると、復元した既存メンバー (最大 72h 前) の entity が空になり、
# 「共有 entity >= 1」が永久に不成立 → **合流が構造的に起きなくなる**
# (2026-08-24: 本番で 25 アイテム全件が単独記事のままだった原因)。
# ⚠ **型の一覧をここに書かない。** JOIN_ENTITY_TYPES から組み立てる — 2026-08-31 に
# tool を定数へ足したのに、この SQL がベタ書きのままで読み込まれず、変更が丸ごと
# 無効だった (「同じものが 2 箇所」)。
_JOIN_TYPES_SQL = ",".join(f"'{t}'" for t in JOIN_ENTITY_TYPES)

_SQL_ENTITIES_BY_ID = f"""
SELECT article_id, entity_type, LOWER(TRIM(value)) FROM article_entities
WHERE entity_type IN ({_JOIN_TYPES_SQL})
  AND LENGTH(TRIM(value)) >= 4 AND article_id IN ({{placeholders}})
"""

# 頻出ガード (ENTITY_FREQ_CAP) の分母。窓内コーパス全体で数えないと、
# 手元の数十件では cap に届かず頻出語が結合信号として通ってしまう。
# 集計は SQL でせず Python 側で行う。キーの作り方 (victim_org の正規化) を
# 参照側と共有する必要があり、SQL では同じ正規化を書けないため
# (2026-08-25: SQL の LOWER(TRIM()) と normalize_for_match がずれて cap が不発だった)。
# 窓内の対象行は実測 8,422 行なので Python 集計で問題ない。
_SQL_ENTITY_COUNTS = f"""
SELECT e.entity_type, LOWER(TRIM(e.value)), e.article_id
FROM article_entities e
WHERE e.entity_type IN ({_JOIN_TYPES_SQL})
  AND LENGTH(TRIM(e.value)) >= 4
  AND e.article_id IN (
    SELECT a.article_id FROM {DEDUP_ARTICLES} a WHERE a.created_at >= ?
  )
"""


def _entity_counts(
    repo: RunHistoryRepository, window_start: datetime
) -> dict[tuple[str, str], int]:
    """窓内コーパスでの (entity_type, value) 出現記事数 = 頻出ガードの分母。

    キーは ``join_entity_key`` で作る — 参照側 (``build_join_entities``) と
    同じ関数を通さないと lookup が当たらず、ガードが無言で不発になる。
    """
    with repo._connect() as conn:  # noqa: SLF001
        rows = conn.execute(_SQL_ENTITY_COUNTS, (window_start.isoformat(),)).fetchall()
    seen: dict[tuple[str, str], set[str]] = {}
    for r in rows:
        key = join_entity_key(str(r[0]), str(r[1]))
        seen.setdefault(key, set()).add(str(r[2]))
    return {key: len(article_ids) for key, article_ids in seen.items()}


def _join_entities_for(
    repo: RunHistoryRepository,
    article_ids: Sequence[str],
    counts: Mapping[tuple[str, str], int],
) -> dict[str, frozenset[tuple[str, str]]]:
    """指定記事の結合信号 entity を引く (候補・既存メンバーで共通に使う唯一の口)。"""
    if not article_ids:
        return {}
    placeholders = ",".join("?" for _ in article_ids)
    with repo._connect() as conn:  # noqa: SLF001
        rows = conn.execute(
            _SQL_ENTITIES_BY_ID.format(placeholders=placeholders),  # noqa: S608 — placeholders のみ
            list(article_ids),
        ).fetchall()
    return build_join_entities([(str(r[0]), str(r[1]), str(r[2])) for r in rows], counts)


def _to_member(row: Mapping[str, object], entities: frozenset[tuple[str, str]]) -> MemberArticle:
    """行 → MemberArticle。**位置でなくキー名で読む** — 列順・列数の変更に強い。"""
    aid = row["article_id"]
    ts = row["anchor_ts"]
    imp, cat, status = row["importance"], row["category"], row["status"]
    title, url = row["title"], row["url"]
    feed_title, feed_url = row["feed_title"], row["feed_url"]
    summary, body = row["summary"], row["body"]
    # ⚠ sqlite3.Row に .get() は無い (PG は dict)。両 backend で使えるのは添字だけ。
    account_class = str(row["account_class"] or "")
    anchor = ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts))
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=UTC)
    return MemberArticle(
        article_id=str(aid),
        title=str(title),
        url=str(url),
        feed_title=str(feed_title),
        feed_url=str(feed_url),
        host=str(url).split("/")[2] if "://" in str(url) else "",
        importance=str(imp),
        category=str(cat),
        status=str(status),
        anchor_ts=anchor,
        summary=str(summary),
        body=str(body),
        entities=entities,
        account_class=account_class,
    )


async def run_eventnews_hourly() -> dict[str, object]:
    """毎時ジョブの入口。戻り値は run_history に載せる要約。"""
    if os.environ.get(_FLAG, "1") == "0":
        _log.info("eventnews_hourly_disabled")
        return {"skipped": "flag_off"}
    return await run_eventnews_window(lookback_hours=_CANDIDATE_LOOKBACK_HOURS)


async def _resolve_kinds(
    repo: RunHistoryRepository,
    config: AppConfig,
    articles: Sequence[MemberArticle],
) -> dict[str, str]:
    """判定に使う記事の種別 (event_kind) を、キャッシュ優先で解決する。

    分類は記事ごとに 1 回 (26B・fast ティア)。失敗は "other" (学習時の退避先と同じ)。
    """
    from src.eventnews import event_kind

    ids = [a.article_id for a in articles]
    kinds = repo.get_article_kinds(ids)
    missing = [a for a in articles if a.article_id not in kinds]
    if missing:
        # Step.TRIAGE 借用は triage の S 族上書きを継承してしまう (2026-09-08 分離)
        llm = build_llm_for(Step.EVENT_KIND, config)
        for a in missing:
            kind = await event_kind.classify(llm, a.title, a.summary)
            kinds[a.article_id] = kind
            repo.set_article_kind(a.article_id, kind, getattr(llm, "model", ""))
        _log.info("event_kind_classified", articles=len(missing))
    return kinds


def pending_items(repo: RunHistoryRepository) -> list[tuple[ItemState, list[MemberArticle]]]:
    """まだ版を持たず、本文を持つメンバーが 2 件以上あるアイテム (新しい順)。

    ⚠ 遡及統合とバックフィルの **両方** がここを通る。2026-09-02 まで
    ``scripts/eventnews_backfill.py`` の私有関数だったため、遡及統合は統合先の
    ``current_version`` を 0 に戻すだけで再生成の口を持たず、**統合しただけで
    本文が消える**状態を作れてしまった (毎時ジョブは新着が入った事象しか生成しない)。
    """
    counts = _entity_counts(repo, datetime.now(UTC) - timedelta(hours=ENTITY_FREQ_WINDOW_HOURS))
    records = [
        r
        for r in repo.list_event_items(origin="live", limit=20000)
        if not r.merged_into and r.state.current_version == 0 and len(r.state.member_ids) >= 2
    ]
    members_by_id = _load_members(repo, [a for r in records for a in r.state.member_ids], counts)
    out: list[tuple[ItemState, list[MemberArticle]]] = []
    for r in records:
        members = [members_by_id[a] for a in r.state.member_ids if a in members_by_id]
        textual, _ = select_members(members)
        if len(textual) >= 2:
            out.append((r.state, members))
    # 新しい事象から順に (読み手にとっての価値が高い順)
    out.sort(key=lambda pair: pair[0].last_reported_at, reverse=True)
    return out


def regenerate_pending_bodies(repo: RunHistoryRepository, sleep_seconds: float = 3.0) -> None:
    """版を失った事象の本文を作り直す (遡及統合・遡及分割の後始末)。

    ⚠ 統合/分割と**セット**でなければ意味がない — current_version を 0 に戻す操作は
    これを呼ばない限り「本文が消えた」で終わる (2026-09-02 に実際に 45 件で発生)。
    """
    import asyncio
    import time as _time

    from src.eventnews.runner import generate_pending

    pending = pending_items(repo)
    print(f"\n再生成の対象: {len(pending)} 件", flush=True)
    if not pending:
        return
    config = load_app_config()
    last = _time.monotonic()

    def _progress(i: int, total: int, item_id: str) -> None:
        nonlocal last
        now = _time.monotonic()
        print(f"  [{i}/{total}] {item_id} (前件 {now - last:.0f}s)", flush=True)
        last = now
        if sleep_seconds > 0:
            _time.sleep(sleep_seconds)

    stats = asyncio.run(
        generate_pending(
            repo,
            pending,
            lambda: build_llm_for(Step.EVENT_NEWS, config),
            on_progress=_progress,
        )
    )
    print(
        f"生成 {stats.generated} / 素材不足で skip {stats.skipped} / 失敗 {stats.failed}",
        flush=True,
    )


async def run_eventnews_window(*, lookback_hours: int, generate: bool = True) -> dict[str, object]:
    """指定した遡及幅で群化 (+ 生成) を 1 回走らせる。

    毎時ジョブとバックフィルで **取得を完全に共有する** ための唯一の入口。
    2026-08-24 の不発は「評価と本番で entity の引き方が違った」ことが原因だったので、
    遡及幅だけを引数にして、それ以外の経路を分岐させない。
    """
    started = time.monotonic()
    repo = RunHistoryRepository()
    since = datetime.now(UTC) - timedelta(hours=lookback_hours)

    with repo._connect() as conn:  # noqa: SLF001 — repo 内部接続の再利用 (他ジョブと同型)
        rows = conn.execute(_SQL_CANDIDATES, (since.isoformat(),)).fetchall()

    counts = _entity_counts(repo, datetime.now(UTC) - timedelta(hours=ENTITY_FREQ_WINDOW_HOURS))
    cand_ids = [str(r["article_id"]) for r in rows]
    cand_ents = _join_entities_for(repo, cand_ids, counts)
    candidates = [_to_member(r, cand_ents.get(str(r["article_id"]), frozenset())) for r in rows]

    # 既にどこかのアイテムのメンバーになっている記事は候補から外す。run_hourly は
    # **復元したアイテム**のメンバーしか除外できないため、復元窓より古いアイテムの
    # メンバーが再候補化すると決定論の item_id が衝突する (遡及構築で実際に落ちた)。
    already = repo.existing_member_article_ids([c.article_id for c in candidates])
    candidates = [c for c in candidates if c.article_id not in already]

    vectors = _load_vectors(repo, [c.article_id for c in candidates])
    candidates = [c for c in candidates if c.article_id in vectors]
    if not candidates:
        _log.info("eventnews_hourly_no_candidates")
        return {"candidates": 0, "elapsed_seconds": round(time.monotonic() - started, 1)}

    existing = hydrate_open_items(repo, lambda ids: _load_members(repo, ids, counts))
    for _, members in existing:
        vectors.update(_load_vectors(repo, [m.article_id for m in members]))

    config = load_app_config()

    def _llm() -> LLMClient:
        return build_llm_for(Step.EVENT_NEWS, config)

    members_all = [m for _, members in existing for m in members]

    # ⭐ ペアの評価は **1 回だけ**。群化に渡す判定 (EVENTNEWS_PAIR_ML=1) と
    #    シャドー記録 (EVENTNEWS_PAIR_SHADOW=1) の両方がこの結果を使う。
    #    以前は decide/observe が同じペアに同じ 26B 判定を二重に掛けていた。
    #    評価が作れなければ空 → 群化は決定論のまま動き、記録も残らない。
    verdicts: list[pair_shadow.PairVerdict] = []
    ml_ready = pair_shadow.is_ml_ready()
    if ml_ready or pair_shadow.is_enabled():
        try:
            pairs_now = pair_shadow.select_pairs(candidates, members_all, vectors)
            involved = list({m.article_id: m for pair in pairs_now for m in pair}.values())
            kinds = await _resolve_kinds(repo, config, involved)
            verdicts = await pair_shadow.judge_pairs(
                pairs_now,
                vectors,
                # ⭐ 判定は fast ティア既定 (base 26B)。31B は 4 倍遅く、外部は枠を食う
                #    (2026-08-31 実測)。Step.TRIAGE 借用は triage の S 族上書きを黙って
                #    継承するため専用 step に分離 (2026-09-08、SYNTHESIS.md §25)
                llm=build_llm_for(Step.PAIR_JUDGE, config),
                embed_summary=lambda arts: _embed_summaries(config, arts),
                kinds=kinds,
            )
        except Exception as e:  # noqa: BLE001 — 評価が作れなくても群化は続ける
            _log.warning("eventnews_pair_eval_failed", error=str(e)[:200])

    pair_decision = pair_shadow.decisions_of(verdicts) if ml_ready else {}
    pair_proba = pair_shadow.probas_of(verdicts) if ml_ready else {}
    if pair_decision:
        _log.info("eventnews_pair_ml", pairs=len(pair_decision), joined=sum(pair_decision.values()))

    result = await run_hourly(
        repo,
        candidates,
        vectors,
        existing,
        _llm if generate else None,
        pair_decision=pair_decision or None,
        pair_proba=pair_proba or None,
    )

    # ⭐ 記録は **群化の後**。書き込みに失敗しても毎時ジョブを止めない (観測は本流ではない)。
    #    ⚠ 規則の判定 (rule_joined) は評価時に独立して計算してある —
    #    ML を群化に使っていても「規則ならどうしたか」の比較が成立する。
    shadow_pairs = 0
    if verdicts and pair_shadow.is_enabled():
        try:
            shadow_pairs = pair_shadow.record(repo, verdicts)
        except Exception as e:  # noqa: BLE001 — 記録の失敗で群化を落とさない
            _log.warning("eventnews_pair_shadow_failed", error=str(e)[:200])

    elapsed = round(time.monotonic() - started, 1)
    # ⚠ ML の承認と実際の合流を **同じ行に**出す。2026-09-01 の切替では承認 13 組に
    #    対し合流 1 件という乖離が起きていたのに、両者が別の行に散っていたため
    #    「ジョブは succeeded」以上のことが分からなかった。ml_approved と
    #    joined が桁で食い違っていたら、判定がどこかで打ち消されている。
    #    (候補側は verdict.left — frozenset からはどちらが候補か分からない)
    approved_articles = len({v.left.article_id for v in verdicts if pair_decision.get(v.key)})
    _log.info(
        "eventnews_hourly_summary",
        elapsed_seconds=elapsed,
        candidates=result.candidates,
        hydrated=result.hydrated_items,
        generated=result.stats.generated,
        ml_approved_pairs=sum(pair_decision.values()),
        ml_approved_articles=approved_articles,
        joined=result.stats.reinforced + result.stats.updated,
    )
    return {
        "candidates": result.candidates,
        "hydrated_items": result.hydrated_items,
        "shadow_pairs": shadow_pairs,
        "created": result.stats.created,
        "updated": result.stats.updated,
        "reinforced": result.stats.reinforced,
        "generated": result.stats.generated,
        "elapsed_seconds": elapsed,
    }


async def _embed_summaries(
    config: AppConfig, articles: Sequence[MemberArticle]
) -> dict[str, np.ndarray]:
    """見出し + 要約の埋込をその場で作る (シャドー観測用・永続化しない)。

    ⭐ 本文の埋込 (article_embeddings) は **触らない** — あれは意味的重複排除も
    使っているので、入れ替えると別の機能に影響する。群化の材料としては
    「書式の揃った要約」の方が効く (実測 +4pt) ので、2 本目として持つ。
    """
    from src.tools.embedding_client import OllamaEmbeddingClient
    from src.tools.model_tiers import resolve_embedding_model

    client = OllamaEmbeddingClient(base_url=config.ollama_base_url, model=resolve_embedding_model())

    out: dict[str, np.ndarray] = {}
    for art in articles:
        text = f"{art.title}\n\n{art.summary}".strip()
        if not text:
            continue
        try:
            res = await client.embed(text)
        except Exception as e:  # noqa: BLE001 — 1 件の失敗で観測を止めない
            _log.warning("summary_embed_failed", article_id=art.article_id, error=str(e)[:120])
            continue
        out[art.article_id] = np.asarray(res.vector, dtype=np.float32)
    return out


def _load_vectors(repo: RunHistoryRepository, article_ids: list[str]) -> dict[str, np.ndarray]:
    """記事 URL 経由で埋込を引く (article_embeddings は url_hash / url がキー)。"""
    if not article_ids:
        return {}
    out: dict[str, np.ndarray] = {}
    placeholders = ",".join("?" for _ in article_ids)
    with repo._connect() as conn:  # noqa: SLF001
        rows = conn.execute(
            "SELECT a.article_id, e.vector, e.dim FROM articles a"  # noqa: S608 — placeholders のみ
            f" JOIN article_embeddings e ON e.url = a.url WHERE a.article_id IN ({placeholders})",
            article_ids,
        ).fetchall()
    for aid, vec, dim in rows:
        v = np.frombuffer(bytes(vec), dtype=np.float32)
        if v.shape[0] == int(dim):
            out[str(aid)] = v / np.linalg.norm(v)
    return out


def _load_members(
    repo: RunHistoryRepository,
    article_ids: list[str],
    counts: Mapping[tuple[str, str], int],
) -> dict[str, MemberArticle]:
    """既存アイテムのメンバーを本文 + 結合信号 entity ごと復元する。"""
    if not article_ids:
        return {}
    join_ents = _join_entities_for(repo, article_ids, counts)
    placeholders = ",".join("?" for _ in article_ids)
    with repo._connect() as conn:  # noqa: SLF001
        rows = conn.execute(
            # ⚠ **全列に別名を付ける**: PG は dict 形式で行を返すため、別名の無い
            # COALESCE(...) は 4 列とも同じキーへ潰れ、11 列のはずが 8 列になる
            # (2026-08-24 の実障害: 既存アイテムの復元経路だけが落ちた)。
            f"SELECT a.article_id AS article_id,"  # noqa: S608
            f" {EVENT_TS_EXPR.format(a='a')} AS anchor_ts,"
            " a.importance AS importance, a.category AS category, a.status AS status,"
            " a.title AS title, a.url AS url,"
            " COALESCE(a.feed_title,'') AS feed_title,"
            " COALESCE(a.feed_url,'') AS feed_url,"
            " COALESCE(a.summary,'') AS summary,"
            " COALESCE(a.body,'') AS body,"
            " COALESCE(a.account_class,'') AS account_class"
            " FROM articles a"
            f" WHERE a.article_id IN ({placeholders})",
            article_ids,
        ).fetchall()
    return {
        str(r["article_id"]): _to_member(r, join_ents.get(str(r["article_id"]), frozenset()))
        for r in rows
    }


__all__ = ["run_eventnews_hourly"]
