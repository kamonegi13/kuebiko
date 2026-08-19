"""Phase B-content-dedup: cross-source content dedup のテスト。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.cti.content_dedup import (
    extract_advisory_ids,
    extract_title_signature,
    find_recent_content_duplicate,
)
from src.storage.run_history import (
    ArticleRecord,
    RunHistoryRepository,
    RunRecord,
)


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "dedup.db")


def _now() -> datetime:
    return datetime.now(UTC)


def _seed(
    repo: RunHistoryRepository,
    *,
    article_id: str,
    title: str,
    feed_title: str = "test-feed",
    hours_ago: float = 1.0,
    url: str | None = None,
) -> None:
    run_id = repo.start_run(
        RunRecord(started_at=_now(), pipeline="x", dry_run=False),
    )
    repo.add_article(
        ArticleRecord(
            run_id=run_id,
            article_id=article_id,
            title=title,
            url=url or f"https://example.com/{article_id}",
            feed_title=feed_title,
            status="posted",
            created_at=_now() - timedelta(hours=hours_ago),
        ),
    )


class TestExtractTitleSignature:
    def test_extracts_english_tokens(self) -> None:
        sig = extract_title_signature("Drupal Core critical vulnerability patched")
        # stop words 除外: critical, vulnerability
        assert "drupal" in sig
        assert "core" in sig
        assert "patched" in sig
        assert "vulnerability" not in sig
        assert "critical" not in sig

    def test_extracts_japanese_tokens(self) -> None:
        sig = extract_title_signature("トレンドマイクロ製品における複数の脆弱性")
        assert "トレンドマイクロ" in sig
        # stop: 製品, 脆弱性, における
        assert "製品" not in sig
        assert "脆弱性" not in sig

    def test_extracts_cve_id(self) -> None:
        sig = extract_title_signature("Apache HTTP/2 CVE-2026-23918 で DoS が可能")
        assert "cve-2026-23918" in sig

    def test_empty_title_returns_empty(self) -> None:
        assert extract_title_signature("") == frozenset()

    def test_strips_date_patterns(self) -> None:
        sig = extract_title_signature("脆弱性情報 (2026年5月)")
        # 日付トークンが残らない
        assert all(not t.isdigit() for t in sig)


class TestFindRecentContentDuplicate:
    def test_detects_trend_micro_advisory_3_sources(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        """同 Trend Micro advisory が 3 source から来ても 2 件目以降は重複と判定。"""
        # 1 件目: JPCERT 統合 RSS が先に post
        _seed(
            repo,
            article_id="jpcert-1",
            title=(
                "JVN: トレンドマイクロ製企業向けエンドポイントセキュリティ製品における複数の脆弱性"
            ),
            feed_title="JPCERT/CC 統合 RSS",
            hours_ago=2.0,
        )
        # 2 件目候補: JVN English が後に来た
        dup = find_recent_content_duplicate(
            repo=repo,
            title="Trend Micro の企業向けエンドポイントセキュリティ製品における複数の脆弱性",
            candidate_article_id="jvn-2",
        )
        assert dup is not None
        assert dup.article_id == "jpcert-1"

    def test_drupal_same_day_brief_border_case_not_deduped(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        """Drupal Core 同日 2 source 報告は title-only 信号では border case。

        Jaccard 0.15 程度で閾値未満。CVE-ID が title に無いと dedup されない。
        (title が極端に短く、共通技術用語が少ないため)。
        embedding ベース (将来 Phase) で対処予定。
        """
        _seed(
            repo,
            article_id="hacker-news-1",
            title="Drupal Core に重大な脆弱性、PostgreSQL 利用サイトが RCE のリスク",
            feed_title="The Hacker News",
            hours_ago=3.0,
        )
        dup = find_recent_content_duplicate(
            repo=repo,
            title="Drupal Core に重大な脆弱性、サイバー攻撃のリスク",
            candidate_article_id="gbhackers-2",
        )
        # title 単独では Jaccard が閾値未満 → 検出されない (現状の限界)
        assert dup is None

    def test_shared_cve_forces_dedup(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        """CVE-ID が両方の title に出れば 強制 dedup (Jaccard 関係なし)。"""
        _seed(
            repo,
            article_id="hn-1",
            title="Drupal Core CVE-2026-9876 で RCE が判明",
            feed_title="The Hacker News",
            hours_ago=2.0,
        )
        dup = find_recent_content_duplicate(
            repo=repo,
            title="まったく違う文脈の記事だが CVE-2026-9876 を扱う",
            candidate_article_id="gb-2",
        )
        assert dup is not None
        assert dup.article_id == "hn-1"

    def test_shai_hulud_progression_not_deduped(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        """24h 超え (25h) の Shai-Hulud 続報は dedup されない (続報として通る)。"""
        _seed(
            repo,
            article_id="prev",
            title="流出した Shai-Hulud マルウェアを用いた新たな npm インフォスティーラー",
            feed_title="BleepingComputer",
            hours_ago=30.0,  # 24h 超え
        )
        dup = find_recent_content_duplicate(
            repo=repo,
            title="Mini Shai-Hulud、@antv npm パッケージを侵害し CI/CD 認証情報を標的",
            candidate_article_id="next",
        )
        # lookback_hours=24 のデフォルトでは 30h 前は対象外
        assert dup is None

    def test_distinct_topics_not_matched(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        """異なる incident (Twill Typhoon vs Volt Typhoon) は dedup されない。"""
        _seed(
            repo,
            article_id="prev",
            title="Volt Typhoon FDMTPバックドアを使用し ICS を標的",
            feed_title="Grok",
            hours_ago=2.0,
        )
        dup = find_recent_content_duplicate(
            repo=repo,
            title="Twill Typhoon APJ地域の金融機関を対象とした諜報キャンペーン",
            candidate_article_id="next",
        )
        # FDMTP, ICS vs APJ, 金融 — 共通 token 少なく Jaccard 低い
        assert dup is None

    def test_low_token_count_returns_none(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        """signature token が 3 未満なら判定 skip (誤判定回避)。"""
        _seed(repo, article_id="x", title="alert", hours_ago=1.0)
        dup = find_recent_content_duplicate(
            repo=repo,
            title="news",  # 1 token のみ
            candidate_article_id="y",
        )
        assert dup is None

    def test_excludes_self_by_article_id(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        """同一 article_id は除外 (再判定時の自己マッチ防止)。"""
        _seed(
            repo,
            article_id="myself",
            title="ロシアによるウクライナ生物研究所への攻撃を非難",
            hours_ago=1.0,
        )
        dup = find_recent_content_duplicate(
            repo=repo,
            title="ロシアによるウクライナ生物研究所への攻撃を非難",
            candidate_article_id="myself",
        )
        assert dup is None


class TestAdvisoryIdDedup:
    """JVN advisory id (URL 由来) の決定論 dedup (監査 2026-08-01)。

    実観測した重複の主形態:
    - JVN (English) と JPCERT/CC 統合 RSS が同一 advisory を日英別タイトルで同日投稿
      (Sharp vs シャープで Jaccard < 0.4、URL パス差 /en/vu/ vs /vu/ で URL dedup も外れる)
      — だが両 URL に同じ JVNVU id が決定論的に載る
    - JVN iPedia (JVNDB) が同一タイトルで翌日再掲 (24h 窓の外)
    """

    def test_extract_advisory_ids_from_urls(self) -> None:
        # JVN 日本語版と English 版はパスが違うが同じ JVNVU id
        a = extract_advisory_ids("https://jvn.jp/vu/JVNVU98759887/", "")
        b = extract_advisory_ids("https://jvn.jp/en/vu/JVNVU98759887/", "")
        assert a == b == frozenset({"jvnvu-98759887"})
        # JVNDB (iPedia)
        c = extract_advisory_ids("https://jvndb.jvn.jp/en/contents/2026/JVNDB-2026-000103.html", "")
        assert c == frozenset({"jvndb-2026-000103"})
        # タイトル中の JVNVU#12345678 表記も拾う
        d = extract_advisory_ids("", "JVNVU#98759887: 複合機における複数の脆弱性")
        assert "jvnvu-98759887" in d

    def test_shared_advisory_id_forces_dedup_despite_low_jaccard(
        self, repo: RunHistoryRepository
    ) -> None:
        """URL に同じ JVNVU id → タイトルが日英で別物でも強制 dedup。"""
        _seed(
            repo,
            article_id="jpcert-mfp",
            title="JVN: シャープ製および東芝テック製複合機(MFP)における複数の脆弱性",
            feed_title="JPCERT/CC 統合 RSS",
            hours_ago=3.0,
            url="https://jvn.jp/vu/JVNVU98759887/",
        )
        dup = find_recent_content_duplicate(
            repo=repo,
            title="Sharp および Toshiba Tec の複合機における複数の脆弱性",
            url="https://jvn.jp/en/vu/JVNVU98759887/",
            candidate_article_id="jvn-en-mfp",
        )
        assert dup is not None
        assert dup.article_id == "jpcert-mfp"

    def test_identical_title_republication_after_24h_deduped(
        self, repo: RunHistoryRepository
    ) -> None:
        """ほぼ同一タイトル (Jaccard >= 0.85) は 24h 超でも同一 advisory として dedup。

        JVN → 翌日 JVNDB (iPedia) 再掲の実事例 (ELECOM、07-28→07-29)。
        """
        title = "ELECOM 製無線 LAN ルーターおよびアクセスポイントにおける複数の脆弱性"
        _seed(
            repo,
            article_id="jvn-elecom",
            title=title,
            feed_title="JVN",
            hours_ago=30.0,
            url="https://jvn.jp/vu/JVNVU90671953/",
        )
        dup = find_recent_content_duplicate(
            repo=repo,
            title=title,
            url="https://jvndb.jvn.jp/en/contents/2026/JVNDB-2026-000103.html",
            candidate_article_id="jvndb-elecom",
        )
        assert dup is not None
        assert dup.article_id == "jvn-elecom"

    def test_moderate_similarity_after_24h_still_passes_as_followup(
        self, repo: RunHistoryRepository
    ) -> None:
        """24h 超の中程度類似 (0.4 帯) は従来どおり続報として通す (設計不変)。"""
        _seed(
            repo,
            article_id="first-report",
            title="Cisco IOS XE の脆弱性を突く大規模スキャンを観測、複数の組織に影響",
            feed_title="The Hacker News",
            hours_ago=30.0,
        )
        dup = find_recent_content_duplicate(
            repo=repo,
            title="Cisco IOS XE への攻撃が拡大、新たな標的組織を確認",
            candidate_article_id="followup",
        )
        assert dup is None


class TestAdvisoryIdMismatchRaisesTheBar:
    """別の advisory id を持つ記事は、語り口の一致では畳まない (2026-08-19)。

    advisory は「〜における複数の脆弱性」のような定型文が支配的で、製品が違っても
    Jaccard 0.4 を超える。実測で JVN iPedia の 79% (22/28)、JPCERT 注意喚起の
    67% (10/15) が重複落選していた。
    ⚠ ただし **id 不一致 = 別物とは断定しない** — JVN → 翌日 JVNDB 再掲は id が
    変わるのに同一の脆弱性。要求水準を「ほぼ同一タイトル」へ引き上げる形にする。
    """

    def test_different_ids_with_boilerplate_titles_are_kept(
        self, repo: RunHistoryRepository
    ) -> None:
        """定型文の重なりだけでは畳まない (別製品の別 advisory)。"""
        _seed(
            repo,
            article_id="jvn-hitachi",
            title="Hitachi 製品における複数の脆弱性",
            feed_title="JVN (English)",
            hours_ago=3.0,
            url="https://jvn.jp/en/jp/JVN91713656/",
        )

        dup = find_recent_content_duplicate(
            repo=repo,
            title="Siemens 製品における複数の脆弱性",
            url="https://jvndb.jvn.jp/en/contents/2026/JVNDB-2026-000123.html",
            candidate_article_id="jvndb-siemens",
        )

        assert dup is None, "別 advisory id なのに定型文の重なりで畳まれた"

    def test_same_id_across_languages_still_deduped(self, repo: RunHistoryRepository) -> None:
        """id 一致は従来どおり強制 dedup (日英ペア)。"""
        _seed(
            repo,
            article_id="jvn-en",
            title="Multiple vulnerabilities in ACME router",
            feed_title="JVN (English)",
            hours_ago=2.0,
            url="https://jvn.jp/en/vu/JVNVU98759887/",
        )

        dup = find_recent_content_duplicate(
            repo=repo,
            title="ACME ルータにおける複数の脆弱性",
            url="https://jvn.jp/vu/JVNVU98759887/",
            candidate_article_id="jvn-ja",
        )

        assert dup is not None
        assert dup.article_id == "jvn-en"

    def test_articles_without_ids_are_unaffected(self, repo: RunHistoryRepository) -> None:
        """id を持たない一般記事の判定は変えない (適用範囲外)。"""
        _seed(
            repo,
            article_id="news-a",
            title="Cl0p exploits Windchill vulnerability to steal data",
            feed_title="Help Net Security",
            hours_ago=2.0,
            url="https://example.com/a",
        )

        dup = find_recent_content_duplicate(
            repo=repo,
            title="Cl0p exploits Windchill vulnerability to steal customer data",
            url="https://other.example/b",
            candidate_article_id="news-b",
        )

        assert dup is not None
