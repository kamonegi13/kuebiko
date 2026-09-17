"""事象ニュース層の import 境界関門 (allowlist / default-deny)。

設計 SSoT: docs/event_news_design.md §2。生成物のパイプライン還流
(triage / routing / synthesis 証拠 / 検索 / digest / spotlight / forecast) を
denylist の数え漏れごと防ぐため、src/ 内で src/eventnews を import してよい場所を
allowlist で固定する。新しい消費者 (UI API 等) を作るときはここに 1 行足すこと —
それが「公開面に露出する変更である」ことを明示する儀式になる。
"""

from __future__ import annotations

import re
from pathlib import Path

# src/ 内で eventnews を import してよいモジュール (repo-relative)。
# 許可された消費者。**default-deny** — ここに無いモジュールが import すると落ちる。
# 増やすときは「生成物がどこへ流れるか」を意識して 1 行足すこと (それがこの関門の目的)。
#
# - repo_eventnews: storage の Mixin (ItemState 型と VERSION_CAP を読む infra。消費者ではない)
# - eventnews_hourly_job: 毎時ジョブ本体 (2026-08-24)。**生成するだけで、生成物を
#   他層へ渡さない** — 読み手向けの出口 (UI/Discord) は未配線で、追加時は別途 1 行要る
_ALLOWLIST: frozenset[str] = frozenset(
    {
        "src/storage/repo_eventnews.py",
        "src/ui/services/eventnews_hourly_job.py",
        # 2026-08-24: 読み手向けの出口 (分析者向け)。2026-08-25 に匿名からは外し、
        # 公開面は public_news.py が担うようになった (allowlist は default-deny)。
        "src/ui/api/eventnews.py",
        # 2026-08-25: **公開ニュース (Tier0 = 匿名で閲覧可) の唯一の出口**。
        # 参照するのは注目の採点 (src/eventnews/urgency.py) のみで、生成物を
        # 他層へ還流させない。公開範囲の判断は test_public_news_api.py が固定する。
        "src/ui/api/public_news.py",
        # 2026-09-17: detect ML (SYNTHESIS §47)。参照するのは記事種別の分類器
        # (src/eventnews/event_kind: KINDS と classify、cache は article_kinds) のみで、
        # 事象ニュースの生成物 (event_items / 版) は読まない。種別は記事単位の性質であって
        # 事象ニュース固有の判断ではない。
        "src/synthesis/grounded/detect_features.py",
        "src/assessment/stateful.py",
    }
)

_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+src\.eventnews\b", re.MULTILINE)


class TestEventNewsImportBoundary:
    def test_only_allowlisted_modules_import_eventnews(self) -> None:
        src_root = Path(__file__).resolve().parents[2] / "src"
        offenders: list[str] = []
        for path in src_root.rglob("*.py"):
            rel = path.relative_to(src_root.parent).as_posix()
            if rel.startswith("src/eventnews/"):
                continue
            if _IMPORT_RE.search(path.read_text(encoding="utf-8")) and rel not in _ALLOWLIST:
                offenders.append(rel)
        assert offenders == [], (
            f"eventnews を import する未許可モジュール: {offenders} — "
            "消費者を増やすなら test_eventnews_boundary._ALLOWLIST に明示追加すること"
        )

    def test_eventnews_does_not_import_judgment_layers(self) -> None:
        """逆向きも固定: eventnews が triage/routing/synthesis を読まない (§2 一方向)。"""
        pkg = Path(__file__).resolve().parents[2] / "src" / "eventnews"
        banned = re.compile(
            r"^\s*(?:from|import)\s+src\.(?:tools\.article_triage|synthesis|pir\.|forecast)",
            re.MULTILINE,
        )
        offenders = [
            p.name for p in pkg.rglob("*.py") if banned.search(p.read_text(encoding="utf-8"))
        ]
        assert offenders == [], f"eventnews から判断層への依存を検出: {offenders}"


