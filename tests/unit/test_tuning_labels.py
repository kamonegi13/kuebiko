"""較正格子 P1 (self_evolving_tuning_design §6): tuning_labels repo + 遅延正解収穫のテスト。

ラベルは「LLM 出力の採点」でなく「外部正解との突合」を起点に定義する (設計 §2)。
収穫は週次 sweep (pull 型・冪等) — 人間が一切触らなくてもラベルが増え続けること (§12) を
dedup_key の冪等性テストで固定する。

不変条件14 (§5 ソース独立性、2026-08-22) のため ``_seed_article`` は claim 記事
(``subject_actor_source='feed'``) と news 記事とで既定ホストを分ける (実運用の
「犯行声明サイト」と「報道サイト」が別ドメインである状態を模す)。明示的に同一ホストを
与えれば自己突合ケースを再現できる。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.storage.run_history import RunHistoryRepository
from src.tuning.label_harvest import run_label_harvest

_NOW = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "tuning.db")


def _seed_article(
    repo: RunHistoryRepository,
    *,
    article_id: str,
    subject_actor_source: str = "none",
    subject_actor_ids: str = "",
    body_len: int = 600,
    article_type: str = "",
    victim_orgs: tuple[str, ...] = (),
    created_at: datetime = _NOW,
    published_at: datetime | None = None,
    title: str | None = None,
    url: str | None = None,
) -> None:
    """収穫の入力 (articles + article_entities) を直接 seed する (test 専用)。

    ``url`` 省略時、claim 記事 (``subject_actor_source='feed'``) と news 記事は
    既定で別ホストになる (不変条件14 の独立性ゲートを既存テストが自然に満たすため)。
    同一ホストの自己突合を再現したいテストは ``url`` を明示的に揃える。
    """
    iso = created_at.isoformat()
    if url is None:
        host = (
            "ransomware-leaksite.example"
            if subject_actor_source == "feed"
            else "news-outlet.example"
        )
        url = f"https://{host}/{article_id}"
    with repo._connect() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO runs (started_at, pipeline, dry_run, status) VALUES (?, 't', 0, 'done')",
            (iso,),
        )
        rid = conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
        conn.execute(
            "INSERT INTO articles (run_id, article_id, title, url, status, created_at,"
            " subject_actor_source, subject_actor_ids, body, article_type, published_at)"
            " VALUES (?,?,?,?, 'posted', ?, ?, ?, ?, ?, ?)",
            (
                rid,
                article_id,
                title if title is not None else f"title-{article_id}",
                url,
                iso,
                subject_actor_source,
                subject_actor_ids,
                "x" * body_len,
                article_type,
                published_at.isoformat() if published_at else None,
            ),
        )
        for org in victim_orgs:
            conn.execute(
                "INSERT INTO article_entities (article_id, entity_type, value, created_at)"
                " VALUES (?,?,?,?)",
                (article_id, "victim_org", org, iso),
            )


class TestTuningLabelRepo:
    def test_record_and_summarize(self, repo: RunHistoryRepository) -> None:
        # Arrange / Act
        first = repo.record_tuning_label(
            dedup_key="k1",
            field="subject_actor",
            label_value="qilin",
            source="E1",
            strength="strong",
            provenance=json.dumps({"producer": "test"}),
            article_id="a1",
        )
        repo.record_tuning_label(
            dedup_key="k2",
            field="editorial_stance",
            label_value="propaganda",
            source="E3",
            strength="strong",
            provenance="{}",
            article_id="a2",
        )
        # Assert
        assert first is not None
        rows = repo.summarize_tuning_labels()
        by_key = {(r["field"], r["source"]): r for r in rows}
        assert by_key[("subject_actor", "E1")]["active"] == 1
        assert by_key[("editorial_stance", "E3")]["active"] == 1

    def test_dedup_key_is_idempotent(self, repo: RunHistoryRepository) -> None:
        def _record() -> int | None:
            return repo.record_tuning_label(
                dedup_key="same",
                field="subject_actor",
                label_value="qilin",
                source="E1",
                strength="strong",
                provenance="{}",
            )

        assert _record() is not None
        assert _record() is None  # 既出 → 重複しない
        rows = repo.summarize_tuning_labels()
        assert rows[0]["total"] == 1

    def test_supersede_same_article_field_source(self, repo: RunHistoryRepository) -> None:
        old_id = repo.record_tuning_label(
            dedup_key="e1",
            field="editorial_stance",
            label_value="opinion",
            source="E3",
            strength="strong",
            provenance="{}",
            article_id="a1",
            supersede_same=True,
        )
        new_id = repo.record_tuning_label(
            dedup_key="e2",
            field="editorial_stance",
            label_value="propaganda",
            source="E3",
            strength="strong",
            provenance="{}",
            article_id="a1",
            supersede_same=True,
        )
        assert old_id is not None and new_id is not None
        rows = repo.list_tuning_labels(field="editorial_stance")
        by_id = {r["id"]: r for r in rows}
        assert by_id[old_id]["superseded_by"] == new_id
        assert by_id[new_id]["superseded_by"] is None
        # summarize は現行 (active) と総数 (total) を区別する
        summary = repo.summarize_tuning_labels()
        row = next(r for r in summary if r["field"] == "editorial_stance")
        assert row["active"] == 1
        assert row["total"] == 2

    def test_supersede_does_not_cross_articles(self, repo: RunHistoryRepository) -> None:
        a = repo.record_tuning_label(
            dedup_key="x1",
            field="subject_actor",
            label_value="qilin",
            source="E1",
            strength="strong",
            provenance="{}",
            article_id="a1",
            supersede_same=True,
        )
        repo.record_tuning_label(
            dedup_key="x2",
            field="subject_actor",
            label_value="akira",
            source="E1",
            strength="strong",
            provenance="{}",
            article_id="a2",
            supersede_same=True,
        )
        rows = repo.list_tuning_labels(field="subject_actor")
        assert all(r["superseded_by"] is None for r in rows)
        assert a is not None


class TestFeedNewsSubjectSweep:
    def test_matches_claim_and_news_within_window(self, repo: RunHistoryRepository) -> None:
        # Arrange: 犯行声明 (feed 帰属) と、同一 victim_org を 1 日後に報じたニュース
        _seed_article(
            repo,
            article_id="claim1",
            subject_actor_source="feed",
            subject_actor_ids="qilin",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=2),
        )
        _seed_article(
            repo,
            article_id="news1",
            victim_orgs=("acme corp",),  # 大文字小文字ゆれは吸収される
            created_at=_NOW - timedelta(days=1),
        )
        # Act
        result = run_label_harvest(repo, now=_NOW)
        # Assert
        assert result.feed_subject_new == 1
        rows = repo.list_tuning_labels(field="subject_actor")
        assert len(rows) == 1
        assert rows[0]["article_id"] == "news1"
        assert rows[0]["label_value"] == "qilin"
        assert rows[0]["source"] == "E1"

    def test_outside_window_is_not_matched(self, repo: RunHistoryRepository) -> None:
        _seed_article(
            repo,
            article_id="claim1",
            subject_actor_source="feed",
            subject_actor_ids="qilin",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=20),
        )
        _seed_article(
            repo,
            article_id="news1",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=1),
        )
        result = run_label_harvest(repo, now=_NOW)
        assert result.feed_subject_new == 0

    def test_conflicting_claims_are_skipped(self, repo: RunHistoryRepository) -> None:
        # 同一 org に別ギャングの声明が窓内に 2 つ → 正解を決められないので付与しない
        _seed_article(
            repo,
            article_id="claim1",
            subject_actor_source="feed",
            subject_actor_ids="qilin",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=2),
        )
        _seed_article(
            repo,
            article_id="claim2",
            subject_actor_source="feed",
            subject_actor_ids="akira",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=3),
        )
        _seed_article(
            repo,
            article_id="news1",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=1),
        )
        result = run_label_harvest(repo, now=_NOW)
        assert result.feed_subject_new == 0
        assert result.feed_subject_conflicts == 1

    def test_short_body_and_recap_are_excluded(self, repo: RunHistoryRepository) -> None:
        _seed_article(
            repo,
            article_id="claim1",
            subject_actor_source="feed",
            subject_actor_ids="qilin",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=2),
        )
        _seed_article(
            repo,
            article_id="stub",
            body_len=100,  # 500 字未満は判定材料にならない
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=1),
        )
        _seed_article(
            repo,
            article_id="recap1",
            article_type="recap",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=1),
        )
        result = run_label_harvest(repo, now=_NOW)
        assert result.feed_subject_new == 0

    def test_digest_articles_are_excluded(self, repo: RunHistoryRepository) -> None:
        """多数事件まとめ (ダイジェスト) には単一主題が無い — ラベルを付けない。

        eval script にあった除外の移植漏れで Grok ダイジェストへ誤ラベルが付いた
        (2026-08-22 backfill 監査で発覚) の回帰テスト。
        """
        _seed_article(
            repo,
            article_id="claim1",
            subject_actor_source="feed",
            subject_actor_ids="qilin",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=2),
        )
        _seed_article(
            repo,
            article_id="digest1",
            title="デイリー ランサムウェア ダイジェスト - 計 35 件",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=1),
        )
        result = run_label_harvest(repo, now=_NOW)
        assert result.feed_subject_new == 0

    def test_stale_claim_anchors_on_discovery_date(self, repo: RunHistoryRepository) -> None:
        """声明の時刻錨は発覚日 (published_at) — 数か月前の事件を遅れて掲載した声明が
        同一組織の別インシデント報道へ誤結合しない (LexisNexis 型、2026-08-22)。"""
        _seed_article(
            repo,
            article_id="stale_claim",
            subject_actor_source="feed",
            subject_actor_ids="fulcrumsec",
            victim_orgs=("LexisNexis",),
            created_at=_NOW - timedelta(days=1),  # 掲載は昨日
            published_at=_NOW - timedelta(days=100),  # 発覚は 100 日前
        )
        _seed_article(
            repo,
            article_id="new_incident_news",
            victim_orgs=("LexisNexis",),
            created_at=_NOW - timedelta(days=2),
        )
        result = run_label_harvest(repo, now=_NOW)
        assert result.feed_subject_new == 0  # 発覚日錨では窓外

    def test_generic_org_names_are_excluded(self, repo: RunHistoryRepository) -> None:
        """§13-1 併発欠陥: 一般語の組織名は同名衝突の誤結合源 — 突合キーにしない。"""
        _seed_article(
            repo,
            article_id="claim1",
            subject_actor_source="feed",
            subject_actor_ids="qilin",
            victim_orgs=("Hospital",),
            created_at=_NOW - timedelta(days=2),
        )
        _seed_article(
            repo,
            article_id="news1",
            victim_orgs=("hospital",),
            created_at=_NOW - timedelta(days=1),
        )
        assert run_label_harvest(repo, now=_NOW).feed_subject_new == 0

    def test_label_carries_snapshot_and_rubric_version(
        self, repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """§13-3 (学習テキスト凍結) + §13-1 (rubric 版の来歴 stamp)。"""
        import src.tuning.label_harvest as lh

        monkeypatch.setattr(lh, "_current_summarizer_rubric_version", lambda: 5)
        _seed_article(
            repo,
            article_id="claim1",
            subject_actor_source="feed",
            subject_actor_ids="qilin",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=2),
        )
        _seed_article(
            repo,
            article_id="news1",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=1),
        )
        assert run_label_harvest(repo, now=_NOW).feed_subject_new == 1
        with repo._connect() as conn:  # noqa: SLF001
            row = conn.execute(
                "SELECT snapshot, provenance FROM tuning_labels WHERE article_id='news1'"
            ).fetchone()
        snap = json.loads(row["snapshot"])
        assert snap["title"] == "title-news1"
        assert len(snap["body"]) >= 500  # 本文 purge 後も教材が残る
        assert json.loads(row["provenance"])["summarizer_rubric_version"] == 5

    def test_backfill_fills_missing_snapshots(self, repo: RunHistoryRepository) -> None:
        _seed_article(repo, article_id="a1", victim_orgs=(), created_at=_NOW)
        repo.record_tuning_label(
            dedup_key="old-label",
            field="subject_actor",
            label_value="qilin",
            source="E1",
            strength="strong",
            provenance="{}",
            article_id="a1",
        )
        from src.tuning.label_harvest import _backfill_snapshots

        assert _backfill_snapshots(repo) == 1
        assert _backfill_snapshots(repo) == 0  # 冪等
        rows = repo.list_tuning_labels(field="subject_actor")
        assert rows and rows[0]["id"] is not None
        with repo._connect() as conn:  # noqa: SLF001
            snap = conn.execute(
                "SELECT snapshot FROM tuning_labels WHERE article_id='a1'"
            ).fetchone()
        assert snap["snapshot"] is not None

    def test_sweep_is_idempotent(self, repo: RunHistoryRepository) -> None:
        _seed_article(
            repo,
            article_id="claim1",
            subject_actor_source="feed",
            subject_actor_ids="qilin",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=2),
        )
        _seed_article(
            repo,
            article_id="news1",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=1),
        )
        first = run_label_harvest(repo, now=_NOW)
        second = run_label_harvest(repo, now=_NOW)
        assert first.feed_subject_new == 1
        assert second.feed_subject_new == 0  # 再実行で増えない
        assert len(repo.list_tuning_labels(field="subject_actor")) == 1


class TestSourceIndependenceGate:
    """不変条件14 (§5・§10.2e、2026-08-22): 突合の両辺が同一ソースなら数えない。"""

    def test_same_host_match_is_skipped(self, repo: RunHistoryRepository) -> None:
        # claim/news を同一ホスト (www. の有無だけ違う) にした自己突合
        _seed_article(
            repo,
            article_id="claim1",
            subject_actor_source="feed",
            subject_actor_ids="qilin",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=2),
            url="https://www.ransomware.live/claim1",
        )
        _seed_article(
            repo,
            article_id="news1",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=1),
            url="https://ransomware.live/news1",
        )
        result = run_label_harvest(repo, now=_NOW)
        assert result.feed_subject_new == 0
        assert result.feed_subject_self_source_skipped == 1
        assert repo.list_tuning_labels(field="subject_actor") == []

    def test_different_host_still_matches(self, repo: RunHistoryRepository) -> None:
        # _seed_article の既定ホスト (claim=leaksite / news=news-outlet) で独立性を満たす
        _seed_article(
            repo,
            article_id="claim1",
            subject_actor_source="feed",
            subject_actor_ids="qilin",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=2),
        )
        _seed_article(
            repo,
            article_id="news1",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=1),
        )
        result = run_label_harvest(repo, now=_NOW)
        assert result.feed_subject_new == 1
        assert result.feed_subject_self_source_skipped == 0

    def test_news_on_collector_surface_is_skipped_even_when_claim_host_differs(
        self, repo: RunHistoryRepository
    ) -> None:
        """収集器の配信面判定 (2026-08-22 初回運転の実測が根拠)。

        claim 行の URL はリークサイトの .onion、news は収集器の配信面
        (www.ransomware.live の RSS 記事) — host 直接比較では独立に見えるが、
        両方とも同一収集器由来なので独立証拠にならない。
        """
        # 突合に使う claim は .onion ホスト
        _seed_article(
            repo,
            article_id="claim_onion",
            subject_actor_source="feed",
            subject_actor_ids="qilin",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=2),
            url="https://leaksite0000.onion/claim",
        )
        # 収集器は配信面 (ransomware.live) にも記事を持つ → 配信面 host 集合に入る
        _seed_article(
            repo,
            article_id="claim_surface",
            subject_actor_source="feed",
            subject_actor_ids="cl0p",
            victim_orgs=("Other Org",),
            created_at=_NOW - timedelta(days=30),
            url="https://www.ransomware.live/other",
        )
        # news は配信面ホスト上の RSS 記事 — claim (.onion) と host は違うが同一収集器
        _seed_article(
            repo,
            article_id="news1",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=1),
            url="https://ransomware.live/news1",
        )
        result = run_label_harvest(repo, now=_NOW)
        assert result.feed_subject_new == 0
        assert result.feed_subject_self_source_skipped == 1
        assert repo.list_tuning_labels(field="subject_actor") == []


class TestReclassifySelfSourceLabels:
    """不変条件14: 既存の自己突合ラベルを削除せず strength で隔離する再分類関数。"""

    def test_reclassifies_self_source_and_is_idempotent(self, repo: RunHistoryRepository) -> None:
        from src.tuning.label_harvest import reclassify_self_source_labels

        # 独立性ゲート実装前に書かれた既存ラベルを模す (claim/news 同一ホスト)
        _seed_article(repo, article_id="claim1", url="https://ransomware.live/claim1")
        _seed_article(repo, article_id="news1", url="https://www.ransomware.live/news1")
        repo.record_tuning_label(
            dedup_key="legacy-self-match",
            field="subject_actor",
            label_value="qilin",
            source="E1",
            strength="strong",
            provenance=json.dumps(
                {"producer": "feed_victim_org_match", "claim_article_id": "claim1"}
            ),
            article_id="news1",
        )

        assert reclassify_self_source_labels(repo) == 1
        rows = repo.list_tuning_labels(field="subject_actor")
        assert rows[0]["strength"] == "self_source"
        # 冪等: 再実行では対象が残っていないので増えない
        assert reclassify_self_source_labels(repo) == 0

    def test_independent_label_is_left_untouched(self, repo: RunHistoryRepository) -> None:
        from src.tuning.label_harvest import reclassify_self_source_labels

        _seed_article(repo, article_id="claim1", url="https://ransomware.live/claim1")
        _seed_article(repo, article_id="news1", url="https://news-outlet.example/news1")
        repo.record_tuning_label(
            dedup_key="legit",
            field="subject_actor",
            label_value="qilin",
            source="E1",
            strength="strong",
            provenance=json.dumps(
                {"producer": "feed_victim_org_match", "claim_article_id": "claim1"}
            ),
            article_id="news1",
        )

        assert reclassify_self_source_labels(repo) == 0
        rows = repo.list_tuning_labels(field="subject_actor")
        assert rows[0]["strength"] == "strong"

    def test_reclassifies_collector_surface_news_even_if_claim_row_is_gone(
        self, repo: RunHistoryRepository
    ) -> None:
        """claim 行が消えていても、news 側が収集器の配信面なら隔離できる (規則①)。

        初回運転で 43 件中 26 件しか隔離できなかった原因 (claim=.onion / news=配信面で
        host 不一致、または claim 行の不在) への対処。
        """
        from src.tuning.label_harvest import reclassify_self_source_labels

        # 配信面 host 集合の供給源 (feed 記事が ransomware.live 上にもある)
        _seed_article(
            repo,
            article_id="surface_claim",
            subject_actor_source="feed",
            url="https://www.ransomware.live/surface",
        )
        # news は配信面上の記事。provenance の claim は存在しない記事を指す
        _seed_article(repo, article_id="news1", url="https://ransomware.live/news1")
        repo.record_tuning_label(
            dedup_key="legacy-onion-claim",
            field="subject_actor",
            label_value="qilin",
            source="E1",
            strength="strong",
            provenance=json.dumps(
                {"producer": "feed_victim_org_match", "claim_article_id": "gone_claim"}
            ),
            article_id="news1",
        )

        assert reclassify_self_source_labels(repo) == 1
        rows = repo.list_tuning_labels(field="subject_actor")
        assert rows[0]["strength"] == "self_source"

    def test_run_label_harvest_reclassifies_at_start(self, repo: RunHistoryRepository) -> None:
        """週次収穫の冒頭で自動的に再分類が走る (dry_run では走らない)。"""
        _seed_article(repo, article_id="claim1", url="https://ransomware.live/claim1")
        _seed_article(repo, article_id="news1", url="https://www.ransomware.live/news1")
        repo.record_tuning_label(
            dedup_key="legacy-self-match",
            field="subject_actor",
            label_value="qilin",
            source="E1",
            strength="strong",
            provenance=json.dumps(
                {"producer": "feed_victim_org_match", "claim_article_id": "claim1"}
            ),
            article_id="news1",
        )

        dry = run_label_harvest(repo, now=_NOW, dry_run=True)
        assert dry.self_source_reclassified == 0
        rows = repo.list_tuning_labels(field="subject_actor")
        assert rows[0]["strength"] == "strong"

        result = run_label_harvest(repo, now=_NOW)
        assert result.self_source_reclassified == 1
        rows = repo.list_tuning_labels(field="subject_actor")
        assert rows[0]["strength"] == "self_source"


class TestFetchSubjectPanelCases:
    """不変条件14: パネル・few-shot の入力候補は自己突合ラベルを読まない。"""

    def test_self_source_labels_are_excluded(self, repo: RunHistoryRepository) -> None:
        for aid in ("selfsrc", "indep"):
            _seed_article(repo, article_id=aid, body_len=600, created_at=_NOW)
        repo.record_tuning_label(
            dedup_key="self1",
            field="subject_actor",
            label_value="qilin",
            source="E1",
            strength="self_source",
            provenance="{}",
            article_id="selfsrc",
        )
        repo.record_tuning_label(
            dedup_key="indep1",
            field="subject_actor",
            label_value="qilin",
            source="E1",
            strength="strong",
            provenance="{}",
            article_id="indep",
        )
        since_iso = (_NOW - timedelta(days=5)).isoformat()
        cases = repo.fetch_subject_panel_cases(since_iso)
        assert {c["article_id"] for c in cases} == {"indep"}


class TestE3Sweeps:
    def test_taxonomy_decisions_become_labels(self, repo: RunHistoryRepository) -> None:
        from src.storage.records import TaxonomyProposalRecord

        pid = repo.insert_taxonomy_proposal(
            TaxonomyProposalRecord(
                proposal_type="pattern_4",
                tier="tier_1_auto",
                target_yaml="victim_sectors",
                target_canonical="energy",
                proposed_change='{"kind": "add_alias"}',
                rationale="typo",
                confidence="high",
            )
        )
        repo.update_taxonomy_proposal_status(pid, status="accepted")
        # pending の提案はラベルにならない
        repo.insert_taxonomy_proposal(
            TaxonomyProposalRecord(
                proposal_type="pattern_5",
                tier="tier_2_review",
                target_yaml="actor_aliases",
                target_canonical="foo",
                proposed_change='{"kind": "new_actor"}',
                rationale="x",
                confidence="low",
            )
        )
        result = run_label_harvest(repo, now=_NOW)
        assert result.taxonomy_new == 1
        rows = repo.list_tuning_labels(field="taxonomy_decision")
        assert len(rows) == 1
        assert rows[0]["label_value"] == "accepted"
        assert rows[0]["source"] == "E3"
        # 冪等
        assert run_label_harvest(repo, now=_NOW).taxonomy_new == 0

    def test_editorial_corrections_become_labels(self, repo: RunHistoryRepository) -> None:
        repo.upsert_editorial_stance_review(
            article_id="a1",
            original_stance="factual_report",
            corrected_stance="propaganda",
            comment="国営メディアの引き写し",
        )
        result = run_label_harvest(repo, now=_NOW)
        assert result.editorial_new == 1
        rows = repo.list_tuning_labels(field="editorial_stance")
        assert len(rows) == 1
        assert rows[0]["article_id"] == "a1"
        assert rows[0]["label_value"] == "propaganda"
        assert rows[0]["source"] == "E3"
        # 自由文コメントは provenance に持ち込まない (§4 マスク方針: 書込側で遮断)
        assert "国営" not in rows[0]["provenance"]

    def test_editorial_recorrection_supersedes(self, repo: RunHistoryRepository) -> None:
        repo.upsert_editorial_stance_review(
            article_id="a1",
            original_stance="factual_report",
            corrected_stance="opinion",
            comment="",
        )
        run_label_harvest(repo, now=_NOW)
        repo.upsert_editorial_stance_review(
            article_id="a1",
            original_stance="factual_report",
            corrected_stance="propaganda",
            comment="",
        )
        run_label_harvest(repo, now=_NOW)
        rows = repo.list_tuning_labels(field="editorial_stance")
        active = [r for r in rows if r["superseded_by"] is None]
        assert len(active) == 1
        assert active[0]["label_value"] == "propaganda"

    def test_dry_run_writes_nothing(self, repo: RunHistoryRepository) -> None:
        repo.upsert_editorial_stance_review(
            article_id="a1",
            original_stance=None,
            corrected_stance="propaganda",
            comment="",
        )
        result = run_label_harvest(repo, now=_NOW, dry_run=True)
        assert result.editorial_new == 1  # 候補数は報告する
        assert repo.list_tuning_labels() == []  # 書き込まない
