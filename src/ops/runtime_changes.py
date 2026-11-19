"""実行時の構成変更の記録 (2026-11-20、11-19 監査 P1-5 の最小構成)。

s26r 切替の後に出力の分布がずれたのに、環境変数の旗とコードの変更は DB に版が残らず、
いつ何が変わったかを切り分けられなかった。full instance の起動のたびに:

- コードの指紋 (``src/`` と ``prompts/`` の中身の sha256)
- **秘密を含まない**旗の値 (名前が旗の形・値が短い語や数字のものだけ。鍵・URL・利用者名・
  ホスト名は名前と値の両方で落とす)

を集め、前回と違うときだけ 1 行書く。モデル割当・基準の版は既存の版履歴
(``app_config_versions``) にあるので写さない (SSoT を 2 つ持たない)。

消費者: 日次ハートビートの 1 行 (直近 24h の変更と、変わった旗の名前)。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository

_log = get_logger(__name__)

_FLAG_NAME = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")
_SECRET_NAME = re.compile(
    r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|WEBHOOK|AUTH|COOKIE|CREDENTIAL|DSN|URL|EMAIL|"
    r"USER|HOST|AUD|DOMAIN|ACCOUNT|PHONE)"
)
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9.:_\-]{1,40}$")
_FINGERPRINT_DIRS = ("src", "prompts")
_FINGERPRINT_SUFFIXES = (".py", ".j2")
HEARTBEAT_WINDOW_HOURS = 24
_MAX_NAMES = 8


def snapshot_flags(env: Mapping[str, str]) -> dict[str, str]:
    """秘密を含まない旗だけを名前順に返す (純粋関数)。"""
    out = {}
    for name in sorted(env):
        value = env[name]
        if not _FLAG_NAME.match(name) or _SECRET_NAME.search(name):
            continue
        if not _SAFE_VALUE.match(value) or "/" in value:
            continue
        out[name] = value
    return out


def code_fingerprint(root: Path) -> str:
    """``src/`` と ``prompts/`` の中身 (相対パス + 内容) の sha256。"""
    digest = hashlib.sha256()
    for top in _FINGERPRINT_DIRS:
        base = root / top
        if not base.is_dir():
            continue
        for path in sorted(p for p in base.rglob("*") if p.suffix in _FINGERPRINT_SUFFIXES):
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def _flags_sha(flags: Mapping[str, str]) -> str:
    raw = json.dumps(dict(flags), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _last_row(repo: RunHistoryRepository) -> dict[str, Any] | None:
    with repo._connect() as conn:  # noqa: SLF001 — ops の小さな表
        row = conn.execute(
            "SELECT code_sha, flags_sha, detail FROM runtime_change_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def _diff(old: Mapping[str, str], new: Mapping[str, str]) -> dict[str, list[str | None]]:
    names = sorted(set(old) | set(new))
    return {n: [old.get(n), new.get(n)] for n in names if old.get(n) != new.get(n)}


def record_runtime_change(
    repo: RunHistoryRepository,
    *,
    code_sha: str,
    env: Mapping[str, str],
    now: datetime | None = None,
) -> bool:
    """前回と違えば 1 行書いて True。同じなら書かずに False。"""
    flags = snapshot_flags(env)
    flags_sha = _flags_sha(flags)
    last = _last_row(repo)
    if last and last["code_sha"] == code_sha and last["flags_sha"] == flags_sha:
        return False
    prev_flags = json.loads(last["detail"]).get("flags", {}) if last else {}
    changed = _diff(prev_flags, flags) if last else {}
    code_changed = last is not None and last["code_sha"] != code_sha
    detail = {"flags": flags, "changed_flags": changed, "code_changed": code_changed}
    at = (now or datetime.now(UTC)).isoformat()
    with repo._connect() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO runtime_change_events (recorded_at, code_sha, flags_sha, detail)"
            " VALUES (?, ?, ?, ?)",
            (at, code_sha, flags_sha, json.dumps(detail, ensure_ascii=False)),
        )
    _log.info(
        "runtime_change_recorded",
        code_sha=code_sha,
        flags_changed=sorted(changed),
    )
    return True


def record_at_startup(repo: RunHistoryRepository, project_root: Path) -> None:
    """起動時に呼ぶ。失敗しても起動は止めない。"""
    try:
        record_runtime_change(repo, code_sha=code_fingerprint(project_root), env=os.environ)
    except Exception as e:  # noqa: BLE001 — 記録の失敗で起動を止めない
        _log.warning("runtime_change_record_failed", error_type=type(e).__name__)


def build_runtime_change_line(
    repo: RunHistoryRepository, *, now: datetime | None = None
) -> str | None:
    """ハートビートの 1 行。直近 24h の構成変更 (変わった旗の名前・コードの更新)。"""
    try:
        at = now or datetime.now(UTC)
        since = (at - timedelta(hours=HEARTBEAT_WINDOW_HOURS)).isoformat()
        with repo._connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT detail FROM runtime_change_events WHERE recorded_at >= ? ORDER BY id",
                (since,),
            ).fetchall()
        details = [json.loads(r["detail"]) for r in rows]
        changes = [d for d in details if d.get("changed_flags") or d.get("code_changed")]
        if not changes:
            return f"構成変更 ({HEARTBEAT_WINDOW_HOURS}h): なし"
        names = sorted({n for d in changes for n in d.get("changed_flags", {})})
        shown = ", ".join(names[:_MAX_NAMES]) + (" …" if len(names) > _MAX_NAMES else "")
        code = " (コード更新あり)" if any(d.get("code_changed") for d in changes) else ""
        flags = f" 旗: {shown}" if names else ""
        return f"構成変更 ({HEARTBEAT_WINDOW_HOURS}h): {len(changes)} 回{code}{flags}"
    except Exception as e:  # noqa: BLE001 — heartbeat 本体を止めない
        _log.warning("runtime_change_line_failed", error_type=type(e).__name__)
        return None


__all__ = [
    "build_runtime_change_line",
    "code_fingerprint",
    "record_at_startup",
    "record_runtime_change",
    "snapshot_flags",
]
