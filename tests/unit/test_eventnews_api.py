"""事象ニュース read API の公開契約。

Tier0 (匿名可) として出すため、**何を返し何を返さないか**をテストで固定する。
設計 SSoT: docs/event_news_design.md §3。
"""

from __future__ import annotations

from src.ui.api.eventnews import GENERATED_NOTE
from src.ui.read_only_policy import READ_ONLY_GET_DENYLIST


class TestPublicExposure:
    def test_event_news_is_tier0_not_in_denylist(self) -> None:
        """公開判断 (2026-08-24): 公開記事から生成した読み物で運用情報を含まない。

        denylist に入れると公開面から消えるため、**入れない**ことを明示的に固定する
        (将来 denylist を編集する人に、これが判断済みであることを伝える)。
        """
        assert not any(path.startswith("/api/v1/eventnews") for path in READ_ONLY_GET_DENYLIST)

    def test_routes_are_get_only(self) -> None:
        """readonly instance は POST/PUT/PATCH/DELETE を 403 で塞ぐ。GET のみなら追加ガード不要。"""
        from src.ui.api.eventnews import eventnews_api

        for route in eventnews_api.routes:
            methods: set[str] = getattr(route, "methods", set())
            assert methods <= {"GET", "HEAD"}, f"{route} が write を持つ"


class TestGeneratedContentIsLabelled:
    def test_note_states_it_is_generated(self) -> None:
        """生成物と原ソースの区別 (§3) — レスポンスが常に生成物である旨を伝える。"""
        assert "生成" in GENERATED_NOTE
        assert "原記事" in GENERATED_NOTE


class TestSingleReadingSurface:
    def test_list_includes_singletons(self) -> None:
        """単独記事も一覧に出す (案 A) — ここを複数媒体に絞ると読む場所が 2 つになる。

        事象単位化の目的は「読む場所を 1 つにする」ことで、生成の有無で出し分けると
        読み手は記事一覧と往復することになり目的を果たさない。
        """
        import inspect

        from src.ui.api.eventnews import list_event_news

        src = inspect.getsource(list_event_news)
        # 生成の有無 (current_version / has_news) で除外していないこと
        assert "has_news" not in src.split("items.append")[0]
        assert "current_version > 0" not in src.split("items.append")[0]

    def test_list_row_carries_a_headline(self) -> None:
        """一覧行は見出しを持つ (生成があれば生成見出し、無ければ原記事タイトル)。"""
        import inspect

        from src.ui.api.eventnews import _headline_and_preview

        src = inspect.getsource(_headline_and_preview)
        assert "art.title" in src, "単独記事は原記事タイトルを見出しにする"
