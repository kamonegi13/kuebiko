"""detect_arms の manifest: inputs の meta を本文の関門に通る形で書く (11-09 の GPU-A2 の失敗)。"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS / "gpu_window"))

import detect_arms  # noqa: E402

from scripts.replay._manifest import assert_no_body  # noqa: E402

_LONG = "situation title = latest revision claim at window start (rejudge 2.3)"


def test_inputs_meta_replaces_deviation_texts_with_count_and_sha() -> None:
    meta = {"seed": 7, "frozen_at": "2026-11-08T00:00:00+00:00", "excluded": {"a": 1},
            "deviations": ["short", _LONG]}  # fmt: skip
    got = detect_arms.inputs_meta_for_manifest(meta)
    want_sha = hashlib.sha256(
        json.dumps(["short", _LONG], ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    assert got == {"seed": 7, "frozen_at": "2026-11-08T00:00:00+00:00", "excluded": {"a": 1},
                   "deviations_count": 2, "deviations_sha256": want_sha}  # fmt: skip
    assert_no_body(got)


def test_inputs_meta_without_deviations() -> None:
    got = detect_arms.inputs_meta_for_manifest({"seed": 1})
    assert got["deviations_count"] == 0
    assert got["frozen_at"] is None
    assert_no_body(got)
