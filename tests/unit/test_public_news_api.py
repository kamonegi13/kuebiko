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

    def test_module_never_reads_publisher_body(self) -> None:
        src = pathlib.Path(public_news.__file__).read_text()
        for forbidden in ("get_article_bodies", ".body_ja", "art.body", '"body"'):
            assert forbidden not in src, f"公開 API が出版社の本文に触れている: {forbidden}"

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
    def test_only_high_importance_is_public(self) -> None:
        assert public_news._PUBLIC_IMPORTANCES == ("high",)

    def test_list_filters_by_importance(self) -> None:
        src = inspect.getsource(public_news.list_public_news)
        assert "importances=list(_PUBLIC_IMPORTANCES)" in src

    def test_detail_rejects_non_high(self) -> None:
        src = inspect.getsource(public_news.get_public_news)
        assert "importance not in _PUBLIC_IMPORTANCES" in src


class TestDedupIsRespected:
    """kuebiko 自身が重複と判定したものを単独記事として公開しない (契約 4)。"""

    def test_duplicate_only_items_are_excluded_before_the_limit(self) -> None:
        """SQL 側で落とす。取得後に間引くと 1 ページの件数が欠ける (実測 limit=6 で 4 件)。"""
        src = inspect.getsource(public_news.list_public_news)
        assert "exclude_duplicate_only=True" in src
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
        assert "member_categories=" in src.split("for r in records:")[0]

    def test_list_advertises_the_categories(self) -> None:
        """フロントがカテゴリ一覧を別経路で持たないよう、一覧が自分で返す。"""
        src = inspect.getsource(public_news.list_public_news)
        assert '"categories": list(PUBLIC_CATEGORIES)' in src


class TestFeatured:
    def test_featured_requires_corroboration_and_a_generated_body(self) -> None:
        """注目枠は「複数媒体が報じ、かつ統合本文がある」もの。単独報を注目にしない。"""
        src = inspect.getsource(public_news.list_public_news)
        assert "min_independent_sources=2 if featured else 0" in src
        assert "has_news=True if featured else None" in src
