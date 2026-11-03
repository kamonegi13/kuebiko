"""GPU-B の prereg の証明書 (ホストで git を見て書き、コンテナ側は写しの一致だけ確かめる)。

コンテナには .git も docs/ も無い (grok_link_arms.py と同じ事情)。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from detect_common import CODE_FILES, PREREG_NAME, file_shas, runtime_flags  # noqa: E402

TOOL_FILES = ("detect_common.py", "detect_attest.py", "detect_snapshot.py", "detect_arms.py")
ATTESTATION_NAME = "prereg_attestation.json"


def tool_shas() -> dict[str, str]:
    return file_shas(Path(__file__).resolve().parent, TOOL_FILES)


PROD_CONTAINER = "kuebiko"


def production_flags(container: str = PROD_CONTAINER) -> dict[str, str]:
    """本番コンテナの環境変数のうち、結果に効く旗 (ホストで docker inspect)。取れなければ中止。"""
    res = subprocess.run(  # noqa: S603 - 引数リスト・shell なし
        ["docker", "inspect", container, "--format", "{{json .Config.Env}}"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        raise SystemExit(f"本番コンテナ {container} の環境を読めない: {res.stderr.strip()[:200]}")
    env = dict(e.split("=", 1) for e in json.loads(res.stdout) if "=" in e)
    return runtime_flags(env)


def attest(repo_root: Path, out: Path) -> dict[str, Any]:
    """ホスト側: prereg のコミットと道具の未コミット変更が無いことを確かめて書く。"""
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from scripts.replay._gate import require_prereg

    records = [
        require_prereg(PREREG_NAME, repo_root, caller=repo_root / "scripts" / "gpu_window" / t)
        for t in TOOL_FILES
    ]
    rec = records[-1]
    payload = {
        "prereg_path": rec.path,
        "prereg_git_sha": rec.git_sha,
        "prereg_blob_sha256": rec.blob_sha256,
        "head_sha": rec.head_sha,
        "prereg_commit_time": rec.commit_time,
        "attested_at": rec.run_started_at,
        "prod_flags": production_flags(),
        "tools": tool_shas(),
        "code_files": file_shas(repo_root, CODE_FILES),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return payload


def check_attestation(path: Path, root: Path = Path(".")) -> dict[str, Any]:
    """コンテナ側: 道具と本番コードの写しが証明書と同じであること (古いイメージを弾く)。"""
    if not path.is_file():
        raise SystemExit(f"prereg の証明書が無い: {path} (ホストで --attest を先に)")
    cert: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if cert.get("tools") != tool_shas():
        raise SystemExit("道具の写しが証明書と違う (イメージが古い / 道具が変わった)。再 attest")
    if cert.get("code_files") != file_shas(root, CODE_FILES):
        raise SystemExit("本番コードの写しが証明書と違う (イメージが古い)。再ビルドして再 attest")
    return cert
