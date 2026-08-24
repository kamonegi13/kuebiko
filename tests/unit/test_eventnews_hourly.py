"""毎時運用の増分処理 — 復元と候補限定の不変条件。

設計 SSoT: docs/event_news_design.md §12/§14b。リプレイと本番で同じ
``runner.process_candidates`` を使うため、この層の責務は「状態の復元」と
「候補の限定」のみ。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import cast

import numpy as np

from src.eventnews.hourly import hydrate_open_items, run_hourly
from src.eventnews.models import ItemState, MemberArticle
from src.storage.repo_eventnews import EventNewsMixin


class _FakeRepo:
    def __init__(self, records: list[object]) -> None:
        self._records = records
        self.created: list[str] = []
        self.members: list[tuple[str, str]] = []
        self.updates: list[str] = []

    def list_event_items(self, **_: object) -> list[object]:
        return self._records

    def create_event_item(self, *, item_id: str, **_: object) -> None:
        self.created.append(item_id)

    def add_event_member(self, *, item_id: str, article_id: str, **_: object) -> None:
        self.members.append((item_id, article_id))

    def update_event_item(self, item_id: str, _fields: dict[str, object]) -> int:
        self.updates.append(item_id)
        return 1

    def record_event_version(self, **_: object) -> None:  # pragma: no cover - 生成なし経路
        raise AssertionError("生成は起きない想定")


class _Rec:
    def __init__(self, state: ItemState, merged_into: str | None = None) -> None:
        self.state = state
        self.merged_into = merged_into


def _member(aid: str, *, hours_ago: int = 0) -> MemberArticle:
    return MemberArticle(
        article_id=aid,
        title=f"t-{aid}",
        url=f"https://e/{aid}",
        feed_title="F",
        feed_url="https://f",
        host="e",
        importance="high",
        category="apt",
        status="posted",
        anchor_ts=datetime.now(UTC) - timedelta(hours=hours_ago),
        summary="s",
        body="",
        entities=frozenset({("cve", "cve-2026-1111")}),
    )


def _state(item_id: str, *, hours_ago: int, members: tuple[str, ...]) -> ItemState:
    ts = datetime.now(UTC) - timedelta(hours=hours_ago)
    return ItemState(
        item_id=item_id,
        first_reported_at=ts,
        last_reported_at=ts,
        status="new",
        importance="high",
        current_version=1,
        member_ids=members,
    )


class TestHydrate:
    def test_items_outside_the_window_are_not_hydrated(self) -> None:
        recent = _Rec(_state("ev-recent", hours_ago=1, members=("a",)))
        stale = _Rec(_state("ev-stale", hours_ago=24 * 30, members=("b",)))
        repo = _FakeRepo([recent, stale])

        out = hydrate_open_items(
            cast(EventNewsMixin, repo), lambda ids: {i: _member(i) for i in ids}
        )

        assert [s.item_id for s, _ in out] == ["ev-recent"]

    def test_merged_items_are_not_hydrated(self) -> None:
        """墓標 (merged_into) のアイテムは参加先にしない。"""
        rec = _Rec(_state("ev-old", hours_ago=1, members=("a",)), merged_into="ev-new")
        repo = _FakeRepo([rec])

        assert (
            hydrate_open_items(cast(EventNewsMixin, repo), lambda ids: {i: _member(i) for i in ids})
            == []
        )


class TestCandidateLimiting:
    def test_articles_already_members_are_skipped(self) -> None:
        """既存メンバーを候補に含めても二重処理しない (毎時実行の冪等性)。"""
        state = _state("ev-1", hours_ago=2, members=("a",))
        existing = [(state, [_member("a", hours_ago=2)])]
        repo = _FakeRepo([])
        cands = [_member("a", hours_ago=2), _member("b", hours_ago=1)]
        vectors = {"a": np.ones(4, dtype=np.float32), "b": np.ones(4, dtype=np.float32)}

        res = asyncio.run(run_hourly(cast(EventNewsMixin, repo), cands, vectors, existing, None))

        assert res.candidates == 1  # b のみ
        assert ("ev-1", "b") in repo.members  # 既存アイテムへ合流した
        assert repo.created == []  # 新規アイテムを作っていない


class TestJobRegistration:
    def test_job_is_registered_with_a_dispatch_target(self) -> None:
        """pipelines.yaml だけでは scheduler に載らない — JobDef と dispatch の両方を固定する。"""
        from src.scheduler.job_registry import load_jobs

        job = next((j for j in load_jobs() if j.id == "eventnews-hourly"), None)
        assert job is not None, "JobDef が無い"
        assert job.interval_minutes == 60
        assert job.offset_minutes == 20, "収集(:00)の後・翻訳(:15)と scraper(:30)の間"
        assert job.protection == "optional", "止めても配信に影響しない (v1 は出口が無い)"

    def test_flag_disables_the_job_entirely(self, monkeypatch: object) -> None:
        import asyncio

        from src.ui.services.eventnews_hourly_job import run_eventnews_hourly

        monkeypatch.setenv("EVENTNEWS_HOURLY", "0")  # type: ignore[attr-defined]
        assert asyncio.run(run_eventnews_hourly()) == {"skipped": "flag_off"}


# --- 毎時ジョブの取得層 (DB 形状) の不変条件 -------------------------------
#
# 2026-08-24 の本番不発: 既存アイテムのメンバーを復元する際、entity を
# ``article_entities.created_at >= 6時間前`` で絞っていたため、窓内の古い
# メンバー (最大 72h 前) の entity が **常に空**になり、「共有 entity >= 1」が
# 永久に不成立 → 合流が一度も起きず、25 アイテム全件が単独記事のまま
# 蓄積していた。ジョブは毎回 succeeded を返すため、外形では気付けない。


def _seed_run(conn: object) -> None:
    conn.execute(  # type: ignore[attr-defined]
        "INSERT INTO runs (id, started_at, pipeline, dry_run, status)"
        " VALUES (1, ?, 'eventnews', 0, 'done')",
        (datetime.now(UTC).isoformat(),),
    )


def _seed_article(conn: object, aid: str, *, created_at: str, published: str) -> None:
    conn.execute(  # type: ignore[attr-defined]
        "INSERT INTO articles (run_id, article_id, url, title, summary, body, importance,"
        " category, status, feed_title, feed_url, published_at, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            1,
            aid,
            f"https://kuebiko.example/{aid}",
            f"title-{aid}",
            "summary",
            "body",
            "high",
            "vuln",
            "posted",
            "F",
            "https://kuebiko.example/feed",
            published,
            created_at,
        ),
    )


def test_existing_members_keep_entities_regardless_of_write_time(tmp_path: object) -> None:
    """復元した既存メンバーが結合信号 entity を持つこと。

    ``article_entities.created_at`` は **行を書いた時刻** であって事象時刻ではない
    (バックフィル・再抽出は過去記事へ当日の日付を書く)。ここを時刻で絞ると
    合流が構造的に不可能になる。
    """
    # Arrange — 3 日前に取り込まれ、entity も 3 日前に書かれた記事
    from src.storage.run_history import RunHistoryRepository
    from src.ui.services.eventnews_hourly_job import _entity_counts, _load_members

    repo = RunHistoryRepository(db_path=tmp_path / "hourly.db")  # type: ignore[operator]
    old = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    with repo._connect() as conn:  # noqa: SLF001
        _seed_run(conn)
        _seed_article(conn, "a-old", created_at=old, published=old)
        conn.execute(
            "INSERT INTO article_entities (article_id, entity_type, value, created_at)"
            " VALUES (?,?,?,?)",
            ("a-old", "cve", "CVE-2026-0001", old),
        )

    # Act
    counts = _entity_counts(repo, datetime.now(UTC) - timedelta(hours=72))
    members = _load_members(repo, ["a-old"], counts)

    # Assert — entity が空なら合流条件を満たしようがない
    assert "a-old" in members
    assert members["a-old"].entities == frozenset({("cve", "cve-2026-0001")})


def test_frequency_cap_denominator_covers_the_window(tmp_path: object) -> None:
    """頻出ガードの分母は窓内コーパス全体で数える。

    手元の数十件だけで数えると cap (12 記事) に届かず、頻出語が結合信号として
    通ってしまい、無関係な記事同士が繋がる。
    """
    # Arrange — 同じ CVE を 15 記事が持つ (cap 超え)
    from src.eventnews.models import ENTITY_FREQ_CAP
    from src.storage.run_history import RunHistoryRepository
    from src.ui.services.eventnews_hourly_job import _entity_counts, _join_entities_for

    repo = RunHistoryRepository(db_path=tmp_path / "cap.db")  # type: ignore[operator]
    now = datetime.now(UTC).isoformat()
    total = ENTITY_FREQ_CAP + 3
    with repo._connect() as conn:  # noqa: SLF001
        _seed_run(conn)
        for i in range(total):
            _seed_article(conn, f"a-{i}", created_at=now, published=now)
            conn.execute(
                "INSERT INTO article_entities (article_id, entity_type, value, created_at)"
                " VALUES (?,?,?,?)",
                (f"a-{i}", "cve", "CVE-2026-9999", now),
            )

    # Act — 2 記事だけを対象に引いても、分母は窓全体で数える
    counts = _entity_counts(repo, datetime.now(UTC) - timedelta(hours=72))
    ents = _join_entities_for(repo, ["a-0", "a-1"], counts)

    # Assert
    assert counts[("cve", "cve-2026-9999")] == total
    assert ents == {}


def test_frequency_window_is_independent_of_the_join_window() -> None:
    """頻出ガードの分母は参加窓と独立であること。

    二つを束ねると「参加窓を広げる → 分母も広がる → cap に掛かる値が増える →
    広げた効果が相殺される」という無関係な結合が生まれる。実測では分母 336h と
    720h で multi アイテムは 167 vs 165 とほぼ不変であり、束ねる理由が無い。
    """
    from src.eventnews import models

    assert models.ENTITY_FREQ_WINDOW_HOURS == models.DORMANT_AFTER_DAYS * 24
    assert models.ENTITY_FREQ_WINDOW_HOURS != models.WINDOW_HOURS


# --- 埋込の取りこぼし補完 --------------------------------------------------
#
# 2026-08-24: 埋込は「意味的 dedup の判定時に生成し、投稿確定の経路で保存」する
# 設計だったため、その経路を通らない記事は埋込を永久に持たなかった。Grok は
# 1 ツイート = sub-article に展開されるが既読化は親レポート URL で行われるため、
# **articles に 906 件ある x.com の URL が dedup_seen_urls に 1 件も無い**。
# 既存 backfill script は dedup_seen_urls を起点にするので構造的に拾えない。


def test_missing_embedding_query_starts_from_articles(tmp_path: object) -> None:
    """既読化されていない記事も埋込対象として拾えること。"""
    # Arrange — dedup_seen_urls に無い記事 (Grok のツイートと同じ状況)
    from src.storage.run_history import RunHistoryRepository

    repo = RunHistoryRepository(db_path=tmp_path / "emb.db")  # type: ignore[operator]
    now = datetime.now(UTC).isoformat()
    with repo._connect() as conn:  # noqa: SLF001
        _seed_run(conn)
        _seed_article(conn, "a-tweet", created_at=now, published=now)

    # Act
    got = repo.list_articles_missing_embedding(model="m", limit=10)

    # Assert
    assert [r[0] for r in got] == ["a-tweet"]
    # 既存の dedup_seen_urls 起点のクエリでは拾えない (これが構造的な盲点だった)
    assert repo.list_urls_missing_embedding(model="m", limit=10) == []


def test_missing_embedding_query_skips_already_embedded(tmp_path: object) -> None:
    """同一 model の埋込が既にある記事は対象外。"""
    from src.storage.run_history import RunHistoryRepository
    from src.tools.url_normalizer import url_hash

    repo = RunHistoryRepository(db_path=tmp_path / "emb2.db")  # type: ignore[operator]
    now = datetime.now(UTC).isoformat()
    with repo._connect() as conn:  # noqa: SLF001
        _seed_run(conn)
        _seed_article(conn, "a-1", created_at=now, published=now)
    url = "https://kuebiko.example/a-1"
    h = url_hash(url)
    repo.mark_url_seen(url_hash=h, url=url, article_id="a-1", title="t")
    repo.add_article_embedding(url_hash=h, url=url, vector=[0.1, 0.2], model="m", title="t")

    assert repo.list_articles_missing_embedding(model="m", limit=10) == []
    # 別 model なら未生成として拾う
    assert [r[0] for r in repo.list_articles_missing_embedding(model="other", limit=10)] == ["a-1"]


def test_backfill_does_not_loop_on_hash_collisions(tmp_path: object) -> None:
    """正規化後 hash が衝突する記事を毎回埋め直さないこと。

    2026-08-24 に実際に無限ループした: ``url_hash`` は fragment (#...) を落とすため、
    ransomware.live の被害者レコード (``/about#<id>`` で 1 件ずつ区別) が同一 hash に
    潰れる。埋込は url_hash が主キーで 1 件しか持てず、URL 一致では永久に見つからない。
    """
    # Arrange — fragment だけが違う 2 記事 (正規化後は同一 hash)
    from src.storage.run_history import RunHistoryRepository
    from src.tools.url_normalizer import url_hash

    repo = RunHistoryRepository(db_path=tmp_path / "alias.db")  # type: ignore[operator]
    now = datetime.now(UTC).isoformat()
    urls = ["https://kuebiko.example/about#aaa", "https://kuebiko.example/about#bbb"]
    with repo._connect() as conn:  # noqa: SLF001
        _seed_run(conn)
        for i, u in enumerate(urls):
            conn.execute(
                "INSERT INTO articles (run_id, article_id, url, title, summary, body, importance,"
                " category, status, feed_title, feed_url, published_at, created_at)"
                " VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"a-{i}",
                    u,
                    f"t{i}",
                    "s",
                    "b",
                    "high",
                    "vuln",
                    "posted",
                    "F",
                    "https://kuebiko.example/feed",
                    now,
                    now,
                ),
            )
    h = url_hash(urls[0])
    assert h == url_hash(urls[1])
    repo.mark_url_seen(url_hash=h, url=urls[0], article_id="a-0", title="t0")
    repo.add_article_embedding(url_hash=h, url=urls[0], vector=[0.1], model="m", title="t0")

    # Act — 2 件目は URL 一致では未生成に見えるが、hash は既に埋まっている
    rows = repo.list_articles_missing_embedding(model="m", limit=10)
    taken = repo.existing_embedding_url_hashes([url_hash(u) for _a, u, _t, _w in rows], model="m")

    # Assert
    assert [r[0] for r in rows] == ["a-1"]
    assert url_hash(urls[1]) in taken


def test_existing_members_are_excluded_from_candidates(tmp_path: object) -> None:
    """既にメンバーの記事を候補に戻さないこと (決定論 item_id の衝突防止)。

    2026-08-24: 遡及構築で候補窓 (14 日) が復元窓 (dormant 期限) を超え、復元されない
    アイテムのメンバーが再候補化して `event_items_pkey` の UniqueViolation で落ちた。
    復元済みメンバーの除外だけでは足りず、DB 側の関門が要る。
    """
    from src.storage.run_history import RunHistoryRepository

    repo = RunHistoryRepository(db_path=tmp_path / "member.db")  # type: ignore[operator]
    base = datetime(2026, 8, 1, tzinfo=UTC)
    repo.create_event_item(
        item_id="ev-x",
        origin="live",
        first_reported_at=base,
        last_reported_at=base,
        importance="high",
    )
    repo.add_event_member(
        item_id="ev-x",
        article_id="a-1",
        joined_at=base,
        contributed_new_facts=1,
        join_signal="seed",
    )

    got = repo.existing_member_article_ids(["a-1", "a-2"])

    assert got == {"a-1"}
    assert repo.existing_member_article_ids([]) == set()


def test_grok_subarticle_carries_author_identity() -> None:
    """Grok の per-tweet 記事が **投稿者** を媒体として持つこと。

    2026-08-24: sub-article が親 Article を指していたため、既読化・埋込が親レポートの
    URL で行われ、ツイート URL は dedup_seen_urls に 1 件も入らなかった
    (実測 x.com: articles 906 / dedup 0)。feed_title も全件 'Grok' で、**何人が
    報じても独立 1 媒体** として数えられていた (実測 102 アカウント)。
    """
    from src.pipeline.grok_convert import grok_subarticle_source
    from src.tools.article_model import Article

    parent = Article(
        id="grok-1",
        title="Grok レポート",
        url="https://grok.com/chat/abc",
        summary_html="",
        published=datetime(2026, 8, 24, tzinfo=UTC),
        feed_title="Grok",
        feed_url="https://grok.com/",
    )

    got = grok_subarticle_source(parent, "https://x.com/FalconFeedsio/status/2091643392816214268")

    assert got.url == "https://x.com/FalconFeedsio/status/2091643392816214268"
    assert got.feed_url == "https://x.com/FalconFeedsio"
    assert got.feed_title == "@FalconFeedsio"
    # permalink でなければ親のまま (壊れた URL で source identity を捏造しない)
    assert grok_subarticle_source(parent, "https://grok.com/chat/abc") is parent
    assert grok_subarticle_source(parent, "") is parent
