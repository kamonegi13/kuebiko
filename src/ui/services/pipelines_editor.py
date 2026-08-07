"""``config/pipelines.yaml`` を pipeline 単位で部分編集するサービス (Phase 3.1)。

UI フォームから schedule / triage / max_articles などのフィールドを更新する際に
yaml 全文を扱わずに該当 pipeline のフィールドだけ書き換える。

設計:
- yaml をロード → dict として in-memory で更新 → ``FileEditor.write_file`` で
  atomic write + ``.bak`` の経路を再利用 (git auto-commit は CLAUDE.md §12 で廃止済)
- ``ruamel.yaml`` ではなく標準 PyYAML を使い、コメントは保持しない
  (yaml 全体は ``/config`` の textarea から従来通り編集可能)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from src.config_loader import load_pipelines
from src.logging_config import get_logger
from src.ui.services.file_editor import FileEditor

_log = get_logger(__name__)

PIPELINES_YAML = "config/pipelines.yaml"


class PipelinesEditError(Exception):
    """pipelines.yaml 編集に失敗。"""


class PipelinesEditor:
    """pipelines.yaml の部分編集用ラッパ。"""

    def __init__(self, file_editor: FileEditor) -> None:
        self._editor = file_editor

    # ----- public -----

    def update_schedule(
        self,
        pipeline_name: str,
        *,
        enabled: bool,
        mode: str,  # "cron" | "interval"
        hour: int | None = None,
        minute: int | None = None,
        interval_minutes: int | None = None,
        day_of_week: str | None = None,
    ) -> None:
        """schedule フィールドを更新する (cron <-> interval 切替対応)。

        Phase 5T-J: cron モードで day_of_week (例: "sun", "mon-fri") 対応。
        空文字 or None は毎日扱い (yaml に書かない)。
        """
        if mode not in ("cron", "interval"):
            raise PipelinesEditError(f"未知の mode: {mode}")
        new_schedule: dict[str, Any] = {"enabled": bool(enabled)}
        if mode == "interval":
            if interval_minutes is None or interval_minutes < 1:
                raise PipelinesEditError("実行間隔 (分) は 1 以上を指定してください")
            new_schedule["interval_minutes"] = int(interval_minutes)
        else:  # cron
            if hour is None or minute is None:
                raise PipelinesEditError("時刻指定では 時・分 の入力が必要です")
            new_schedule["hour"] = int(hour)
            new_schedule["minute"] = int(minute)
            if day_of_week:
                # 簡易バリデーション: 32 字以内 + 許容文字のみ
                dow = day_of_week.strip().lower()
                if len(dow) > 32:
                    raise PipelinesEditError("曜日の指定は 32 字以内で入力してください")
                allowed = set("0123456789-,monthuewdfrisat ")
                if any(c not in allowed for c in dow):
                    raise PipelinesEditError(
                        "曜日は数字か曜日略号 (sun/mon/.../sat)、範囲の - 、区切りの , のみ使えます"
                    )
                new_schedule["day_of_week"] = dow
        self._update_pipeline_field(pipeline_name, "schedule", new_schedule)

    def update_max_articles(self, pipeline_name: str, max_articles: int) -> None:
        if not (1 <= max_articles <= 200):
            raise PipelinesEditError("1 回の取得上限は 1〜200 の範囲で指定してください")
        self._update_subfield(pipeline_name, "source", "max_articles", int(max_articles))

    def update_triage(
        self,
        pipeline_name: str,
        *,
        enabled: bool,
        keep_importance: list[str],
        max_keep: int,
    ) -> None:
        valid = {"high", "medium", "low"}
        keep = [v for v in keep_importance if v in valid]
        if not keep and enabled:
            raise PipelinesEditError("選別を有効にする場合は、残す重要度を 1 つ以上選んでください")
        if not (1 <= max_keep <= 200):
            raise PipelinesEditError("残す件数の上限は 1〜200 の範囲で指定してください")
        self._update_subfield(pipeline_name, "processor", "triage_enabled", bool(enabled))
        self._update_subfield(pipeline_name, "processor", "triage_keep_importance", keep)
        self._update_subfield(pipeline_name, "processor", "triage_max_keep", int(max_keep))

    def update_think(self, pipeline_name: str, *, think_enabled: bool) -> None:
        """thinking モードの有効/無効を更新する。"""
        self._update_subfield(
            pipeline_name,
            "processor",
            "think_enabled",
            bool(think_enabled),
        )

    def update_dedup_thresholds(
        self,
        pipeline_name: str,
        *,
        threshold_hard: float,
        threshold_cluster: float,
        window_hours_hard: int,
        window_hours_cluster: int,
    ) -> None:
        """意味的重複排除の 2 段階しきい値と各時間ウィンドウを更新する (Phase 5L-2)。"""
        if not (0.0 <= threshold_hard <= 1.0):
            raise PipelinesEditError("重複と判定する類似度は 0.0〜1.0 の範囲で指定してください")
        if not (0.0 <= threshold_cluster <= 1.0):
            raise PipelinesEditError(
                "同一話題としてまとめる類似度は 0.0〜1.0 の範囲で指定してください"
            )
        if threshold_cluster >= threshold_hard:
            raise PipelinesEditError(
                "同一話題としてまとめる類似度は、重複と判定する類似度より小さくしてください",
            )
        if not (0 <= window_hours_hard <= 8760):
            raise PipelinesEditError(
                "重複判定の対象期間 (時間) は 0〜8760 (1 年) の範囲で指定してください"
            )
        if not (0 <= window_hours_cluster <= 8760):
            raise PipelinesEditError(
                "話題まとめの対象期間 (時間) は 0〜8760 (1 年) の範囲で指定してください"
            )
        # 4 フィールドをまとめて更新 (atomic 書き込み)
        for key, value in (
            ("similarity_threshold_hard", float(threshold_hard)),
            ("similarity_threshold_cluster", float(threshold_cluster)),
            ("dedup_window_hours_hard", int(window_hours_hard)),
            ("dedup_window_hours_cluster", int(window_hours_cluster)),
        ):
            self._update_subfield(pipeline_name, "processor", key, value)

    # ----- internal -----

    def _load(self) -> dict[str, Any]:
        text = self._editor.read_file(PIPELINES_YAML, kind="yaml")
        data = yaml.safe_load(text) if text.strip() else {}
        if not isinstance(data, dict) or "pipelines" not in data:
            raise PipelinesEditError(
                "pipelines.yaml が { pipelines: [...] } の形式ではありません",
            )
        return data

    def _save(self, data: dict[str, Any], commit_message: str) -> None:
        text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        self._editor.write_file(
            PIPELINES_YAML,
            text,
            kind="yaml",
            commit_message=commit_message,
        )
        # 設定検証 (失敗すれば次回 load で気付くが、ここで早期検出)
        try:
            load_pipelines(Path(self._editor.env_path().parent) / PIPELINES_YAML)
        except Exception as e:  # noqa: BLE001
            raise PipelinesEditError(f"yaml 検証失敗: {e}") from e

    def set_cluster_scraper_enabled(
        self,
        pipeline_name: str,
        scraper_name: str,
        *,
        enabled: bool,
    ) -> None:
        """Phase 5T-E: cluster pipeline 内の個別 watcher を on/off する。

        pipelines.yaml の ``pipelines[pipeline_name].source.scrapers[].enabled``
        を書き換える。pipeline が web_scraper_cluster でない場合は例外。
        対象 scraper が見つからない場合も例外。
        """
        data = self._load()
        p = self._find_pipeline(data, pipeline_name)
        source = p.get("source")
        if not isinstance(source, dict) or source.get("type") != "web_scraper_cluster":
            raise PipelinesEditError(
                f"pipeline {pipeline_name!r} は web_scraper_cluster ではありません",
            )
        scrapers = source.get("scrapers")
        if not isinstance(scrapers, list):
            raise PipelinesEditError(
                f"pipeline {pipeline_name!r} の source.scrapers が list ではありません",
            )
        target_idx: int | None = None
        for i, entry in enumerate(scrapers):
            if isinstance(entry, dict) and entry.get("name") == scraper_name:
                target_idx = i
                break
        if target_idx is None:
            raise PipelinesEditError(
                f"pipeline {pipeline_name!r} に scraper {scraper_name!r} が見つかりません",
            )
        scrapers[target_idx]["enabled"] = bool(enabled)
        self._save(
            data,
            commit_message=(f"chore(ui): {scraper_name} → enabled={enabled} via /schedule"),
        )
        _log.info(
            "pipelines_yaml_scraper_toggled",
            pipeline=pipeline_name,
            scraper=scraper_name,
            enabled=enabled,
        )

    def _find_pipeline(
        self,
        data: dict[str, Any],
        pipeline_name: str,
    ) -> dict[str, Any]:
        for p in data.get("pipelines", []):
            if isinstance(p, dict) and p.get("name") == pipeline_name:
                return p
        raise PipelinesEditError(f"pipeline {pipeline_name!r} が見つかりません")

    def _update_pipeline_field(
        self,
        pipeline_name: str,
        field: str,
        value: Any,
    ) -> None:
        data = self._load()
        p = self._find_pipeline(data, pipeline_name)
        p[field] = value
        self._save(
            data,
            commit_message=f"chore(ui): update {pipeline_name} {field} via /schedule",
        )
        _log.info(
            "pipelines_yaml_field_updated",
            pipeline=pipeline_name,
            field=field,
        )

    def _update_subfield(
        self,
        pipeline_name: str,
        section: str,
        key: str,
        value: Any,
    ) -> None:
        data = self._load()
        p = self._find_pipeline(data, pipeline_name)
        sub = p.get(section)
        if not isinstance(sub, dict):
            raise PipelinesEditError(f"section {section!r} が dict ではありません")
        sub[key] = value
        self._save(
            data,
            commit_message=f"chore(ui): update {pipeline_name} {section}.{key} via /schedule",
        )
        _log.info(
            "pipelines_yaml_subfield_updated",
            pipeline=pipeline_name,
            section=section,
            key=key,
        )


def get_pipelines_editor(app_state: Any) -> PipelinesEditor:
    """app.state.file_editor を使って PipelinesEditor を組み立てる。"""
    file_editor: FileEditor = app_state.file_editor
    return PipelinesEditor(file_editor)
