"""src.config_loader のテスト (Step 3)。

設定ファイル不在・スキーマ不正・任意項目のデフォルト・frozen セマンティクス
を網羅。エラーメッセージが「どのファイル / どのキーが原因か」を判別できる
ことを確認する。
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from src.config_loader import (
    AgentsConfig,
    ChannelRouting,
    PipelineConfig,
    load_agents,
    load_app_config,
    load_channel_routing,
    load_pipelines,
)

# --------- 環境分離フィクスチャ ---------

REQUIRED_ENV_KEYS: tuple[str, ...] = (
    "DISCORD_WEBHOOK_ALERT",
    "DISCORD_WEBHOOK_BRIEF",
    "DISCORD_WEBHOOK_WATCH",
    "DISCORD_WEBHOOK_OPS",
)
OPTIONAL_ENV_KEYS: tuple[str, ...] = (
    "IMAP_HOST",
    "IMAP_PORT",
    "IMAP_USER",
    "IMAP_PASSWORD",
    "OLLAMA_BASE_URL",
    "OLLAMA_MAIN_MODEL",
    "LOG_LEVEL",
    "TIMEZONE",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """テスト中は ``.env`` を読まず、関連する環境変数も全クリアする。"""
    monkeypatch.chdir(tmp_path)
    for key in REQUIRED_ENV_KEYS + OPTIONAL_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield tmp_path


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_WEBHOOK_ALERT", "https://discord.com/api/webhooks/1/a")
    monkeypatch.setenv("DISCORD_WEBHOOK_BRIEF", "https://discord.com/api/webhooks/2/b")
    monkeypatch.setenv("DISCORD_WEBHOOK_WATCH", "https://discord.com/api/webhooks/3/w")
    monkeypatch.setenv("DISCORD_WEBHOOK_OPS", "https://discord.com/api/webhooks/4/o")


def _write_yaml(path: Path, data: Any) -> None:
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


# --------- AppConfig ---------


class TestAppConfig:
    def test_loads_required_fields_from_env(
        self,
        clean_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_required_env(monkeypatch)
        cfg = load_app_config()
        assert cfg.discord_webhook_alert.endswith("/a")
        assert cfg.discord_webhook_brief.endswith("/b")

    def test_missing_webhook_yields_empty_string(
        self,
        clean_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """webhook 欠落は raise しない (各 ch は optional)。

        env 未設定なら空文字 (publisher 構築時に skip)。
        """
        _set_required_env(monkeypatch)
        monkeypatch.delenv("DISCORD_WEBHOOK_WATCH", raising=False)
        cfg = load_app_config()
        assert cfg.discord_webhooks["watch"] == ""

    def test_optional_fields_have_defaults(
        self,
        clean_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_required_env(monkeypatch)
        cfg = load_app_config()
        # モデル選択はティア方式 (model_tiers.py) に移行済で AppConfig にモデルスロットは無い。
        # .env に残るのは base_url のみ (bootstrap 既定は BUILTIN_MODEL_TIERS)。
        assert cfg.ollama_base_url == "http://localhost:11434"
        assert cfg.imap_host == "imap.gmail.com"
        assert cfg.imap_port == 993
        assert cfg.imap_user == ""
        assert cfg.log_level == "INFO"
        assert cfg.timezone == "Asia/Tokyo"

    def test_env_vars_override_defaults(
        self,
        clean_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_required_env(monkeypatch)
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        monkeypatch.setenv("IMAP_PORT", "143")
        cfg = load_app_config()
        assert cfg.ollama_base_url == "http://host.docker.internal:11434"
        assert cfg.log_level == "DEBUG"
        assert cfg.imap_port == 143

    def test_discord_webhooks_property_returns_full_mapping(
        self,
        clean_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_required_env(monkeypatch)
        cfg = load_app_config()
        webhooks = cfg.discord_webhooks
        assert set(webhooks.keys()) == {
            "alert",
            "brief",
            "watch",
            "ops",
            "japan_watch",
        }
        assert webhooks["alert"].endswith("/a")
        assert webhooks["brief"].endswith("/b")
        assert webhooks["watch"].endswith("/w")
        assert webhooks["ops"].endswith("/o")
        # japan_watch は env 未設定なので空
        assert webhooks["japan_watch"] == ""

    def test_app_config_is_frozen(
        self,
        clean_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_required_env(monkeypatch)
        cfg = load_app_config()
        with pytest.raises(ValidationError):
            cfg.log_level = "ERROR"

    def test_unknown_env_var_is_ignored(
        self,
        clean_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_required_env(monkeypatch)
        monkeypatch.setenv("UNRELATED_VAR", "should-not-error")
        load_app_config()  # extra="ignore" のため失敗しない


# --------- load_pipelines ---------

VALID_PIPELINE: dict[str, Any] = {
    "pipelines": [
        {
            "name": "direct-rss-fetch",
            "source": {
                "type": "rss",
                "max_articles": 20,
            },
            "processor": {
                "extract_method": "trafilatura",
                "extract_min_length": 200,
                "target_language": "ja",
            },
        },
    ],
}


class TestLoadPipelines:
    def test_loads_valid_yaml(self, tmp_path: Path) -> None:
        p = tmp_path / "pipelines.yaml"
        _write_yaml(p, VALID_PIPELINE)
        result = load_pipelines(p)
        assert len(result) == 1
        assert isinstance(result[0], PipelineConfig)
        assert result[0].name == "direct-rss-fetch"
        assert result[0].source.type == "rss"
        assert result[0].source.max_articles == 20
        assert result[0].processor.extract_method == "trafilatura"

    def test_loads_default_path_when_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "config").mkdir()
        _write_yaml(tmp_path / "config" / "pipelines.yaml", VALID_PIPELINE)
        result = load_pipelines()  # 引数なし → デフォルトパス
        assert len(result) == 1

    def test_file_not_found_raises_with_clear_message(self, tmp_path: Path) -> None:
        p = tmp_path / "missing.yaml"
        with pytest.raises(FileNotFoundError) as exc_info:
            load_pipelines(p)
        assert "見つかりません" in str(exc_info.value)
        assert str(p.resolve()) in str(exc_info.value)

    def test_invalid_yaml_raises_value_error(self, tmp_path: Path) -> None:
        p = tmp_path / "broken.yaml"
        p.write_text("{invalid: [unclosed", encoding="utf-8")
        with pytest.raises(ValueError, match="YAML パースに失敗"):
            load_pipelines(p)

    def test_missing_pipelines_key_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "no_key.yaml"
        _write_yaml(p, {"unrelated": "value"})
        with pytest.raises(ValueError, match="pipelines"):
            load_pipelines(p)

    def test_pipelines_must_be_list(self, tmp_path: Path) -> None:
        p = tmp_path / "wrong_type.yaml"
        _write_yaml(p, {"pipelines": {"not": "a list"}})
        with pytest.raises(ValueError, match="リスト"):
            load_pipelines(p)

    def test_schema_violation_raises_with_context(self, tmp_path: Path) -> None:
        p = tmp_path / "bad_schema.yaml"
        bad = copy.deepcopy(VALID_PIPELINE)
        bad["pipelines"][0]["source"]["type"] = "invalid_source"
        _write_yaml(p, bad)
        with pytest.raises(ValueError, match="スキーマが不正"):
            load_pipelines(p)

    def test_max_articles_out_of_range_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "out_of_range.yaml"
        bad = copy.deepcopy(VALID_PIPELINE)
        bad["pipelines"][0]["source"]["max_articles"] = 999
        _write_yaml(p, bad)
        with pytest.raises(ValueError, match="スキーマが不正"):
            load_pipelines(p)

    def test_unknown_field_is_forbidden(self, tmp_path: Path) -> None:
        p = tmp_path / "extra.yaml"
        bad = copy.deepcopy(VALID_PIPELINE)
        bad["pipelines"][0]["unknown_field"] = "x"
        _write_yaml(p, bad)
        with pytest.raises(ValueError, match="スキーマが不正"):
            load_pipelines(p)


class TestMonthlySynthesisPipeline:
    """段3 (真の月次): monthly を専用 cron (月末) に分離し weekly 便乗生成を停止。"""

    def test_schedule_parses_day_of_month(self, tmp_path: Path) -> None:
        cfg: dict[str, Any] = {
            "pipelines": [
                {
                    "name": "monthly-x",
                    "schedule": {"enabled": True, "hour": 20, "minute": 0, "day": "last"},
                    "source": {"type": "status_synthesis", "synthesis_periods": ["monthly"]},
                    "processor": {
                        "extract_method": "trafilatura",
                        "extract_min_length": 200,
                        "target_language": "ja",
                    },
                }
            ]
        }
        p = tmp_path / "pipelines.yaml"
        _write_yaml(p, cfg)
        result = load_pipelines(p)
        assert result[0].schedule is not None
        assert result[0].schedule.day == "last"
        assert list(result[0].source.synthesis_periods) == ["monthly"]

    def test_real_config_separates_monthly_from_weekly(self) -> None:
        """実 config: weekly は weekly のみ、monthly は専用 cron (案2: 1日未明) で生成。"""
        by_name = {p.name: p for p in load_pipelines(Path("config/pipelines.yaml"))}
        weekly = by_name["weekly-status-synthesis"]
        assert list(weekly.source.synthesis_periods) == ["weekly"]  # 便乗 monthly を停止
        monthly = by_name["monthly-status-synthesis"]
        assert monthly.schedule is not None
        assert monthly.schedule.day == "1"  # 案2: 1 日未明 (prev-period で前月総括)
        assert monthly.schedule.day_of_week is None  # 固定曜日でない
        assert list(monthly.source.synthesis_periods) == ["monthly"]

    def test_pipeline_config_is_frozen(self, tmp_path: Path) -> None:
        p = tmp_path / "pipelines.yaml"
        _write_yaml(p, VALID_PIPELINE)
        result = load_pipelines(p)
        with pytest.raises(ValidationError):
            result[0].name = "renamed"

    # ---- Phase 5P: similarity threshold の明示と default 維持 ----

    def test_explicit_similarity_thresholds_override_defaults(
        self,
        tmp_path: Path,
    ) -> None:
        """yaml に明示した threshold が PipelineConfig に反映されること。"""
        p = tmp_path / "explicit.yaml"
        cfg = copy.deepcopy(VALID_PIPELINE)
        cfg["pipelines"][0]["processor"].update(
            {
                "similarity_threshold_hard": 0.95,
                "similarity_threshold_cluster": 0.82,
                "dedup_window_hours_hard": 200,
                "dedup_window_hours_cluster": 36,
            },
        )
        _write_yaml(p, cfg)
        result = load_pipelines(p)
        assert result[0].processor.similarity_threshold_hard == pytest.approx(0.95)
        assert result[0].processor.similarity_threshold_cluster == pytest.approx(0.82)
        assert result[0].processor.dedup_window_hours_hard == 200
        assert result[0].processor.dedup_window_hours_cluster == 36

    def test_missing_thresholds_fall_back_to_pydantic_defaults(
        self,
        tmp_path: Path,
    ) -> None:
        """yaml 未指定時は pydantic default が使われ、後方互換が保たれる。"""
        p = tmp_path / "no_threshold.yaml"
        _write_yaml(p, VALID_PIPELINE)  # processor に threshold を含めない
        result = load_pipelines(p)
        # config_loader.py の既定値に追従 (本変更で値を変えない)
        assert result[0].processor.similarity_threshold_hard == pytest.approx(0.92)
        assert result[0].processor.similarity_threshold_cluster == pytest.approx(0.78)
        assert result[0].processor.dedup_window_hours_hard == 168
        assert result[0].processor.dedup_window_hours_cluster == 48


# --------- load_agents ---------

VALID_AGENTS: dict[str, dict[str, str]] = {
    role: {
        "role": f"{role}-role",
        "goal": "do something useful",
        "backstory": "a story",
        "llm_model": "gemma4:31b",
    }
    for role in ("collector", "curator", "analyst", "editor", "publisher")
}


class TestLoadAgents:
    def test_loads_valid_yaml(self, tmp_path: Path) -> None:
        p = tmp_path / "agents.yaml"
        _write_yaml(p, VALID_AGENTS)
        result = load_agents(p)
        assert isinstance(result, AgentsConfig)
        assert result.collector.role == "collector-role"
        assert result.publisher.llm_model == "gemma4:31b"

    def test_file_not_found_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="見つかりません"):
            load_agents(tmp_path / "missing.yaml")

    def test_invalid_yaml_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "broken.yaml"
        p.write_text("{invalid: [unclosed", encoding="utf-8")
        with pytest.raises(ValueError, match="YAML パースに失敗"):
            load_agents(p)

    def test_top_level_must_be_dict(self, tmp_path: Path) -> None:
        p = tmp_path / "list.yaml"
        _write_yaml(p, ["not", "a", "dict"])
        with pytest.raises(ValueError, match="dict"):
            load_agents(p)

    def test_missing_role_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "agents.yaml"
        partial = copy.deepcopy(VALID_AGENTS)
        del partial["analyst"]
        _write_yaml(p, partial)
        with pytest.raises(ValueError, match="スキーマが不正"):
            load_agents(p)

    def test_extra_role_is_forbidden(self, tmp_path: Path) -> None:
        p = tmp_path / "agents.yaml"
        bad = copy.deepcopy(VALID_AGENTS)
        bad["spy"] = {"role": "x", "goal": "y", "backstory": "z", "llm_model": "m"}
        _write_yaml(p, bad)
        with pytest.raises(ValueError, match="スキーマが不正"):
            load_agents(p)

    def test_empty_role_value_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "agents.yaml"
        bad = copy.deepcopy(VALID_AGENTS)
        bad["collector"]["role"] = ""
        _write_yaml(p, bad)
        with pytest.raises(ValueError, match="スキーマが不正"):
            load_agents(p)

    def test_agents_config_is_frozen(self, tmp_path: Path) -> None:
        p = tmp_path / "agents.yaml"
        _write_yaml(p, VALID_AGENTS)
        result = load_agents(p)
        with pytest.raises(ValidationError):
            result.collector = result.editor


# --------- 同梱スケルトン (config/*.yaml) のサニティ ---------


class TestSkeletonFiles:
    """リポジトリ同梱の config/*.yaml が実際にロードできることを確認する。"""

    def test_pipelines_yaml_skeleton_loads(self) -> None:
        # tests/unit/<file> → 親 (tests/unit) → 親 (tests) → 親 (repo root)
        repo_root = Path(__file__).resolve().parents[2]
        result = load_pipelines(repo_root / "config" / "pipelines.yaml")
        assert len(result) >= 1
        names = [p.name for p in result]
        assert "direct-rss-fetch" in names

    def test_agents_yaml_skeleton_loads(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        result = load_agents(repo_root / "config" / "agents.yaml")
        assert result.collector.llm_model
        assert result.publisher.role


# --------- Phase 5C: ChannelRouting ---------


class TestChannelRouting:
    """``channel_routing`` キーの読み込み + 既定値挙動。"""

    def test_default_when_section_missing(self, tmp_path: Path) -> None:
        p = tmp_path / "no_routing.yaml"
        p.write_text(
            "pipelines: []\n",
            encoding="utf-8",
        )
        routing = load_channel_routing(p)
        assert routing.importance_map == {
            "high": "alert",
            "medium": "brief",
            "low": "watch",
        }
        assert routing.system_notify_enabled is True

    def test_overrides_importance_map(self, tmp_path: Path) -> None:
        p = tmp_path / "with_routing.yaml"
        p.write_text(
            "channel_routing:\n"
            "  importance_map:\n"
            "    high: alert\n"
            "    medium: watch\n"
            "    low: watch\n"
            "  system_notify_enabled: false\n"
            "pipelines: []\n",
            encoding="utf-8",
        )
        routing = load_channel_routing(p)
        assert routing.importance_map["high"] == "alert"
        assert routing.importance_map["medium"] == "watch"
        assert routing.system_notify_enabled is False

    def test_invalid_channel_value_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.yaml"
        p.write_text(
            "channel_routing:\n  importance_map:\n    high: bogus_channel\npipelines: []\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="channel_routing"):
            load_channel_routing(p)

    def test_pipelines_skeleton_has_channel_routing(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        routing = load_channel_routing(repo_root / "config" / "pipelines.yaml")
        # Phase 5C: 既定マッピングを採用
        assert isinstance(routing, ChannelRouting)
        assert routing.importance_map["high"] == "alert"
        assert routing.importance_map["medium"] == "brief"
        assert routing.importance_map["low"] == "watch"


class TestPipelineScheduleOffset:
    """interval_offset_minutes (in-cycle race 回避の stagger) の検証。"""

    def test_offset_defaults_zero(self) -> None:
        from src.config_loader import PipelineSchedule

        s = PipelineSchedule(interval_minutes=60)
        assert s.interval_offset_minutes == 0

    def test_offset_parsed(self) -> None:
        from src.config_loader import PipelineSchedule

        s = PipelineSchedule(interval_minutes=60, interval_offset_minutes=5)
        assert s.interval_offset_minutes == 5

    def test_offset_range_validated(self) -> None:
        import pytest
        from pydantic import ValidationError

        from src.config_loader import PipelineSchedule

        with pytest.raises(ValidationError):
            PipelineSchedule(interval_minutes=60, interval_offset_minutes=60)


class TestMorningBriefPipeline:
    """段4(a): 朝レンダー統合 — morning-brief 1 本に統合、旧 2 本を廃止。"""

    def test_real_config_has_morning_brief_and_drops_old(self) -> None:
        by_name = {p.name: p for p in load_pipelines(Path("config/pipelines.yaml"))}
        mb = by_name["morning-brief"]
        assert mb.source.type == "morning_brief"
        assert mb.schedule is not None
        assert (mb.schedule.hour, mb.schedule.minute) == (6, 30)
        # 旧 morning 系 2 本は morning-brief に統合・廃止
        assert "pir-daily-focus" not in by_name
        assert "daily-status-synthesis-morning" not in by_name
        # 朝刊/夕刊: 旧 daily-status-synthesis-evening は evening-brief に改称 (type 朝夕共用)
        assert "daily-status-synthesis-evening" not in by_name
        eb = by_name["evening-brief"]
        assert eb.source.type == "morning_brief"  # 朝夕共用 type (orchestrator が slot 判定)
        assert eb.schedule is not None
        assert (eb.schedule.hour, eb.schedule.minute) == (19, 30)
