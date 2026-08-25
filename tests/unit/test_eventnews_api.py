"""事象ニュース read API の公開契約。

Tier0 (匿名可) として出すため、**何を返し何を返さないか**をテストで固定する。
設計 SSoT: docs/event_news_design.md §3。
"""

from __future__ import annotations

from types import SimpleNamespace

from src.ui.api.eventnews import GENERATED_NOTE
from src.ui.read_only_policy import is_public_get


class TestPublicExposure:
    def test_event_news_is_not_anonymous(self) -> None:
        """2026-08-25 改訂: 分析者向けの判定メタデータや原記事の本文冒頭まで返すため
        **匿名には出さない**。公開面は `/api/v1/public/news` が担う。
        """
        assert not is_public_get("/api/v1/eventnews")
        assert not is_public_get("/api/v1/eventnews/ev-1")
        assert is_public_get("/api/v1/public/news")

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


class TestPreviewFallback:
    """一覧の冒頭テキストの決め方。

    2026-08-25: 一覧に **本文が一切出ないカード** が並んでいた。原因は単独報の
    preview を ``summary`` だけから作っていたこと。事象は被覆のため
    ``skipped_duplicate`` の記事も構成記事に含むが、重複判定された記事は
    **要約 LLM を通らない**ので summary が空になる。実測では単独報 high 433 件の
    うち 95 件が skipped_duplicate で、うち 92 件が要約空・**本文は全件あり**。
    """

    def test_uses_summary_when_present(self) -> None:
        from src.ui.api.eventnews import _preview_text

        assert _preview_text(SimpleNamespace(summary="要約です。")) == "要約です。"

    def test_returns_empty_when_summary_is_missing(self) -> None:
        """本文は ArticleRecord に無いので、ここでは空を返し呼び手が埋める。"""
        from src.ui.api.eventnews import _preview_text

        assert _preview_text(SimpleNamespace(summary="")) == ""
        assert _preview_text(SimpleNamespace()) == ""

    def test_clip_collapses_whitespace_and_truncates(self) -> None:
        from src.ui.api.eventnews import _PREVIEW_CHARS, _clip

        assert _clip("行1\n\n  行2\t行3") == "行1 行2 行3"
        assert len(_clip("あ" * 500)) == _PREVIEW_CHARS

    def test_article_record_has_no_body_so_a_lookup_is_required(self) -> None:
        """**実型の契約**を固定する。

        当初 ``getattr(art, "body", "")`` で埋めるつもりだったが、``ArticleRecord``
        は body を持たず、フォールバックが無言で空振りしていた
        (偽オブジェクトのテストは通るのに本番では直らない)。body が将来入るなら
        この test が落ちるので、そのとき ``get_article_bodies`` 経由をやめてよい。
        """
        from src.storage.records import ArticleRecord

        assert "body" not in ArticleRecord.__annotations__
        assert "summary" in ArticleRecord.__annotations__

    def test_list_fills_empty_previews_from_bodies_in_one_query(self) -> None:
        """要約が空の分は **1 クエリ** で本文から埋める (N+1 にしない)。"""
        import inspect

        from src.ui.api.eventnews import _headlines_and_previews

        src = inspect.getsource(_headlines_and_previews)
        assert src.count("repo.get_article_bodies") == 1
        assert "for item_id, aid in need_body.items()" in src


class TestSemanticSearch:
    """意味検索の事象への移植 (2026-08-25)。

    実測 (5 クエリ): 語句検索と重なるのは 0-4 件で、**16-20 件は意味検索にしか
    出ない**。「ランサムウェアによる製造業への攻撃」は語句 0 件 / 意味 20 件。
    """

    def test_is_opt_in(self) -> None:
        """既定は off。embedding を毎回の検索で走らせない。"""
        import inspect

        from src.ui.api.eventnews import list_event_news

        assert inspect.signature(list_event_news).parameters["semantic"].default is False

    def test_is_combined_with_keyword_search_by_or(self) -> None:
        """語句一致を **狭めない**。言い換えを足すのが目的。"""
        import inspect

        from src.ui.api.eventnews import list_event_news

        src = inspect.getsource(list_event_news)
        block = src.split("if semantic and term:")[1][:400]
        assert "*(search_member_ids or [])" in block, "語句側の結果を捨てている"

    def test_is_not_reachable_anonymously(self) -> None:
        """⚠ 公開面 (Tier0) に embedding 計算を開放しない。

        匿名で LLM/embedding を消費できる経路は 2026-08-25 に閉じたばかり
        (`/api/v1/search?mode=precise` 等)。事象ニュースはそもそも匿名から
        読めないので、この経路も自動的に閉じている — それを固定する。
        """
        assert not is_public_get("/api/v1/eventnews")

    def test_public_news_has_no_semantic_parameter(self) -> None:
        """公開 API 側に持ち込まれていないこと。"""
        import inspect

        from src.ui.api.public_news import list_public_news

        assert "semantic" not in inspect.signature(list_public_news).parameters

    def test_embedder_absence_degrades_to_keyword_only(self) -> None:
        """embedder 未設定でも検索は動く (意味検索だけ無効になる)。"""
        import inspect

        from src.ui.api.eventnews import _semantic_article_ids

        src = inspect.getsource(_semantic_article_ids)
        assert "if embedder is None" in src
        assert "return None" in src

    def test_runs_off_the_event_loop(self) -> None:
        """同期 endpoint から asyncio.run で完結させる (event loop を塞がない)。"""
        import inspect

        from src.ui.api.eventnews import _semantic_article_ids, list_event_news

        assert not inspect.iscoroutinefunction(list_event_news)
        assert "asyncio.run" in inspect.getsource(_semantic_article_ids)
