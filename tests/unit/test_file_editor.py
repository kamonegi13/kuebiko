"""src.ui.services.file_editor のテスト (Phase 1.5)。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.ui.services.file_editor import EditError, FileEditor, mask_env_value


def _init_git(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "test"], check=True)


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    (tmp_path / "prompts" / "briefing").mkdir(parents=True)
    (tmp_path / "config").mkdir()
    (tmp_path / "prompts" / "briefing/summarizer.j2").write_text(
        "hello {{ name }}", encoding="utf-8"
    )
    (tmp_path / "config" / "pipelines.yaml").write_text("pipelines: []\n", encoding="utf-8")
    (tmp_path / ".env").write_text("FOO=bar\n", encoding="utf-8")
    _init_git(tmp_path)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "init"],
        check=True,
    )
    return tmp_path


class TestListing:
    def test_list_prompts(self, project_root: Path) -> None:
        editor = FileEditor(project_root)
        prompts = editor.list_prompts()
        assert len(prompts) == 1
        assert prompts[0].name == "summarizer.j2"
        assert prompts[0].parent.name == "briefing"  # 区分サブディレクトリを rglob で拾う

    def test_list_yaml(self, project_root: Path) -> None:
        editor = FileEditor(project_root)
        yamls = editor.list_yaml_configs()
        assert len(yamls) == 1
        assert yamls[0].name == "pipelines.yaml"


class TestRead:
    def test_read_prompt(self, project_root: Path) -> None:
        editor = FileEditor(project_root)
        text = editor.read_file("prompts/briefing/summarizer.j2", kind="prompt")
        assert text == "hello {{ name }}"

    def test_read_yaml(self, project_root: Path) -> None:
        editor = FileEditor(project_root)
        text = editor.read_file("config/pipelines.yaml", kind="yaml")
        assert "pipelines" in text


class TestAllowlist:
    def test_outside_prompts_dir_rejected(self, project_root: Path) -> None:
        editor = FileEditor(project_root)
        with pytest.raises(EditError, match="許可されていません"):
            editor.read_file("config/pipelines.yaml", kind="prompt")

    def test_path_traversal_rejected(self, project_root: Path) -> None:
        editor = FileEditor(project_root)
        with pytest.raises(EditError):
            editor.read_file("../etc/passwd", kind="prompt")

    def test_wrong_extension_rejected_for_prompt(self, project_root: Path) -> None:
        editor = FileEditor(project_root)
        with pytest.raises(EditError):
            editor.read_file("prompts/file.txt", kind="prompt")

    def test_wrong_extension_rejected_for_yaml(self, project_root: Path) -> None:
        editor = FileEditor(project_root)
        with pytest.raises(EditError):
            editor.read_file("config/file.txt", kind="yaml")


class TestWriteWithBackupAndCommit:
    def test_write_creates_backup(self, project_root: Path) -> None:
        editor = FileEditor(project_root)
        editor.write_file(
            "prompts/briefing/summarizer.j2",
            "new content {{ name }}",
            kind="prompt",
            commit_message="test edit",
        )
        backup = project_root / "prompts" / "briefing/summarizer.j2.bak"
        assert backup.exists()
        assert "hello" in backup.read_text(encoding="utf-8")
        assert "new content" in (project_root / "prompts" / "briefing/summarizer.j2").read_text(
            encoding="utf-8",
        )

    def test_write_does_not_touch_git(self, project_root: Path) -> None:
        """アプリは編集で git にコミットしない (anti-pattern 廃止)。

        書き込みは行われるが、HEAD は不変・対象ファイルは未コミットの変更として残る。
        """
        head_before = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        editor = FileEditor(project_root)
        editor.write_file(
            "prompts/briefing/summarizer.j2",
            "edited content {{ name }}",
            kind="prompt",
            commit_message="ui edit summarizer",
        )
        head_after = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert head_after == head_before  # 新規コミットなし
        status = subprocess.run(
            [
                "git",
                "-C",
                str(project_root),
                "status",
                "--porcelain",
                "prompts/briefing/summarizer.j2",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert status.strip() != ""  # 未コミットの変更として残る

    def test_write_yaml_within_config(self, project_root: Path) -> None:
        editor = FileEditor(project_root)
        editor.write_file(
            "config/pipelines.yaml",
            "pipelines:\n  - name: test\n",
            kind="yaml",
            commit_message="yaml test",
        )
        text = (project_root / "config" / "pipelines.yaml").read_text(encoding="utf-8")
        assert "name: test" in text


class TestMaskHelper:
    def test_mask_env_value(self) -> None:
        assert mask_env_value("abcdefghij") == "abcd***"
        assert mask_env_value("ab") == "***"
        assert mask_env_value("") == ""
