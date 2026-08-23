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