# --- メタデータ集約の回帰 ---------------------------------------------------
#
# 2026-08-24: `facet_counts.setdefault(k, {})[v] = facet_counts[k].get(v, 0) + 1`
# と 1 行で書いたため、**右辺が setdefault より先に評価されて** KeyError になり、
# 詳細 API が 500 を返した。単体テストでは書式の正しさしか見ておらず、実物を
# 叩くまで気付けなかった。ここで集約そのものを固定する。


class _FakeRepo:
    def __init__(self, entities: dict[str, dict[str, int]], articles: dict[str, object]) -> None:
        self._entities = entities
        self._articles = articles

    def count_entities_for_articles(self, ids: list[str]) -> dict[str, dict[str, int]]:
        return self._entities

    def get_articles_by_ids(self, ids: list[str]) -> dict[str, object]:
        return self._articles


class _FakeArticle:
    """ArticleRecord の判定関連フィールドだけを持つ代役 (既定は未判定)。"""

    _FIELDS = (
        "subject_actor_ids",
        "victim_sector_canonical",
        "victim_country_iso",
        "socio_political_intent",
        "intent_confidence",
        "category",
        "editorial_stance",
        "posted_channel",
        "socio_political_rationale",
        "technical_axis_summary",
        "remediation",
        "analyst_note",
    )

    def __init__(self, **kw: object) -> None:
        for name in self._FIELDS:
            setattr(self, name, kw.get(name))


class _FakeMember:
    def __init__(self, article_id: str) -> None:
        self.article_id = article_id


def test_metadata_aggregates_facets_across_members() -> None:
    """複数記事に跨る facet を件数付きで集計する。"""
    # Arrange
    from typing import cast

    from src.storage.run_history import RunHistoryRepository
    from src.ui.api.eventnews import _metadata_payload

    repo = _FakeRepo(
        {"cve": {"CVE-2026-1": 2, "CVE-2026-2": 1}},
        {
            "a1": _FakeArticle(victim_sector_canonical="government", category="apt"),
            "a2": _FakeArticle(victim_sector_canonical="government", category="vuln"),
        },
    )

    # Act
    out = _metadata_payload(
        cast(RunHistoryRepository, repo), [_FakeMember("a1"), _FakeMember("a2")]
    )

    # Assert
    sector = next(f for f in out["facets"] if f["key"] == "victim_sector")
    assert sector["values"] == [{"value": "government", "articles": 2}]
    # 表示名と語彙名は backend が指定する (ui_copy_policy: 生 enum を直出ししない)
    assert sector["label"] == "被害セクター"
    assert sector["vocab"] == "sector"
    cve = next(g for g in out["entities"] if g["type"] == "cve")
    assert cve["values"][0] == {"value": "CVE-2026-1", "articles": 2}


def test_free_text_judgement_keeps_per_article_source_numbers() -> None:
    """自由記述の判定欄は 1 本にまとめず、記事ごとに出典番号を付けて並べる。

    要約すると決定論の集計でなくなる (生成物になる)。出典番号は members の並びと
    一致していなければ、読み手が [N] から原記事へ辿れない。
    """
    # Arrange
    from typing import cast

    from src.storage.run_history import RunHistoryRepository
    from src.ui.api.eventnews import _metadata_payload

    repo = _FakeRepo(
        {},
        {
            "a1": _FakeArticle(remediation="パッチ適用"),
            "a2": _FakeArticle(remediation="回避策なし"),
        },
    )

    # Act
    out = _metadata_payload(
        cast(RunHistoryRepository, repo), [_FakeMember("a1"), _FakeMember("a2")]
    )

    # Assert
    block = next(t for t in out["judgement"]["texts"] if t["label"] == "対処")
    assert block["items"] == [
        {"text": "パッチ適用", "source_index": 1},
        {"text": "回避策なし", "source_index": 2},
    ]


def test_metadata_is_empty_without_members() -> None:
    from typing import cast

    from src.storage.run_history import RunHistoryRepository
    from src.ui.api.eventnews import _metadata_payload

    out = _metadata_payload(cast(RunHistoryRepository, _FakeRepo({}, {})), [])
    assert out == {"entities": [], "subject_actors": [], "facets": [], "judgement": {}}


