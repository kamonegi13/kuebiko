"""較正格子の恒久資産エクスポート (§13-3 対処、2026-08-22)。

H3 の恒久資産 = ラベル + 較正原資 + gold set のうち、gold set は gitignore の
ローカルファイル、ラベル群は DB のみ (pg_dump 頼み) だった。日次で両方を
schema 非依存の JSONL に書き出し、pg_dump と同じ ``data/backups/`` に置いて
DB バックアップと同水準の保護に揃える (rotation 付き)。

外部媒体への退避は運用課題として残る (§13 LOW) — 本 module の責務は
「単一テーブル/単一ファイル障害で資産が消えない」ことまで。
"""

from __future__ import annotations

import json
import shutil
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.logging_config import get_logger

_log = get_logger(__name__)

_BACKUP_DIR = Path("data/backups")
_GOLDSET_PATH = Path("data/eval/goldset.jsonl")
_PREFIX = "tuning_assets_"
_KEEP = 8  # 世代数 (日次 → 約 1 週間強)


def export_tuning_assets(
    repo: Any,
    *,
    backup_dir: Path = _BACKUP_DIR,
    goldset_path: Path = _GOLDSET_PATH,
    now: datetime | None = None,
    keep: int = _KEEP,
) -> Path | None:
    """ラベル・裁定・評価 + gold set を 1 つの tar.gz に書き出す。失敗は None (fail-open)。"""
    try:
        base = now or datetime.now(UTC)
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = base.strftime("%Y%m%d")
        out_path = backup_dir / f"{_PREFIX}{stamp}.tar.gz"
        work = backup_dir / f".{_PREFIX}work"
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)

        def _dump(name: str, rows: list[dict[str, Any]]) -> None:
            with (work / name).open("w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

        _dump("tuning_labels.jsonl", repo.export_tuning_labels())
        _dump("panel_verdicts.jsonl", repo.export_panel_verdicts())
        _dump("panel_resolutions.jsonl", repo.list_recent_resolutions(limit=100_000))
        _dump("tuning_evals.jsonl", repo.list_tuning_evals(limit=100_000))
        if goldset_path.exists():
            shutil.copy2(goldset_path, work / "goldset.jsonl")

        with tarfile.open(out_path, "w:gz") as tar:
            for p in sorted(work.iterdir()):
                tar.add(p, arcname=p.name)
        shutil.rmtree(work)

        # rotation (古い順に削除)
        exports = sorted(backup_dir.glob(f"{_PREFIX}*.tar.gz"))
        for old in exports[:-keep]:
            old.unlink(missing_ok=True)

        _log.info("tuning_assets_exported", path=str(out_path), kept=min(len(exports), keep))
        return out_path
    except Exception as e:  # noqa: BLE001 — 資産退避の失敗で maintenance を止めない
        _log.error("tuning_assets_export_failed", error=f"{type(e).__name__}: {e}")
        return None
