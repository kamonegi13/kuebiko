"""テスト全体の共通設定。"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _disable_local_llm_usage_recording(monkeypatch: pytest.MonkeyPatch) -> None:
    """ローカル LLM の使用量記録 (P0-16) をテストでは止める。

    止めないと、テストの LLM 呼び出し (モック越しを含む) が本番と同じ ``llm_usage`` の
    統計に混ざる。記録そのものを試すテストは自分で ``setenv("LLM_USAGE_LOCAL", "1")`` する。
    """
    monkeypatch.setenv("LLM_USAGE_LOCAL", "0")


@pytest.fixture(autouse=True)
def _isolate_local_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """運用者ローカルの上書き層 (config/local/) をテストから切り離す。

    開発機の実ファイル (gitignore) の有無でテストの結果が変わらないよう、存在しない
    一時ディレクトリへ向ける。ローカル層を試すテストはこの env の先にファイルを書く。
    """
    monkeypatch.setenv(
        "KUEBIKO_LOCAL_CONFIG_DIR", str(tmp_path_factory.mktemp("local_overlay") / "local")
    )