def test_list_filters_apply_before_limit(tmp_path: object) -> None:
    """絞り込みは LIMIT より前に効くこと。

    取得後に filter すると「新着 N 件のうち high のもの」になり、「high の新着 N 件」に
    ならない。アイテムが数十件の間は気付かないが、遡及構築で 2,000 件規模になると
    high 絞り込みがほとんど何も返さなくなる。
    """
    # Arrange — medium を 10 件、その後ろ (古い側) に high を 3 件
    from datetime import UTC, datetime, timedelta

    from src.storage.run_history import RunHistoryRepository

    repo = RunHistoryRepository(db_path=tmp_path / "list.db")  # type: ignore[operator]
    base = datetime(2026, 8, 1, tzinfo=UTC)
    for i in range(10):
        repo.create_event_item(
            item_id=f"ev-m{i}",
            origin="live",
            first_reported_at=base + timedelta(hours=i + 100),
            last_reported_at=base + timedelta(hours=i + 100),
            importance="medium",
        )
    for i in range(3):
        repo.create_event_item(
            item_id=f"ev-h{i}",
            origin="live",
            first_reported_at=base + timedelta(hours=i),
            last_reported_at=base + timedelta(hours=i),
            importance="high",
        )

    # Act — 新着 5 件だけを見ると high は 0 件だが、high で絞れば 3 件返るべき
    got = repo.list_event_items(origin="live", importances=["high"], limit=5)

    # Assert
    assert [r.state.item_id for r in got] == ["ev-h2", "ev-h1", "ev-h0"]


def test_prompt_does_not_ask_for_inline_citation_numbers() -> None:
    """プロンプトが「text に [N] を書け」と「書くな」を同時に言っていないこと。

    2026-08-24: 二重表記を止めるために「text の中に [N] を書かない」を足したが、
    その上にあった「1 media のみが報じている事実は『[N] のみが報じる』と明示する」を
    直し忘れ、**矛盾した指示**になっていた。結果 LLM は媒体名を参照しようとして
    識別子一覧に無いため置換され、「と (原文参照) のみが報じる」という壊れた文が
    出た (実測 2/119 件)。
    """
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / "prompts" / "eventnews" / "refine.j2").read_text(
        encoding="utf-8"
    )
    # 「text に書かない」側の指示は必ず在ること
    assert "text の中に `[1]` のような出典番号を書かない" in text
    # 「[N] のみが報じる」と書かせる指示が残っていないこと
    assert "「[N] のみが報じる」" not in text
    # 掲載場所・URL を事実として書かせない (本文に無い URL は置換され文が壊れる)
    assert "掲載場所・URL を内容として書かない" in text


def test_list_filters_lift_article_matches_to_events(tmp_path: object) -> None:
    """記事側の絞り込みが「該当メンバーを含む事象」に持ち上がること。

    事象は記事の集合なので、絞り込みは **1 件でも該当メンバーを含むか** で判定する。
    """
    from datetime import UTC, datetime, timedelta

    from src.storage.run_history import RunHistoryRepository

    repo = RunHistoryRepository(db_path=tmp_path / "filter.db")  # type: ignore[operator]
    base = datetime(2026, 8, 1, tzinfo=UTC)
    for i in range(2):
        repo.create_event_item(
            item_id=f"ev-{i}",
            origin="live",
            first_reported_at=base + timedelta(hours=i),
            last_reported_at=base + timedelta(hours=i),
            importance="high",
        )
        repo.add_event_member(
            item_id=f"ev-{i}",
            article_id=f"a-{i}",
            joined_at=base,
            contributed_new_facts=1,
            join_signal="seed",
        )

    got = repo.list_event_items(origin="live", member_article_ids=["a-1"], limit=10)
    assert [r.state.item_id for r in got] == ["ev-1"]
    # 空リスト = 「該当記事ゼロ」なので事象もゼロ (全件化させない)
    assert repo.list_event_items(origin="live", member_article_ids=[], limit=10) == []
    # None = 絞らない
    assert len(repo.list_event_items(origin="live", member_article_ids=None, limit=10)) == 2


