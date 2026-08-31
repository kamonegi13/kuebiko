"""外部ティアが黙って fallback し続けたときに死活へ出ることを固定する。

2026-08-30: bridge の認証が切れ、**15 時間すべての外部呼出がローカルへ落ちて**
いたのに、ジョブは毎回 succeeded を返すので死活は緑のままだった。遡及が遅い
という別の理由でたまたま気付いた。⭐ **「動いている」と「意図した腕で動いている」
は別**。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.storage.run_history import RunHistoryRepository
from src.ui.services.source_health import build_heartbeat_text


def _heartbeat(external_tier_line: str | None) -> tuple[str, str, str]:
    return build_heartbeat_text(
        run_counts={"succeeded": 30},
        silent=[],
        feeds_total=100,
        external_tier_line=external_tier_line,
    )


def test_warning_line_raises_importance() -> None:
    """⚠️ が付いたら通知の重要度が上がる (本文に出るだけでは気付けない)。"""
    _, body, importance = _heartbeat("⚠️外部ティア(claudecode:sonnet): 最終成功から 15h")
    assert "外部ティア" in body
    assert importance == "medium"


def test_healthy_line_stays_low() -> None:
    _, body, importance = _heartbeat("外部ティア(claudecode:sonnet): 最終成功から 0h")
    assert "外部ティア" in body
    assert importance == "low"


def test_local_only_setup_says_nothing() -> None:
    """外部モデルを割り当てていない構成では監視対象が無い (常時警告にしない)。"""
    _, body, importance = _heartbeat(None)
    assert "外部ティア" not in body
    assert importance == "low"


def _repo_with(tmp_path: Path, rows: Sequence[tuple[int, str]]) -> RunHistoryRepository:
    """(何時間前, model) の版を入れた repo を作る。"""

    repo = RunHistoryRepository(db_path=tmp_path / "tier.db")
    base = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
    for n, (hours_ago, model) in enumerate(rows):
        item_id = f"ev-{n:04d}"
        at = base - timedelta(hours=hours_ago)
        repo.create_event_item(
            item_id=item_id,
            origin="live",
            first_reported_at=at,
            last_reported_at=at,
            importance="high",
        )
        repo.record_event_version(
            item_id=item_id,
            version=1,
            generated_at=at,
            model=model,
            prompt_version="eventnews-v6",
            headline="h",
            body_json="{}",
            new_facts_json="[]",
            verified_at=None,
            dropped_lines=0,
            repaired_ids=0,
        )
    return repo


def test_detects_a_long_run_of_fallbacks(tmp_path: Path) -> None:
    """外部で最後に成功してから、fallback だけで 15 時間ぶん生成が続いた状態。"""
    from unittest.mock import patch

    from src.ui.services.source_health import _build_external_tier_line

    repo = _repo_with(tmp_path, [(20, "claudecode:sonnet"), (5, "claudecode:sonnet→gemma4:31b")])
    with patch("src.tools.model_tiers.resolve_tier_model", return_value="claudecode:sonnet"):
        line = _build_external_tier_line(repo)
    assert line is not None and line.startswith("⚠️"), line


def test_healthy_when_the_latest_generation_used_the_external_arm(tmp_path: Path) -> None:
    from unittest.mock import patch

    from src.ui.services.source_health import _build_external_tier_line

    repo = _repo_with(tmp_path, [(20, "claudecode:sonnet"), (1, "claudecode:sonnet")])
    with patch("src.tools.model_tiers.resolve_tier_model", return_value="claudecode:sonnet"):
        line = _build_external_tier_line(repo)
    assert line is not None and not line.startswith("⚠️"), line


def test_sleeping_host_is_not_a_false_positive(tmp_path: Path) -> None:
    """Mac が眠って生成が丸ごと止まっている間は鳴らない。

    「最後の生成」と「最後の外部成功」の**開き**で見るので、両方が同じだけ古く
    なるだけ。2026-08-31 に停滞を経路障害と誤診した実例があるので明示的に固定する。
    """
    from unittest.mock import patch

    from src.ui.services.source_health import _build_external_tier_line

    repo = _repo_with(tmp_path, [(48, "claudecode:sonnet")])
    with patch("src.tools.model_tiers.resolve_tier_model", return_value="claudecode:sonnet"):
        line = _build_external_tier_line(repo)
    assert line is not None and not line.startswith("⚠️"), line


def test_local_tier_is_not_monitored(tmp_path: Path) -> None:
    from unittest.mock import patch

    from src.ui.services.source_health import _build_external_tier_line

    repo = _repo_with(tmp_path, [(1, "gemma4:31b")])
    with patch("src.tools.model_tiers.resolve_tier_model", return_value="gemma4:31b"):
        assert _build_external_tier_line(repo) is None
