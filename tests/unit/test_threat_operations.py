"""Phase Threats: threat_operations service の unit test。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.storage.run_history import (
    ArticleRecord,
    RunHistoryRepository,
    RunRecord,
)
from src.ui.services.threat_operations import (
    _build_actor_lookup,
    _extract_actors_from_text,
    _make_sparkline,
    fetch_actor_detail,
    fetch_threat_operations_snapshot,
)


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "th.db")


def _now() -> datetime:
    return datetime.now(UTC)


def _add(
    repo: RunHistoryRepository,
    *,
    article_id: str,
    title: str,
    feed_title: str = "test",
    hours_ago: float = 1.0,
    summary: str = "",
    importance: str = "medium",
    posted_channel: str = "brief",
    victim_country_iso: str | None = None,
    victim_sector_canonical: str | None = None,
    subject_actor_ids: str | None = None,
    subject_actor_source: str | None = None,
) -> None:
    run_id = repo.start_run(RunRecord(started_at=_now(), pipeline="x", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=run_id,
            article_id=article_id,
            title=title,
            url=f"https://example.com/{article_id}",
            feed_title=feed_title,
            summary=summary,
            importance=importance,
            posted_channel=posted_channel,
            status="posted",
            created_at=_now() - timedelta(hours=hours_ago),
            victim_country_iso=victim_country_iso,
            victim_sector_canonical=victim_sector_canonical,
            subject_actor_ids=subject_actor_ids,
            subject_actor_source=subject_actor_source,
        ),
    )


class TestSparkline:
    def test_empty_returns_empty_or_flat(self) -> None:
        # SVG sparkline: empty tuple → empty string
        assert _make_sparkline(()) == ""

    def test_all_zero_renders_flat_line(self) -> None:
        out = _make_sparkline((0, 0, 0, 0))
        # SVG output with a horizontal line indicating "no activity"
        assert "<svg" in out
        assert "line" in out  # flat line tag

    def test_monotonic_increase_renders_svg_path(self) -> None:
        out = _make_sparkline((1, 2, 4, 8))
        assert "<svg" in out
        assert "<path" in out
        # 4 points → at least 3 L commands
        assert out.count("L") >= 3


class TestActorLookup:
    def test_lookup_includes_canonical_and_aliases(self) -> None:
        from src.cti.actor_normalizer import load_actor_aliases

        reg = load_actor_aliases()
        lookup = _build_actor_lookup(reg.actors)
        # APT41 が aliases 含めて検出される
        text = "wicked panda の活動が観測された"
        actors = _extract_actors_from_text(text, lookup)
        assert "apt41" in actors

    def test_case_insensitive(self) -> None:
        from src.cti.actor_normalizer import load_actor_aliases

        reg = load_actor_aliases()
        lookup = _build_actor_lookup(reg.actors)
        actors = _extract_actors_from_text("SALT TYPHOON observed", lookup)
        assert "salt_typhoon" in actors

    def test_no_substring_false_positive_inside_words(self) -> None:
        """word-boundary 化の回帰テスト: 単語内の部分一致で誤帰属しない。

        実害例 (2026-06-11 監査): "Northrop Grumman"→Russia GRU 31 件、
        "MSSP"→PRC MSS、"APT38" テキスト→APT3。
        """
        from src.cti.actor_normalizer import ActorAlias

        actors = (
            ActorAlias(id="russia_gru", canonical="Russia GRU", aliases=("GRU",)),
            ActorAlias(id="prc_mss", canonical="PRC MSS", aliases=("MSS",)),
            ActorAlias(id="apt3", canonical="APT3"),
        )
        lookup = _build_actor_lookup(actors)
        assert _extract_actors_from_text("Northrop Grumman wins Navy contract", lookup) == set()
        assert _extract_actors_from_text("GRUB bootloader vulnerability", lookup) == set()
        assert _extract_actors_from_text("MSSP providers report attacks", lookup) == set()
        assert _extract_actors_from_text("APT38 が金融機関を攻撃", lookup) == set()
        # 単語として独立した言及は正しく拾う (日本語境界含む)
        assert _extract_actors_from_text("GRU 系ハッカーの作戦", lookup) == {"russia_gru"}
        assert _extract_actors_from_text("中国 MSS の諜報活動", lookup) == {"prc_mss"}

    def test_longest_alternation_wins(self) -> None:
        """alternation は長い名前優先: APT38 用 entry があれば APT3 に消費されない。"""
        from src.cti.actor_normalizer import ActorAlias

        actors = (
            ActorAlias(id="apt3", canonical="APT3"),
            ActorAlias(id="apt38", canonical="APT38"),
        )
        lookup = _build_actor_lookup(actors)
        assert _extract_actors_from_text("APT38 campaign", lookup) == {"apt38"}

    def test_ambiguous_actor_requires_context_cue(self) -> None:
        """一般語と衝突する曖昧アクター (Tick 等) は文脈 cue の共起で初めてマッチする。

        actor_normalizer の ambiguous gate と同じ規則。2026-07-17 以前は本ページの
        抽出だけ gate を通っておらず、一般語アクターが過剰計上され得た。
        """
        from src.cti.actor_normalizer import ActorAlias

        actors = (
            ActorAlias(
                id="tick",
                canonical="Tick",
                aliases=("Bronze Butler",),
                ambiguous=True,
                context_cues=("bronze butler", "espionage", "諜報"),
            ),
        )
        lookup = _build_actor_lookup(actors)
        # 一般語としての tick (cue なし) → マッチしない
        assert _extract_actors_from_text("Remember to tick the checkbox", lookup) == set()
        assert _extract_actors_from_text("Tick-borne disease研究の報告", lookup) == set()
        # cue 共起 → マッチ
        assert _extract_actors_from_text("Tick espionage campaign in Japan", lookup) == {"tick"}
        # 別名自体が cue に含まれるので別名言及は自己充足
        assert _extract_actors_from_text("Bronze Butler の新たな活動", lookup) == {"tick"}

    def test_org_double_count_suppressed_when_group_identified(self) -> None:
        """グループ特定時は親 organization への二重計上を抑止 (Actors Stage 5)。"""
        from src.cti.actor_normalizer import ActorAlias

        actors = (
            ActorAlias(
                id="russia_gru", canonical="Russia GRU", aliases=("GRU",), kind="organization"
            ),
            ActorAlias(id="apt28", canonical="APT28", kind="group", sponsor_org="russia_gru"),
        )
        lookup = _build_actor_lookup(actors)
        # グループ + 機関が併記 → グループのみ (機関は二重計上しない)
        assert _extract_actors_from_text("APT28 (GRU 配下) の新作戦", lookup) == {"apt28"}
        # 機関のみの言及 → 機関が残る (グループ未特定の受け皿)
        assert _extract_actors_from_text("GRU 系ハッカーが攻撃", lookup) == {"russia_gru"}


class TestFetchSnapshot:
    def test_empty_db_returns_empty_actors(
        self,
        repo: RunHistoryRepository,
        tmp_path: Path,
    ) -> None:
        snap = fetch_threat_operations_snapshot(
            lookback_days=30,
            db_path=tmp_path / "th.db",
        )
        assert snap.actors == ()
        assert snap.discovery.unknown_bucket_count == 0
        # families は yaml から
        assert len(snap.families) > 0

    def test_detects_known_actor(
        self,
        repo: RunHistoryRepository,
        tmp_path: Path,
    ) -> None:
        _add(
            repo,
            article_id="a1",
            title="Salt Typhoon が新たな侵入",
            summary="米通信網標的",
            hours_ago=2.0,
        )
        snap = fetch_threat_operations_snapshot(
            lookback_days=30,
            db_path=tmp_path / "th.db",
        )
        assert len(snap.actors) >= 1
        salt = next((a for a in snap.actors if a.actor_id == "salt_typhoon"), None)
        assert salt is not None
        assert salt.total_articles == 1
        assert salt.family == "typhoon"
        assert salt.nation == "cn"
        assert salt.is_new is True  # baseline 期間に未観測

    def test_subject_gate_excludes_mention_only_actor(
        self,
        repo: RunHistoryRepository,
        tmp_path: Path,
    ) -> None:
        # 評価済み記事: Salt Typhoon と Lazarus を言及するが主題は salt_typhoon のみ。
        # subject-gate で言及のみの lazarus は活動計上から除外される (2026-07-29)。
        _add(
            repo,
            article_id="a1",
            title="Salt Typhoon の侵入、Lazarus Group とは別系統",
            summary="米通信網を標的",
            subject_actor_ids="salt_typhoon",
            subject_actor_source="llm",
        )
        snap = fetch_threat_operations_snapshot(
            lookback_days=30,
            db_path=tmp_path / "th.db",
        )
        ids = {a.actor_id for a in snap.actors}
        assert "salt_typhoon" in ids
        assert "lazarus" not in ids

    def test_subject_gate_legacy_row_counts_mentions(
        self,
        repo: RunHistoryRepository,
        tmp_path: Path,
    ) -> None:
        # legacy 記事 (subject_actor_source=None): 主題層稼働前なので mention を計上 (fallback)
        _add(
            repo,
            article_id="a1",
            title="Salt Typhoon と Lazarus Group が同時に観測された",
            subject_actor_ids=None,
            subject_actor_source=None,
        )
        snap = fetch_threat_operations_snapshot(
            lookback_days=30,
            db_path=tmp_path / "th.db",
        )
        ids = {a.actor_id for a in snap.actors}
        assert "salt_typhoon" in ids
        assert "lazarus" in ids

    def test_filter_by_family(
        self,
        repo: RunHistoryRepository,
        tmp_path: Path,
    ) -> None:
        _add(repo, article_id="a1", title="Salt Typhoon")
        _add(repo, article_id="a2", title="Lazarus group attack")
        snap = fetch_threat_operations_snapshot(
            lookback_days=30,
            family_filter="typhoon",
            db_path=tmp_path / "th.db",
        )
        ids = [a.actor_id for a in snap.actors]
        assert "salt_typhoon" in ids
        assert "lazarus" not in ids

    def test_filter_by_nation(
        self,
        repo: RunHistoryRepository,
        tmp_path: Path,
    ) -> None:
        _add(repo, article_id="a1", title="Salt Typhoon")
        _add(repo, article_id="a2", title="APT28 phishing")
        snap = fetch_threat_operations_snapshot(
            lookback_days=30,
            nation_filter="cn",
            db_path=tmp_path / "th.db",
        )
        ids = [a.actor_id for a in snap.actors]
        assert "salt_typhoon" in ids
        assert "apt28" not in ids

    def test_unknown_bucket_count(
        self,
        repo: RunHistoryRepository,
        tmp_path: Path,
    ) -> None:
        # 既知 actor を含まないタイトル
        _add(repo, article_id="x1", title="ランサムウェア新グループが発覚")
        _add(repo, article_id="x2", title="重要インフラへの不審な通信")
        snap = fetch_threat_operations_snapshot(
            lookback_days=30,
            db_path=tmp_path / "th.db",
        )
        assert snap.discovery.unknown_bucket_count == 2


class TestFetchActorDetail:
    def test_unknown_actor_returns_none(
        self,
        repo: RunHistoryRepository,
        tmp_path: Path,
    ) -> None:
        # repo fixture で DB init 済 (空 articles テーブル)
        d = fetch_actor_detail(
            "does-not-exist-actor",
            lookback_days=30,
            db_path=tmp_path / "th.db",
        )
        assert d is None

    def test_known_actor_no_articles_returns_zero_detail(
        self,
        repo: RunHistoryRepository,
        tmp_path: Path,
    ) -> None:
        # 辞書に居る actor は観測 0 (休眠) でも詳細を返す — ミッション脅威評価で
        # 「Critical・休眠」アクターを点検可能にするため (暗域=不明≠安全)。
        # 旧仕様 (None) は 2026-07-17 に意図的に変更
        d = fetch_actor_detail(
            "salt_typhoon",
            lookback_days=30,
            db_path=tmp_path / "th.db",
        )
        assert d is not None
        assert d.activity.total_articles == 0
        assert d.recent_articles == ()

    def test_known_actor_with_articles_returns_detail(
        self,
        repo: RunHistoryRepository,
        tmp_path: Path,
    ) -> None:
        _add(
            repo,
            article_id="s1",
            title="Salt Typhoon hits new target",
            hours_ago=2.0,
        )
        _add(
            repo,
            article_id="s2",
            title="More Salt Typhoon activity observed",
            hours_ago=4.0,
        )
        d = fetch_actor_detail(
            "salt_typhoon",
            lookback_days=30,
            db_path=tmp_path / "th.db",
        )
        assert d is not None
        assert d.activity.total_articles == 2
        assert len(d.recent_articles) == 2
        # timeline_daily は lookback+1 個の (date, count) tuple
        assert len(d.timeline_daily) >= 1


class TestVictimCountrySectors:
    """ミッション脅威評価用の被害観測スライス (切り詰めない country×sector 集計)。"""

    def test_aggregates_country_sector_pairs(
        self,
        repo: RunHistoryRepository,
        tmp_path: Path,
    ) -> None:
        _add(
            repo,
            article_id="v1",
            title="Salt Typhoon breaches agency",
            victim_country_iso="JP",
            victim_sector_canonical="government",
        )
        _add(
            repo,
            article_id="v2",
            title="Salt Typhoon hits telecom",
            victim_country_iso="us",  # 小文字でも大文字に正規化される
            victim_sector_canonical="telecom",
        )
        _add(
            repo,
            article_id="v3",
            title="Salt Typhoon detected again",
            victim_country_iso="US",
            victim_sector_canonical="telecom",
        )
        snap = fetch_threat_operations_snapshot(lookback_days=30, db_path=tmp_path / "th.db")
        a = next(x for x in snap.actors if x.actor_id == "salt_typhoon")
        assert ("US", "telecom", 2) in a.victim_country_sectors
        assert ("JP", "government", 1) in a.victim_country_sectors


class TestIncludeDormant:
    def test_dormant_group_actors_included_with_zero_counts(
        self,
        repo: RunHistoryRepository,
        tmp_path: Path,
    ) -> None:
        _add(repo, article_id="d1", title="Salt Typhoon activity")
        snap = fetch_threat_operations_snapshot(
            lookback_days=30, include_dormant=True, db_path=tmp_path / "th.db"
        )
        by_id = {a.actor_id: a for a in snap.actors}
        # 観測ありの actor は従来どおり
        assert by_id["salt_typhoon"].total_articles == 1
        # 観測 0 の辞書 group actor も含まれる (例: volt_typhoon)
        assert "volt_typhoon" in by_id
        assert by_id["volt_typhoon"].total_articles == 0
        # organization は含めない (脅威評価の対象外)
        dormant_orgs = [
            a for a in snap.actors if a.kind == "organization" and a.total_articles == 0
        ]
        assert dormant_orgs == []

    def test_default_excludes_dormant(
        self,
        repo: RunHistoryRepository,
        tmp_path: Path,
    ) -> None:
        _add(repo, article_id="d1", title="Salt Typhoon activity")
        snap = fetch_threat_operations_snapshot(lookback_days=30, db_path=tmp_path / "th.db")
        assert all(a.total_articles > 0 for a in snap.actors)


class TestPirActorIndex:
    """③ PIR→辞書ID 解決 + Threats page の PIR 連携。"""

    def test_resolves_pir_actors_to_actor_ids(self) -> None:
        from src.cti.actor_normalizer import ActorAlias
        from src.pir.models import Pir, StrongSignals
        from src.ui.services.threat_operations import (
            _build_actor_lookup,
            _build_pir_actor_index,
        )

        actors = (
            ActorAlias(id="lazarus", canonical="Lazarus", aliases=("APT38",)),
            ActorAlias(id="volt", canonical="Volt Typhoon"),
        )
        lookup = _build_actor_lookup(actors)
        pirs = [
            Pir(
                id="pir_dprk",
                title="DPRK",
                strong_signals=StrongSignals(actors=["Lazarus"]),
            ),
            Pir(
                id="pir_cn",
                title="CN",
                strong_signals=StrongSignals(actors=["Volt Typhoon", "辞書外アクター"]),
            ),
        ]
        index = _build_pir_actor_index(lookup, pirs)
        assert index["lazarus"] == frozenset({"pir_dprk"})
        assert index["volt"] == frozenset({"pir_cn"})

    def test_alias_resolves_to_same_id(self) -> None:
        from src.cti.actor_normalizer import ActorAlias
        from src.pir.models import Pir, StrongSignals
        from src.ui.services.threat_operations import (
            _build_actor_lookup,
            _build_pir_actor_index,
        )

        actors = (ActorAlias(id="lazarus", canonical="Lazarus", aliases=("APT38",)),)
        lookup = _build_actor_lookup(actors)
        # alias 名 (APT38) で PIR が actor を指しても id に解決される
        pirs = [Pir(id="pir_x", title="X", strong_signals=StrongSignals(actors=["APT38"]))]
        index = _build_pir_actor_index(lookup, pirs)
        assert index["lazarus"] == frozenset({"pir_x"})

    def test_unknown_actor_name_ignored(self) -> None:
        from src.cti.actor_normalizer import ActorAlias
        from src.pir.models import Pir, StrongSignals
        from src.ui.services.threat_operations import (
            _build_actor_lookup,
            _build_pir_actor_index,
        )

        lookup = _build_actor_lookup((ActorAlias(id="lazarus", canonical="Lazarus"),))
        pirs = [Pir(id="pir_x", title="X", strong_signals=StrongSignals(actors=["NonExistent"]))]
        assert _build_pir_actor_index(lookup, pirs) == {}

    def test_snapshot_annotates_and_filters_by_pir(
        self, repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.ui.services.threat_operations as to_mod
        from src.pir.models import Pir, StrongSignals

        _add(repo, article_id="a1", title="Lazarus group targets a bank", hours_ago=1.0)
        pir = Pir(id="pir_dprk", title="DPRK", strong_signals=StrongSignals(actors=["Lazarus"]))
        monkeypatch.setattr(to_mod, "_load_enabled_pir_priorities", lambda: [pir])

        snap = fetch_threat_operations_snapshot(db_path=repo._db_path, lookback_days=30)
        laz = next((a for a in snap.actors if a.actor_id == "lazarus"), None)
        assert laz is not None
        assert "pir_dprk" in laz.matched_pir_ids

        # pir_filter = 該当 PIR → 残る
        kept = fetch_threat_operations_snapshot(
            db_path=repo._db_path, lookback_days=30, pir_filter="pir_dprk"
        )
        assert any(a.actor_id == "lazarus" for a in kept.actors)
        # pir_filter = 非該当 PIR → 除外
        dropped = fetch_threat_operations_snapshot(
            db_path=repo._db_path, lookback_days=30, pir_filter="pir_other"
        )
        assert not any(a.actor_id == "lazarus" for a in dropped.actors)
        # __any__ → PIR 該当 actor は残る
        anyf = fetch_threat_operations_snapshot(
            db_path=repo._db_path, lookback_days=30, pir_filter="__any__"
        )
        assert any(a.actor_id == "lazarus" for a in anyf.actors)


class TestSnapshotCoreCache:
    """production 相当 (既定 DB・now 未指定) での TTL cache の挙動。"""

    def test_cache_hit_until_invalidated(
        self, repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.ui.services.threat_operations as to_mod

        # 既定 DB path をこの test の tmp DB に向けて cache 経路を有効化する
        monkeypatch.setattr(to_mod, "DEFAULT_DB_PATH", repo._db_path)
        to_mod.invalidate_threat_snapshot_cache()
        try:
            _add(repo, article_id="c1", title="Volt Typhoon activity observed")
            s1 = fetch_threat_operations_snapshot(lookback_days=30, db_path=repo._db_path)
            vt1 = next(a for a in s1.actors if a.actor_id == "volt_typhoon")
            assert vt1.total_articles == 1

            # cache 有効中は新規記事が反映されない (TTL 内は据え置きが仕様)
            _add(repo, article_id="c2", title="Volt Typhoon strikes again")
            s2 = fetch_threat_operations_snapshot(lookback_days=30, db_path=repo._db_path)
            vt2 = next(a for a in s2.actors if a.actor_id == "volt_typhoon")
            assert vt2.total_articles == 1

            # filter / 検索は cache 済み activities への post-filter で効く
            hit = fetch_threat_operations_snapshot(
                lookback_days=30, db_path=repo._db_path, search_query="BRONZE SILHOUETTE"
            )
            assert any(a.actor_id == "volt_typhoon" for a in hit.actors)

            # 明示 invalidate で即時反映
            to_mod.invalidate_threat_snapshot_cache()
            s3 = fetch_threat_operations_snapshot(lookback_days=30, db_path=repo._db_path)
            vt3 = next(a for a in s3.actors if a.actor_id == "volt_typhoon")
            assert vt3.total_articles == 2
        finally:
            to_mod.invalidate_threat_snapshot_cache()

    def test_now_specified_bypasses_cache(
        self, repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.ui.services.threat_operations as to_mod

        monkeypatch.setattr(to_mod, "DEFAULT_DB_PATH", repo._db_path)
        to_mod.invalidate_threat_snapshot_cache()
        try:
            _add(repo, article_id="n1", title="Volt Typhoon activity observed")
            s1 = fetch_threat_operations_snapshot(
                lookback_days=30, db_path=repo._db_path, now=_now()
            )
            assert any(a.actor_id == "volt_typhoon" for a in s1.actors)
            _add(repo, article_id="n2", title="Volt Typhoon strikes again")
            s2 = fetch_threat_operations_snapshot(
                lookback_days=30, db_path=repo._db_path, now=_now()
            )
            vt = next(a for a in s2.actors if a.actor_id == "volt_typhoon")
            assert vt.total_articles == 2
        finally:
            to_mod.invalidate_threat_snapshot_cache()
