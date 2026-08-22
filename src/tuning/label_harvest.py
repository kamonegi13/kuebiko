"""遅延正解ラベルの週次収穫 (較正格子 P1 — docs/self_evolving_tuning_design.md §4 C1)。

「当時の判定 vs 後日確定した事実」を突合して tuning_labels に書く。producer は全て
sweep 型 (pull・冪等) — push hook と違い過去分も自動で拾え、人間が一切触らなくても
ラベルが増え続ける (§12 人間非介在の縮退設計)。1 producer の失敗で他を止めない。

P1 の producer (§10.3 の決定):
- ①feed 突合 (E1): 犯行声明 (feed 帰属) と同一 victim_org を ±5 日に報じたニュース記事へ
  主体アクターのラベルを付与 (eval_judgment_subject_recall 2026-08-21 の実証手法)。
- ②MITRE 確定 (E1): mitre_sync の自動適用 hook が書く (本モジュール外、sync 時に確定)。
- E3 合流 (§11-A): taxonomy 採否・editorial 訂正の人間裁定をラベル化。

日付の突合は Python で行う (SQL の per-row 日付演算は SQLite/PG で方言が割れるため。
sqlite_pg_bare_column_group_by 2026-08-04 の教訓 — 可搬でない SQL を書かない)。

**不変条件14 (ソース独立性、§5・§10.2e、2026-08-22)**: E1 ラベル 51 件中 43 件が
突合の両辺とも www.ransomware.live 由来の自己突合だった (実測)。遠隔監督ラベルは
突合の両辺が独立ソースであることを構造的に保証する — 同一ソース・同一収集器由来の
突合はラベルとして数えない。``subject_backfill.py`` (本番の帰属補完) は対象外
(ransomware.live 自記事にギャング名を付けるのは帰属としては正しい。禁止されるのは
それを独立ラベルとして数えることだけ)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from src.cti.ioc_source_filter import normalize_host
from src.logging_config import get_logger

_log = get_logger(__name__)

# 犯行声明とニュース記事を同一事象とみなす時間窓 (±日)。eval 2026-08-21 と同値。
_MATCH_WINDOW_DAYS = 5
# 収穫の遡り日数 (週次実行の取りこぼし・遅延到着に余裕を持たせる)
_LOOKBACK_DAYS = 35
# snapshot に凍結する本文長 (judgment の _BODY_MAX_CHARS と同水準)
_SNAPSHOT_BODY_CHARS = 8000
# 一般語の組織名は同名衝突で誤結合しやすい (§13-1 併発欠陥)。完全一致で除外する
# 最小 denylist — victim_org 抽出側の is_vendor_noise とは別軸 (あちらはベンダ言及)
_GENERIC_ORG_DENYLIST = frozenset(
    {
        "government",
        "ministry",
        "police",
        "hospital",
        "university",
        "school",
        "bank",
        "city",
        "council",
        "市役所",
        "病院",
        "大学",
        "政府",
        "警察",
        "学校",
    }
)


def _snapshot_json(title: str, body: str, category: str) -> str:
    """学習テキストの凍結 (§13-3)。本文 90 日 purge 後も教材として残す JSON。"""
    return json.dumps(
        {
            "title": title.strip()[:300],
            "body": " ".join(body.split())[:_SNAPSHOT_BODY_CHARS],
            "category": category,
        },
        ensure_ascii=False,
    )


def _current_summarizer_rubric_version() -> int | None:
    """突合キー (victim_org) の供給源 = summarizer の現行 rubric 版 (§13-1 閉ループ対策)。

    ラベル来歴に記録することで、版間のラベル供給量ドリフトを事後に層別監査できる。
    """
    try:
        from src.storage.config_store import list_history

        history = list_history("summarizer_rubric", limit=1)
        return history[0].version if history else None
    except Exception:  # noqa: BLE001 — 版が読めなくても収穫は続行
        return None


class _LabelRepo(Protocol):
    """収穫が必要とする repo 面 (RunHistoryRepository のサブセット)。"""

    def record_tuning_label(
        self,
        *,
        dedup_key: str,
        field: str,
        label_value: str,
        source: str,
        strength: str,
        provenance: str,
        article_id: str | None = ...,
        arrived_at: datetime | None = ...,
        supersede_same: bool = ...,
        snapshot: str | None = ...,
    ) -> int | None: ...

    def list_labels_missing_snapshot(self) -> list[dict[str, Any]]: ...

    def set_label_snapshot(self, label_id: int, snapshot: str) -> None: ...

    def fetch_feed_subject_claims(self, since_iso: str) -> list[dict[str, Any]]: ...

    def fetch_victim_org_news_candidates(self, since_iso: str) -> list[dict[str, Any]]: ...

    def list_e1_subject_labels_not_self_source(self) -> list[dict[str, Any]]: ...

    def fetch_article_url(self, article_id: str) -> str | None: ...

    def mark_label_self_source(self, label_id: int) -> None: ...

    def list_taxonomy_proposals(
        self, *, status: str | None = ..., tier: str | None = ..., limit: int = ...
    ) -> list[Any]: ...

    def list_editorial_stance_reviews(self) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class LabelHarvestResult:
    """1 回の収穫の実行結果 (producer 別の新規件数)。"""

    feed_subject_new: int = 0
    feed_subject_conflicts: int = 0
    # 不変条件14 (§5): 突合の両辺が同一ソースだったためラベルを書かなかった件数
    feed_subject_self_source_skipped: int = 0
    taxonomy_new: int = 0
    editorial_new: int = 0
    # 不変条件14 (§5): 既存ラベルのうち今回 strength='self_source' に再分類した件数
    self_source_reclassified: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total_new(self) -> int:
        return self.feed_subject_new + self.taxonomy_new + self.editorial_new


def _as_dt(value: Any) -> datetime | None:
    """created_at の両 backend 対応 (SQLite=TEXT / PG=TIMESTAMPTZ or TEXT)。"""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return None


def _record(
    repo: _LabelRepo,
    *,
    dry_run: bool,
    dedup_key: str,
    label_field: str,
    label_value: str,
    source: str,
    provenance: dict[str, Any],
    article_id: str | None = None,
    arrived_at: datetime | None = None,
    supersede_same: bool = False,
    snapshot: str | None = None,
) -> bool:
    """1 ラベルの書き込み。dry_run は候補として数えるだけで書かない。"""
    if dry_run:
        return True
    new_id = repo.record_tuning_label(
        dedup_key=dedup_key,
        field=label_field,
        label_value=label_value,
        source=source,
        strength="strong",
        provenance=json.dumps(provenance, ensure_ascii=False),
        article_id=article_id,
        arrived_at=arrived_at,
        supersede_same=supersede_same,
        snapshot=snapshot,
    )
    return new_id is not None


def _sweep_feed_news_subject(
    repo: _LabelRepo,
    *,
    now: datetime,
    dry_run: bool,
    lookback_days: int = _LOOKBACK_DAYS,
) -> tuple[int, int, int]:
    """収穫① (E1): 犯行声明 × ニュース記事の victim_org ±5 日突合 → 主体ラベル。

    同一 org へ別ギャングの声明が窓内に複数ある場合は正解を決められないので付与しない
    (conflict として数える — 保守側の既定)。

    不変条件14 (§5 ソース独立性、2026-08-22): 突合候補のうち claim と news が
    同一ホスト由来のものは独立証拠にならない。同一 gt に複数の claim 候補がある場合は
    host が異なる (= 独立ソースの) 候補を優先して採用し、独立候補が 1 つも無ければ
    突合自体をスキップする (ラベルを書かない、self_source_skipped として計上)。
    """
    since_iso = (now - timedelta(days=lookback_days)).isoformat()
    claims = repo.fetch_feed_subject_claims(since_iso)
    news = repo.fetch_victim_org_news_candidates(since_iso)

    rubric_version = _current_summarizer_rubric_version()
    claims_by_org: dict[str, list[tuple[str, str, datetime, str]]] = {}
    for c in claims:
        ts = _as_dt(c["created_at"])
        gt = str(c["gt"]).split(",")[0].strip()
        if ts is None or not gt or not c["org"]:
            continue
        if c["org"] in _GENERIC_ORG_DENYLIST:
            continue  # 一般語の組織名は同名衝突の誤結合源 (§13-1)
        claims_by_org.setdefault(str(c["org"]), []).append(
            (str(c["article_id"]), gt, ts, str(c.get("url", "")))
        )

    new_count = 0
    conflicts = 0
    self_source_skipped = 0
    window = timedelta(days=_MATCH_WINDOW_DAYS)
    for n in news:
        n_ts = _as_dt(n["created_at"])
        org = str(n["org"])
        if n_ts is None or org not in claims_by_org:
            continue
        matched = [
            (claim_id, gt, c_url)
            for claim_id, gt, c_ts, c_url in claims_by_org[org]
            if abs(n_ts - c_ts) <= window
        ]
        if not matched:
            continue
        gts = {gt for _, gt, _ in matched}
        if len(gts) > 1:
            conflicts += 1
            continue
        gt = next(iter(gts))

        # 不変条件14: news 側と host が異なる (独立ソースの) claim 候補だけを採用対象にする。
        # www. の有無等の素朴な揺らぎは normalize_host (既存 IOC 出典判定と同じ流儀。
        # 完全な registrable domain 比較ではないが個人運用規模では十分) で吸収する。
        news_host = normalize_host(str(n.get("url", "")))
        independent = [
            (claim_id, c_url)
            for claim_id, _, c_url in matched
            if normalize_host(c_url) != news_host
        ]
        if not independent:
            self_source_skipped += 1
            continue
        claim_article_id = independent[0][0]

        inserted = _record(
            repo,
            dry_run=dry_run,
            dedup_key=f"feedmatch:{n['article_id']}:{gt}",
            label_field="subject_actor",
            label_value=gt,
            source="E1",
            article_id=str(n["article_id"]),
            arrived_at=n_ts,
            supersede_same=True,
            provenance={
                "producer": "feed_victim_org_match",
                "claim_article_id": claim_article_id,
                "org": org,
                "window_days": _MATCH_WINDOW_DAYS,
                # §13-1: 突合キー (victim_org) は summarizer LLM 出力 — 供給時の rubric 版を
                # 来歴に残し、版間のラベル供給ドリフトを事後監査できるようにする
                "summarizer_rubric_version": rubric_version,
            },
            snapshot=_snapshot_json(
                str(n.get("title", "")), str(n.get("body", "")), str(n.get("category", ""))
            ),
        )
        new_count += int(inserted)
    return new_count, conflicts, self_source_skipped


def reclassify_self_source_labels(repo: _LabelRepo) -> int:
    """既存の E1 主体ラベルのうち自己突合分を非破壊に隔離する (不変条件14、一回性/冪等)。

    実測で E1 ラベル 51 件中 43 件が突合の両辺とも www.ransomware.live 由来の自己突合
    だった (docs/self_evolving_tuning_design.md §10.2e)。ラベル自体・provenance (来歴) は
    変更せず、``strength`` を ``'self_source'`` に更新するだけで消費者
    (panel / few-shot / supply_sentinel) から構造的に除外する。

    走査対象は ``strength <> 'self_source'`` のみなので、一度隔離した行は次回以降
    対象から外れる (= 自然に冪等)。収穫 sweep の冒頭で毎回呼んでよい。
    """
    reclassified = 0
    for row in repo.list_e1_subject_labels_not_self_source():
        try:
            provenance = json.loads(str(row["provenance"]))
        except (TypeError, ValueError):
            continue
        claim_article_id = provenance.get("claim_article_id")
        if not claim_article_id:
            continue
        claim_url = repo.fetch_article_url(str(claim_article_id))
        if not claim_url:
            continue  # claim 記事が既に無い → host 判定不能、保守的に据え置く
        news_host = normalize_host(str(row.get("url", "")))
        if news_host and news_host == normalize_host(claim_url):
            repo.mark_label_self_source(int(row["id"]))
            reclassified += 1
    if reclassified:
        _log.info("label_self_source_reclassified", count=reclassified)
    return reclassified


def _sweep_taxonomy_decisions(repo: _LabelRepo, *, dry_run: bool) -> int:
    """E3 合流 (§11-A): taxonomy 提案への人間の採否をラベル化 (保留は裁定ではない)。"""
    new_count = 0
    for status in ("accepted", "rejected"):
        for p in repo.list_taxonomy_proposals(status=status, limit=1000):
            if p.id is None:
                continue
            inserted = _record(
                repo,
                dry_run=dry_run,
                dedup_key=f"taxonomy:{p.id}:{status}",
                label_field="taxonomy_decision",
                label_value=status,
                source="E3",
                arrived_at=p.reviewed_at,
                provenance={
                    "producer": "taxonomy_review",
                    "proposal_id": p.id,
                    "proposal_type": p.proposal_type,
                    "target_yaml": p.target_yaml,
                    "target_canonical": p.target_canonical,
                },
            )
            new_count += int(inserted)
    return new_count


def _sweep_editorial_corrections(repo: _LabelRepo, *, dry_run: bool) -> int:
    """E3 合流 (§11-A): editorial 論調訂正をラベル化 (write-only 解消)。

    自由文コメントは provenance に持ち込まない (§4 マスク方針を書込側で満たす)。
    再訂正は supersede_same で現行 1 本に保つ。
    """
    new_count = 0
    for r in repo.list_editorial_stance_reviews():
        corrected = str(r["corrected_stance"])
        body = str(r.get("body", ""))
        inserted = _record(
            repo,
            dry_run=dry_run,
            dedup_key=f"editorial:{r['article_id']}:{corrected}",
            label_field="editorial_stance",
            label_value=corrected,
            source="E3",
            article_id=str(r["article_id"]),
            arrived_at=_as_dt(r["created_at"]),
            supersede_same=True,
            provenance={
                "producer": "editorial_review",
                "original_stance": r["original_stance"],
            },
            snapshot=(
                _snapshot_json(str(r.get("title", "")), body, str(r.get("category", "")))
                if body
                else None
            ),
        )
        new_count += int(inserted)
    return new_count


def _backfill_snapshots(repo: _LabelRepo) -> int:
    """snapshot 未凍結ラベルへ本文が残っているうちに写経する (§13-3、冪等)。"""
    filled = 0
    for row in repo.list_labels_missing_snapshot():
        repo.set_label_snapshot(
            int(row["id"]),
            _snapshot_json(str(row["title"]), str(row["body"]), str(row["category"])),
        )
        filled += 1
    if filled:
        _log.info("label_snapshot_backfilled", count=filled)
    return filled


def run_label_harvest(
    repo: _LabelRepo,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    lookback_days: int = _LOOKBACK_DAYS,
) -> LabelHarvestResult:
    """全 producer を実行する (週次 pipeline から呼ばれる)。

    1 producer の例外は errors に落として残りを続行する (収穫全体を止めない)。
    ``lookback_days`` は過去データ一括収穫 (backfill) 用に広げられる (冪等)。
    上限の目安は body 保持 90 日 (それ以前は突合先の本文が purge 済み)。
    """
    base = now or datetime.now(UTC)
    errors: list[str] = []
    feed_new = feed_conflicts = feed_self_skipped = taxonomy_new = editorial_new = 0
    reclassified = 0

    if not dry_run:
        # 不変条件14 (§5): 既存の自己突合ラベルの隔離を毎回冒頭で試みる (冪等)。
        try:
            reclassified = reclassify_self_source_labels(repo)
        except Exception as e:  # noqa: BLE001 — 1 producer の失敗で他を止めない
            _log.warning("label_harvest_reclassify_failed", error=str(e))
            errors.append(f"reclassify: {type(e).__name__}: {e}")

    try:
        feed_new, feed_conflicts, feed_self_skipped = _sweep_feed_news_subject(
            repo, now=base, dry_run=dry_run, lookback_days=lookback_days
        )
    except Exception as e:  # noqa: BLE001 — 1 producer の失敗で他を止めない
        _log.warning("label_harvest_feed_sweep_failed", error=str(e))
        errors.append(f"feed_subject: {type(e).__name__}: {e}")

    try:
        taxonomy_new = _sweep_taxonomy_decisions(repo, dry_run=dry_run)
    except Exception as e:  # noqa: BLE001
        _log.warning("label_harvest_taxonomy_sweep_failed", error=str(e))
        errors.append(f"taxonomy: {type(e).__name__}: {e}")

    try:
        editorial_new = _sweep_editorial_corrections(repo, dry_run=dry_run)
    except Exception as e:  # noqa: BLE001
        _log.warning("label_harvest_editorial_sweep_failed", error=str(e))
        errors.append(f"editorial: {type(e).__name__}: {e}")

    if not dry_run:
        try:
            _backfill_snapshots(repo)  # 本文 purge 前に学習テキストを凍結 (§13-3)
        except Exception as e:  # noqa: BLE001
            _log.warning("label_snapshot_backfill_failed", error=str(e))
            errors.append(f"snapshot: {type(e).__name__}: {e}")

    result = LabelHarvestResult(
        feed_subject_new=feed_new,
        feed_subject_conflicts=feed_conflicts,
        feed_subject_self_source_skipped=feed_self_skipped,
        taxonomy_new=taxonomy_new,
        editorial_new=editorial_new,
        self_source_reclassified=reclassified,
        errors=errors,
    )
    _log.info(
        "label_harvest_complete",
        feed_subject_new=result.feed_subject_new,
        feed_subject_conflicts=result.feed_subject_conflicts,
        feed_subject_self_source_skipped=result.feed_subject_self_source_skipped,
        self_source_reclassified=result.self_source_reclassified,
        taxonomy_new=result.taxonomy_new,
        editorial_new=result.editorial_new,
        dry_run=dry_run,
        error_count=len(errors),
    )
    return result