def test_list_offset_paginates(tmp_path: object) -> None:
    """offset でページングできること (新着順)。"""
    from datetime import UTC, datetime, timedelta

    from src.storage.run_history import RunHistoryRepository

    repo = RunHistoryRepository(db_path=tmp_path / "page.db")  # type: ignore[operator]
    base = datetime(2026, 8, 1, tzinfo=UTC)
    for i in range(3):
        repo.create_event_item(
            item_id=f"ev-{i}",
            origin="live",
            first_reported_at=base + timedelta(hours=i),
            last_reported_at=base + timedelta(hours=i),
            importance="high",
        )
    page1 = repo.list_event_items(origin="live", limit=2)
    page2 = repo.list_event_items(origin="live", limit=2, offset=2)
    assert [r.state.item_id for r in page1] == ["ev-2", "ev-1"]
    assert [r.state.item_id for r in page2] == ["ev-0"]


def test_search_matches_generated_text_or_member_articles(tmp_path: object) -> None:
    """検索は「生成本文に含む」または「構成記事に含む」の OR で一致する。

    生成本文は日本語・原記事は英語のことが多く、標本 60 事象のうち 57 件が
    「生成本文にしか無い語」を含んでいた。一覧で見えているのは生成された見出しなので、
    そこに見える語で引けないのは事故。
    """
    from datetime import UTC, datetime

    from src.storage.run_history import RunHistoryRepository

    repo = RunHistoryRepository(db_path=tmp_path / "search.db")  # type: ignore[operator]
    now = datetime(2026, 8, 1, tzinfo=UTC)
    for i in range(2):
        repo.create_event_item(
            item_id=f"ev-{i}",
            origin="live",
            first_reported_at=now,
            last_reported_at=now,
            importance="high",
        )
        repo.add_event_member(
            item_id=f"ev-{i}",
            article_id=f"a-{i}",
            joined_at=now,
            contributed_new_facts=1,
            join_signal="seed",
        )
    repo.record_event_version(
        item_id="ev-0",
        version=1,
        generated_at=now,
        model="m",
        prompt_version="v",
        headline="バッファオーバーフローの脆弱性",
        body_json="{}",
        new_facts_json="[]",
        verified_at=now,
        dropped_lines=0,
        repaired_ids=0,
    )

    # 生成本文だけに在る語で引ける
    assert repo.search_event_versions("バッファオーバーフロー") == ["ev-0"]
    got = repo.list_event_items(
        origin="live", search_item_ids=["ev-0"], search_member_article_ids=[], limit=10
    )
    assert [r.state.item_id for r in got] == ["ev-0"]
    # 構成記事側だけの一致でも引ける (OR)
    got2 = repo.list_event_items(
        origin="live", search_item_ids=[], search_member_article_ids=["a-1"], limit=10
    )
    assert [r.state.item_id for r in got2] == ["ev-1"]
    # 両方空 = 該当なし (全件化させない)
    assert (
        repo.list_event_items(
            origin="live", search_item_ids=[], search_member_article_ids=[], limit=10
        )
        == []
    )


def test_personal_notes_are_not_anonymously_readable() -> None:
    """個人メモが公開面 (Tier0 匿名) から読めないこと。

    2026-08-24: `GET /api/v1/notes` が readonly instance から本文ごと読めていた
    (分析者の所見がそのまま公開面に出ていた)。denylist への追加漏れが原因で、
    2026-08-25 に allowlist (default-deny) へ反転した。Tier1 (Cloudflare Access
    認証済み) では従来どおり閲覧可。
    """
    from src.ui.read_only_policy import is_read_only_blocked_get

    assert is_read_only_blocked_get("/api/v1/notes")
    assert is_read_only_blocked_get("/api/v1/event-notes")
    assert is_read_only_blocked_get("/api/v1/notes")
    assert is_read_only_blocked_get("/api/v1/event-notes/ev-1")
    # 2026-08-25: 事象ニュース (分析者向け) も匿名からは外し、公開面は
    # `/api/v1/public/news` が担う (原記事の本文を返さない契約付き)。
    assert is_read_only_blocked_get("/api/v1/eventnews")
    assert not is_read_only_blocked_get("/api/v1/public/news")
