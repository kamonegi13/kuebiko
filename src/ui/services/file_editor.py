"""ファイル編集サービス (Phase 1.5)。

CLAUDE.md §12 のセキュリティポリシーに従う:
- 編集対象 allowlist: ``prompts/*.j2``, ``config/*.yaml``, ``.env`` のみ
- path traversal 防止: ``Path.resolve()`` してから許可ディレクトリ配下を確認
- 編集前 ``*.bak`` 自動生成
- 入力検証 (pydantic スキーマ) → atomic write (.bak のみ、git auto-commit は廃止済み)
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Literal

from src.logging_config import get_logger

_log = get_logger(__name__)

EditTarget = Literal["prompt", "yaml", "env"]


class EditError(Exception):
    """編集対象が allowlist に含まれない / path traversal 検知 / 書き込み失敗。"""


class FileEditor:
    """allowlist 制約 + atomic write + git auto-commit を統合した編集サービス。"""

    def __init__(self, project_root: Path | str) -> None:
        self._root = Path(project_root).resolve()
        self._prompts_dir = (self._root / "prompts").resolve()
        self._config_dir = (self._root / "config").resolve()
        self._env_path = (self._root / ".env").resolve()

    # ----- public API -----

    def list_prompts(self) -> list[Path]:
        if not self._prompts_dir.exists():
            return []
        return sorted(p for p in self._prompts_dir.glob("*.j2") if p.is_file())

    def list_yaml_configs(self) -> list[Path]:
        if not self._config_dir.exists():
            return []
        return sorted(p for p in self._config_dir.glob("*.yaml") if p.is_file())

    def env_path(self) -> Path:
        return self._env_path

    def read_file(self, path: Path | str, *, kind: EditTarget) -> str:
        target = self._resolve_and_check(path, kind)
        if not target.exists():
            return ""
        return target.read_text(encoding="utf-8")

    def write_file(
        self,
        path: Path | str,
        content: str,
        *,
        kind: EditTarget,
        commit_message: str | None = None,  # 後方互換: 受け取るが未使用 (git commit 廃止)
    ) -> Path:
        """検証済みの content を atomic rename で書き出す (.bak バックアップ付き)。

        **git commit は行わない**: 稼働中のアプリが開発リポジトリにコミットするのは
        anti-pattern のため廃止 (運用 config は DB store へ、履歴は DB で管理)。
        本メソッドは prompts(*.j2) / .env / 一部 legacy yaml の file 編集に残る。
        """
        target = self._resolve_and_check(path, kind)
        target.parent.mkdir(parents=True, exist_ok=True)

        # 1. バックアップ生成 (既存があれば)
        if target.exists():
            backup = target.with_suffix(target.suffix + ".bak")
            shutil.copy2(target, backup)
            _log.info(
                "file_editor_backup",
                path=str(target.relative_to(self._root)),
                backup=str(backup.relative_to(self._root)),
            )

        # 2. atomic rename: tmpfile → target
        # Docker bind-mount された個別ファイルでは EBUSY で失敗するため、
        # その場合のみ直接書込に fallback (env_editor と同方針)。
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(target.parent),
            delete=False,
            prefix=f".{target.name}.",
            suffix=".tmp",
        ) as tmpf:
            tmpf.write(content)
            tmpf.flush()
            os.fsync(tmpf.fileno())
            tmp_path = Path(tmpf.name)
        try:
            os.replace(tmp_path, target)
        except OSError as e:
            _log.warning(
                "file_editor_atomic_rename_failed_fallback_to_direct_write",
                path=str(target.relative_to(self._root)),
                error=str(e),
                errno=e.errno,
            )
            with contextlib.suppress(OSError):
                tmp_path.unlink()
            target.write_text(content, encoding="utf-8")

        _log.info(
            "file_editor_wrote",
            path=str(target.relative_to(self._root)),
            bytes=len(content),
        )

        # git commit は意図的に行わない (アプリが git を触る anti-pattern の廃止)。
        return target

    # ----- internal -----

    def _resolve_and_check(self, path: Path | str, kind: EditTarget) -> Path:
        target = (
            (self._root / Path(path)).resolve()
            if not Path(path).is_absolute()
            else Path(
                path,
            ).resolve()
        )

        if kind == "prompt":
            if target.parent != self._prompts_dir or target.suffix != ".j2":
                raise EditError(
                    f"保存先として許可されていません: {path} "
                    "(prompts 配下の .j2 ファイルのみ編集できます)",
                )
        elif kind == "yaml":
            if target.parent != self._config_dir or target.suffix != ".yaml":
                raise EditError(
                    f"保存先として許可されていません: {path} "
                    "(config 配下の .yaml ファイルのみ編集できます)",
                )
        elif kind == "env":
            if target != self._env_path:
                raise EditError(f".env 以外は編集できません: {path}")
        else:  # pragma: no cover (Literal で型保証)
            raise EditError(f"未知の編集対象種別です: {kind}")
        return target


def mask_env_value(value: str, *, prefix_len: int = 4, suffix: str = "***") -> str:
    """``.env`` の値を表示用にマスクする (logging_config の方針と同形式)。"""
    if not value:
        return ""
    if len(value) <= prefix_len:
        return suffix
    return value[:prefix_len] + suffix
