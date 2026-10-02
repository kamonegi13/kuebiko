"""公開ニュース API (Tier0) の契約。

匿名の第三者が読む唯一の面なので、**何を返さないか**をテストで固定する。
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from types import SimpleNamespace

from src.ui.api import public_news
from src.ui.read_only_policy import PUBLIC_GET_ALLOWLIST, is_public_get


class TestAnonymousSurface:
    def test_public_news_is_the_only_content_api_exposed(self) -> None:
        """匿名で読める API は allowlist の 4 つだけ (default-deny)。

        2026-08-25 に denylist から反転した。反転前は 98 個の GET のうち 67 個が
        匿名で読め、PIR (収集関心の定義)・取込履歴・購読ソース構成・Grok アカウント
        状態・LLM を消費する精密検索まで含まれていた。
        """
        assert is_public_get("/api/v1/public/news")
        assert is_public_get("/api/v1/public/news/ev-1")
        for path in (
            "/api/v1/pir",
            "/api/v1/articles",
            "/api/v1/search",
            "/api/v1/notes",
            "/api/v1/runs/recent",
            "/api/v1/subscriptions",
            "/api/v1/grok-mail",
            "/api/v1/intel-graph/synthesis",
            "/api/v1/actors",
            "/api/v1/export/articles.csv",
            "/api/v1/semantic-search",
        ):
            assert not is_public_get(path), f"{path} が匿名に露出している"

    def test_spa_and_assets_stay_public(self) -> None:
        """API 以外 (SPA shell / assets / 認証導線) は公開のまま — 弾くと画面が出ない。"""
        for path in ("/", "/app", "/app/news", "/assets/index-abc.js", "/auth/login"):
            assert is_public_get(path)

    def test_allowlist_contains_no_operational_api(self) -> None:
        """allowlist に運用系を足していないか (増やすときの歯止め)。"""
        assert set(PUBLIC_GET_ALLOWLIST) == {
            "/api/health",
            "/api/v1/runtime-flags",
            "/api/v1/vocabularies",
            "/api/v1/public/news",
        }

    def test_routes_are_get_only(self) -> None:
        for route in public_news.public_news_api.routes:
            methods: set[str] = getattr(route, "methods", set())
            assert methods <= {"GET", "HEAD"}


class TestRedistributionBoundary:
    """CLAUDE.md §9 の線引き。

    - ``articles.body`` / ``body_ja`` = trafilatura で抽出した **原記事そのもの**。
      公開すれば再配布になる。§10 が robots.txt を無視する根拠を「配信は要約 +
      引用 URL のみ」に置いているため、ここを破るとその前提ごと崩れる
    - ``articles.summary`` = **kuebiko の LLM が書いた要約**。§9 が明示的に認めている
    """

    def test_payload_builders_never_touch_publisher_body(self) -> None:
        """**返す側**は本文に触れない。

        2026-08-25: 注目の採点で「実際に悪用されている」を本文から判定する必要が
        生じたため、`_score_pool` だけは本文を読む。ただし読んだ本文は **点数に
        しかならず**、レスポンスには出ない。契約は「読まない」ではなく
        **「返さない」** なので、返す側の関数を名指しで固定する。
        """
        for fn in (public_news.list_public_news, public_news.get_public_news):
            src = inspect.getsource(fn)
            for forbidden in ("get_article_bodies", ".body_ja", "art.body", '"body"'):
                assert forbidden not in src, f"{fn.__name__} が本文に触れている: {forbidden}"

    def test_scoring_returns_scores_not_text(self) -> None:
        """採点は本文を読んでよいが、返すのは点数と事象だけ。"""
        import typing

        hints = typing.get_type_hints(public_news._score_pool)
        assert str(hints["return"]).replace(" ", "").startswith("list[tuple[int,")

    def test_kuebiko_summary_is_allowed(self) -> None:
        """単独報は kuebiko の要約を出す。高 importance の 94% が要約を持つ。"""
        src = inspect.getsource(public_news.list_public_news)
        assert 'getattr(first, "summary", "")' in src

    def test_payload_keys_are_explicit(self) -> None:
        """返す dict のキーを固定する (うっかり本文キーが増えないように)。"""
        tree = ast.parse(pathlib.Path(public_news.__file__).read_text())
        keys: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for k in node.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        keys.add(k.value)
        assert "body" not in keys
        assert "body_ja" not in keys
        assert "content" not in keys


class TestCitationsAreMandatory:
    def test_items_without_a_citation_are_dropped(self) -> None:
        """出典 (媒体名 + 原記事 URL) を示せない項目は公開しない。"""
        src = inspect.getsource(public_news.list_public_news)
        assert "if not citations:" in src
        assert "continue" in src.split("if not citations:")[1][:80]

    def test_detail_404s_without_citations(self) -> None:
        src = inspect.getsource(public_news.get_public_news)
        assert "if not citations:" in src
        assert "404" in src.split("if not citations:")[1][:120]

    def test_internal_article_id_is_not_published(self) -> None:
        """出典に内部 id を混ぜない (公開面から記事ストアを推測させない)。"""
        src = inspect.getsource(public_news._public_citation)
        assert 'k != "article_id"' in src


class TestHighOnly:
    """公開する重要度 (2026-08-31 に medium を条件付きで開放)。

    high は単独報でも出す / medium は「生成済み かつ 独立 2 媒体以上」/ low は出さない。
    条件の中身は tests/unit/test_public_importance_gate.py が持つ。
    """

    def test_low_is_never_public(self) -> None:
        assert "low" not in public_news._PUBLIC_IMPORTANCES

    def test_high_has_no_extra_condition(self) -> None:
        """重要だから 1 媒体でも知らせる、という判断が既に働いている (案 A)。"""
        assert "high" in public_news._PUBLIC_IMPORTANCES
        assert "high" not in public_news._PUBLIC_IMPORTANCE_RULES

    def test_medium_is_gated(self) -> None:
        assert "medium" in public_news._PUBLIC_IMPORTANCES
        rule = public_news._PUBLIC_IMPORTANCE_RULES["medium"]
        assert rule.min_independent_sources >= 2
        assert rule.requires_news

    def test_list_filters_by_importance(self) -> None:
        src = inspect.getsource(public_news.list_public_news)
        assert '"importances": list(_PUBLIC_IMPORTANCES)' in src
        # 条件も **クエリに渡す** (取得後に filter すると LIMIT より後になる)
        assert '"importance_rules": _PUBLIC_IMPORTANCE_RULES' in src

    def test_detail_uses_the_same_predicate_as_the_list(self) -> None:
        """詳細だけ条件が緩いと、一覧に出ないものが直リンクで読める。"""
        src = inspect.getsource(public_news.get_public_news)
        assert "_is_public(record)" in src


class TestDedupIsRespected:
    """kuebiko 自身が重複と判定したものを単独記事として公開しない (契約 4)。"""

    def test_duplicate_only_items_are_excluded_before_the_limit(self) -> None:
        """SQL 側で落とす。取得後に間引くと 1 ページの件数が欠ける (実測 limit=6 で 4 件)。"""
        src = inspect.getsource(public_news.list_public_news)
        assert '"exclude_duplicate_only": True' in src
        assert "_is_duplicate_only" not in src.split("for r in records:")[1]

    def test_duplicate_only_item_404s(self) -> None:
        src = inspect.getsource(public_news.get_public_news)
        assert "_is_duplicate_only" in src
        assert "404" in src.split("_is_duplicate_only")[1][:200]

    def test_generated_items_survive_even_if_members_were_duplicates(self) -> None:
        """複数媒体をまとめた読み物になっているなら公開してよい。"""
        assert public_news._is_duplicate_only({}, ()) is True
        posted = SimpleNamespace(status="posted")
        dup = SimpleNamespace(status="skipped_duplicate")
        assert public_news._is_duplicate_only({"a": dup, "b": posted}, ("a", "b")) is False
        assert public_news._is_duplicate_only({"a": dup}, ("a",)) is True

    def test_kept_duplicate_counts_as_duplicate(self) -> None:
        """2026-10-02 以降、重複は status='posted' + duplicate_of で残る。公開の判定は変えない。"""
        kept = SimpleNamespace(status="posted", duplicate_of="prior-art")
        posted = SimpleNamespace(status="posted", duplicate_of=None)
        assert public_news._is_duplicate_only({"a": kept}, ("a",)) is True
        assert public_news._is_duplicate_only({"a": kept, "b": posted}, ("a", "b")) is False


class TestCategoryPages:
    """カテゴリ別ページ (公開サイトの導線)。"""

    def test_categories_match_the_shared_group_definition(self) -> None:
        """グループの中身を公開面に複製しない (記事側 facet と同じ定義を使う)。"""
        from src.ui.api.articles_feed import _CATEGORY_GROUPS

        assert public_news._categories_for("vuln") == _CATEGORY_GROUPS["vuln"]
        assert public_news._categories_for("threat") == _CATEGORY_GROUPS["threat"]
        assert public_news._categories_for("incident_breach") == _CATEGORY_GROUPS["incident_breach"]

    def test_single_category_passes_through(self) -> None:
        assert public_news._categories_for("geopolitical") == ["geopolitical"]

    def test_unknown_category_is_rejected_not_ignored(self) -> None:
        """未知の値で「絞らない」にすると、綴り違いが全件表示になって気付けない。

        2026-08-25: `_categories_for` が None を返すのに endpoint 側がそれを
        「絞らない」として扱い、`?category=policy` が全件を返していた。
        **述語のテストだけでは足りない — endpoint が None をどう扱うかまで固定する**。
        """
        assert public_news._categories_for("policy") is None
        assert public_news._categories_for("../../etc") is None

        src = inspect.getsource(public_news.list_public_news)
        assert "if category and categories is None:" in src
        assert "404" in src.split("if category and categories is None:")[1][:120]

    def test_filter_is_applied_before_the_limit(self) -> None:
        src = inspect.getsource(public_news.list_public_news)
        assert '"member_categories": categories' in src.split("for r in records:")[0]

    def test_list_advertises_the_categories(self) -> None:
        """フロントがカテゴリ一覧を別経路で持たないよう、一覧が自分で返す。"""
        src = inspect.getsource(public_news.list_public_news)
        assert '"categories": list(PUBLIC_CATEGORIES)' in src


class TestFeatured:
    """注目 = **読み手が今いちばん知る必要がある事案** (2026-08-25 に 2 度改訂)。

    1. 新しい順      → その窓で最大の話題を 10 日中 7 日 逃していた
    2. 媒体数順      → **報道量は注意の量であって重要性ではない** (利用者指摘)。
       実測で日本関連は公開対象の 4.5%、一次情報は 2.8% しかなく、媒体数順では
       まず選ばれない
    3. 行動要度 (現行)
    """

    def test_ranking_does_not_use_media_count(self) -> None:
        """⚠ 媒体数を順位に混ぜない。同点処理にも使わない (使えば量が順位を決める)。"""
        src = inspect.getsource(public_news._featured_records)
        assert "independent_sources" not in src
        assert "corroboration" not in src
        assert "min_independent_sources" not in src

    def test_ranking_uses_the_urgency_score(self) -> None:
        src = inspect.getsource(public_news._score_pool)
        assert "urgency_score" in src

    def test_ties_break_by_recency(self) -> None:
        src = inspect.getsource(public_news._featured_records)
        assert "last_reported_at" in src

    def test_featured_requires_a_generated_body(self) -> None:
        """単独報を注目にしない。"""
        src = inspect.getsource(public_news._featured_records)
        assert "has_news=True" in src

    def test_featured_is_bounded_to_a_recent_window(self) -> None:
        assert public_news.FEATURED_WINDOW_HOURS == 72
        assert "since=" in inspect.getsource(public_news._featured_records)

    def test_featured_widens_the_window_instead_of_going_empty(self) -> None:
        assert public_news.FEATURED_FALLBACK_HOURS > public_news.FEATURED_WINDOW_HOURS
        src = inspect.getsource(public_news._featured_records)
        assert "for hours in (FEATURED_WINDOW_HOURS, FEATURED_FALLBACK_HOURS)" in src

    def test_roundup_articles_are_excluded(self) -> None:
        """まとめ記事は複数の話題を含み信号が同時に立つ (実測で週刊まとめが 3 位に)。"""
        from src.eventnews.urgency import EXCLUDED_CATEGORIES

        assert "recap" in EXCLUDED_CATEGORIES
        assert "is_excluded_category" in inspect.getsource(public_news._score_pool)

    def test_candidates_are_high_importance_only(self) -> None:
        """重要性は PIR → importance が決める。行動要度はその中の順位付け。"""
        src = inspect.getsource(public_news.list_public_news)
        assert '"importances": list(_PUBLIC_IMPORTANCES)' in src
        # 収集量ではなく importance が母集団を決める (low は入れない)
        assert "low" not in public_news._PUBLIC_IMPORTANCES


class TestPublicMap:
    """公開版の地図 (2026-08-25)。

    このツールの特徴だが、**分析画面の脅威マップをそのまま公開しない**。
    あちらはアクター帰属と意図 (Diamond Model) の層を重ねており、報道された事実
    ではなく kuebiko の分析判断を含む。公開面では根拠の連鎖を示せない。
    """

    def test_map_is_reachable_anonymously(self) -> None:
        """allowlist の前方一致で公開される (別途登録は要らない)。"""
        assert is_public_get("/api/v1/public/news/map")

    def test_map_returns_only_victim_countries(self) -> None:
        """アクター帰属と意図の層を持ち込まない。"""
        src = inspect.getsource(public_news.get_public_map)
        for forbidden in ("actor", "intent", "nation", "geo_events"):
            assert forbidden not in src, f"公開地図に {forbidden} 層が入っている"

    def test_map_population_matches_the_article_list(self) -> None:
        """母集団を記事一覧と揃える。

        別母集団だと「地図は 592 件なのに記事は 30 件」と食い違い読み手が混乱する。
        """
        src = inspect.getsource(public_news.get_public_map)
        assert "importances=list(_PUBLIC_IMPORTANCES)" in src
        assert "exclude_duplicate_only=True" in src
        assert "exclude_merged=True" in src

    def test_map_reports_what_it_could_not_place(self) -> None:
        """⚠ 地図は収集網の観測であって世界ではない。

        実測では公開対象の 56% に被害国が付いていない。割合を隠すと「これが世界の
        実態」と読まれる。placed / unplaced / total を必ず返す。
        """
        src = inspect.getsource(public_news.get_public_map)
        for key in ('"placed"', '"unplaced"', '"total"'):
            assert key in src
        assert "世界全体の実態を示すものではありません" in src

    def test_country_coordinates_and_labels_come_from_the_ssot(self) -> None:
        """座標は Geocoder、表示名は countries.yaml。公開面に辞書を複製しない。"""
        src = inspect.getsource(public_news.get_public_map)
        assert "Geocoder()" in src
        assert "_yaml_display_map(str(_COUNTRIES_YAML))" in src

    def test_unresolvable_country_counts_as_unplaced(self) -> None:
        """座標が引けない国を黙って消さない (合計が合わなくなる)。"""
        src = inspect.getsource(public_news.get_public_map)
        block = src.split("if point is None:")[1][:120]
        assert "unplaced += count" in block
