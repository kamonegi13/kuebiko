"""PIR facet dropdown 用の軽量経路の契約。

2026-08-25 の障害: ニュース検索と事象ニュースの facet を共通化した際、dropdown が
**KPI 付きの一覧** (`GET /api/v1/pir`) を叩いていた。あちらは 30 日 × 15,000 記事を
走査するため cold 1.9 秒 + DB 接続を 1 本占有する。全ページ読み込みで呼ばれるように
なった結果、接続プール (max 10) を使い切ってアプリ全体が 30 秒 timeout で停止した。
"""

from __future__ import annotations

import inspect

from src.ui.api.pir import list_pir_options, list_pirs, pir_api


def _paths() -> list[str]:
    return [getattr(r, "path", "") for r in pir_api.routes]


class TestLightweightOptions:
    def test_options_is_registered_before_the_id_route(self) -> None:
        """FastAPI は登録順に照合する。`/{pir_id}` の後ろに置くと捕まって 404 になる。"""
        paths = _paths()
        assert "/api/v1/pir/options" in paths, "軽量 options 経路が無い"
        assert paths.index("/api/v1/pir/options") < paths.index("/api/v1/pir/{pir_id}")

    def test_options_does_not_scan_articles(self) -> None:
        """dropdown の選択肢に KPI 評価は要らない。記事へ触れた時点で設計が壊れている。"""
        src = inspect.getsource(list_pir_options)
        assert "evaluate_pirs_batch" not in src
        assert "evaluate_pir_matches" not in src
        assert "_load_posted_rows" not in src

    def test_options_returns_only_identity_fields(self) -> None:
        """id / title / enabled のみ。増やすなら公開面 (Tier0) への露出を再検討すること。"""
        from src.ui.api.pir import PirOption

        assert set(PirOption.model_fields) == {"id", "title", "enabled"}


class TestListStampedeGuard:
    def test_list_recomputes_under_a_lock(self) -> None:
        """TTL 切れの瞬間に N 本来ても走査は 1 本だけにする。

        N 本同時に走ると DB 接続を N 本掴み、max 10 のプールを使い切って
        **アプリ全体**が止まる (実際に停止した)。
        """
        src = inspect.getsource(list_pirs)
        assert "_LIST_LOCK" in src
        # lock 取得後に必ず再チェックする (先行スレッドの結果を捨てない)
        after_lock = src.split("_LIST_LOCK")[1]
        assert "_LIST_CACHE.get" in after_lock
