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
