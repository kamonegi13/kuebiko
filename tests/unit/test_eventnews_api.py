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
    def test_list_includes_singletons_by_default(self) -> None:
        """単独記事も **既定で** 一覧に出す (案 A) — 絞ると読む場所が 2 つになる。

        事象単位化の目的は「読む場所を 1 つにする」ことで、生成の有無で既定から
        出し分けると読み手は記事一覧と往復する。2026-08-25 に読み手が自分で選べる
        絞り込み (``has_news`` / ``min_independent_sources``) を足したが、
        **既定値は「絞らない」から動かしてはいけない**。
        """
        import inspect

        from src.ui.api.eventnews import list_event_news

        params = inspect.signature(list_event_news).parameters
        assert params["has_news"].default is None
        assert params["min_independent_sources"].default == 0

    def test_event_only_filters_reach_the_query(self) -> None:
        """事象固有の軸は **DB 側** で絞る (取得後の filter は LIMIT と噛み合わない)。

        取得後に間引くと「新着 N 件のうち複数媒体のもの」になり、
        「複数媒体の新着 N 件」にならない (遡及構築で 2,000 件規模になり顕在化済み)。
        """
        import inspect

        from src.ui.api.eventnews import list_event_news

        head = inspect.getsource(list_event_news).split("items.append")[0]
        call = head.split("repo.list_event_items(")[1]
        assert "min_independent_sources=" in call
        assert "has_news=has_news" in call

    def test_list_row_carries_a_headline(self) -> None:
        """一覧行は見出しを持つ (生成があれば生成見出し、無ければ原記事タイトル)。"""
        import inspect

        from src.ui.api.eventnews import _headlines_and_previews

        src = inspect.getsource(_headlines_and_previews)
        assert "art.title" in src, "単独記事は原記事タイトルを見出しにする"


class TestListPerformance:
    def test_list_resolves_headlines_in_bulk(self) -> None:
        """一覧はアイテムごとに版・記事を引かない (N+1 は数日で体感悪化する)。

        1 日 ~127 件のペースで事象が増えるため、limit=80 で 160 クエリになる。
        版は latest_event_versions、記事は get_articles_by_ids で各 1 クエリにまとめる。
        """
        import inspect

        from src.ui.api.eventnews import _headlines_and_previews, list_event_news

        list_src = inspect.getsource(list_event_news)
        assert "_headlines_and_previews" in list_src
        # ループ内で 1 件ずつ引いていないこと
        loop_body = list_src.split("for r in shown:")[-1]
        assert "list_event_versions" not in loop_body
        assert "get_articles_by_ids" not in loop_body

        bulk_src = inspect.getsource(_headlines_and_previews)
        assert bulk_src.count("repo.latest_event_versions") == 1
        assert bulk_src.count("repo.get_articles_by_ids") == 1

    def test_latest_version_is_taken_from_the_front(self) -> None:
        """list_event_versions は version DESC — versions[-1] は **最古** になる。"""
        import inspect

        from src.ui.api.eventnews import _version_payload

        src = inspect.getsource(_version_payload)
        assert "versions[0]" in src
        assert "versions[-1]" not in src
