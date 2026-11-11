"""学習成果の照合 (out_manifest): 取り出しの sha256 照合と、サーバ側の書き出しの大きさ確認。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.cloud_train import out_manifest as om

BUCKET = "vol"
RUN = "r1"
GOOD_ST = (2).to_bytes(8, "little") + b"{}" + b"payload"


class FakeS3:
    """S3 窓口の偽物。corrupt_downloads 回だけ取得を壊す / short_uploads 回だけ送りを欠けさせる。"""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = dict(objects)
        self.downloads: list[str] = []
        self.corrupt_downloads: dict[str, int] = {}
        self.short_uploads: dict[str, int] = {}
        self.uploads: list[str] = []

    def get_paginator(self, _: str) -> Any:
        outer = self

        class _P:
            def paginate(self, Bucket: str, Prefix: str) -> list[dict[str, Any]]:  # noqa: N803
                return [
                    {
                        "Contents": [
                            {"Key": k, "Size": len(v)}
                            for k, v in outer.objects.items()
                            if k.startswith(Prefix)
                        ]
                    }
                ]

        return _P()

    def get_object(self, Bucket: str, Key: str) -> Any:  # noqa: N803
        if Key not in self.objects:
            raise KeyError(Key)
        data = self.objects[Key]

        class _B:
            def read(self) -> bytes:
                return data

        return {"Body": _B()}

    def download_file(self, bucket: str, key: str, filename: str, Config: Any = None) -> None:  # noqa: N803
        self.downloads.append(key)
        data = self.objects[key]
        if self.corrupt_downloads.get(key, 0) > 0:
            self.corrupt_downloads[key] -= 1
            data = b"X" + data[1:]
        Path(filename).write_bytes(data)

    def upload_file(self, filename: str, bucket: str, key: str, Config: Any = None) -> None:  # noqa: N803
        self.uploads.append(key)
        data = Path(filename).read_bytes()
        if self.short_uploads.get(key, 0) > 0:
            self.short_uploads[key] -= 1
            data = data[:-1]
        self.objects[key] = data

    def head_object(self, Bucket: str, Key: str) -> dict[str, int]:  # noqa: N803
        return {"ContentLength": len(self.objects[Key])}


def _manifest_bytes(files: dict[str, bytes]) -> bytes:
    return json.dumps(
        {
            "version": 1,
            "files": {
                k: {"sha256": hashlib.sha256(v).hexdigest(), "size": len(v)}
                for k, v in files.items()
            },
        }
    ).encode()


def _remote(files: dict[str, bytes], with_manifest: bool = True) -> FakeS3:
    objs = {f"runs/{RUN}/out/{k}": v for k, v in files.items()}
    if with_manifest:
        objs[f"runs/{RUN}/out/{om.MANIFEST_NAME}"] = _manifest_bytes(files)
    return FakeS3(objs)


FILES = {"adapter/model.safetensors": GOOD_ST, "metrics.jsonl": b"{}\n"}


def test_fetch_passes_when_all_hashes_match(tmp_path: Path) -> None:
    c = _remote(FILES)
    logs: list[str] = []
    om.fetch_run(c, BUCKET, RUN, tmp_path, log=logs.append)
    assert (tmp_path / "out/metrics.jsonl").read_bytes() == b"{}\n"
    assert any("全ファイル照合 OK" in m for m in logs)


def test_fetch_refetches_when_hash_mismatches_once(tmp_path: Path) -> None:
    c = _remote(FILES)
    c.corrupt_downloads[f"runs/{RUN}/out/metrics.jsonl"] = 1
    om.fetch_run(c, BUCKET, RUN, tmp_path, log=lambda _: None)
    assert c.downloads.count(f"runs/{RUN}/out/metrics.jsonl") == 2
    assert (tmp_path / "out/metrics.jsonl").read_bytes() == b"{}\n"


def test_fetch_fails_after_three_attempts_and_names_file(tmp_path: Path) -> None:
    c = _remote(FILES)
    c.corrupt_downloads[f"runs/{RUN}/out/metrics.jsonl"] = 99
    with pytest.raises(om.FetchVerifyError, match="out/metrics.jsonl"):
        om.fetch_run(c, BUCKET, RUN, tmp_path, log=lambda _: None)
    assert c.downloads.count(f"runs/{RUN}/out/metrics.jsonl") == om.MAX_ATTEMPTS
    assert not (tmp_path / "out/metrics.jsonl").exists()


def test_existing_file_with_same_size_but_wrong_content_is_refetched(tmp_path: Path) -> None:
    c = _remote(FILES)
    local = tmp_path / "out/metrics.jsonl"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"X}\n")  # 大きさは同じ・中身が違う
    om.fetch_run(c, BUCKET, RUN, tmp_path, log=lambda _: None)
    assert local.read_bytes() == b"{}\n"


def test_existing_correct_file_is_not_downloaded_again(tmp_path: Path) -> None:
    c = _remote(FILES)
    om.fetch_run(c, BUCKET, RUN, tmp_path, log=lambda _: None)
    c.downloads.clear()
    om.fetch_run(c, BUCKET, RUN, tmp_path, log=lambda _: None)
    assert c.downloads == []


def test_without_manifest_falls_back_to_size_and_warns(tmp_path: Path) -> None:
    c = _remote(FILES, with_manifest=False)
    logs: list[str] = []
    om.fetch_run(c, BUCKET, RUN, tmp_path, log=logs.append)
    assert any("SHA256SUMS.json が無い" in m for m in logs)
    assert (tmp_path / "out/metrics.jsonl").exists()


def test_manifest_file_missing_on_remote_fails(tmp_path: Path) -> None:
    c = _remote(FILES)
    del c.objects[f"runs/{RUN}/out/metrics.jsonl"]
    with pytest.raises(om.FetchVerifyError, match="保存領域に無い"):
        om.fetch_run(c, BUCKET, RUN, tmp_path, log=lambda _: None)


def test_bad_safetensors_header_is_rejected(tmp_path: Path) -> None:
    bad = b"\xff" * 8 + b"zzzz"  # ヘッダの長さが巨大
    c = _remote({"adapter/model.safetensors": bad})
    with pytest.raises(om.FetchVerifyError, match="ヘッダ"):
        om.fetch_run(c, BUCKET, RUN, tmp_path, log=lambda _: None)


def test_adapter_last_is_skipped(tmp_path: Path) -> None:
    c = _remote(FILES)
    c.objects[f"runs/{RUN}/out/adapter-last/big.safetensors"] = b"x"
    om.fetch_run(c, BUCKET, RUN, tmp_path, log=lambda _: None)
    assert not (tmp_path / "out/adapter-last").exists()


def test_build_manifest_excludes_adapter_last_and_self(tmp_path: Path) -> None:
    (tmp_path / "adapter-last").mkdir()
    (tmp_path / "adapter-last/a").write_bytes(b"1")
    (tmp_path / "keep.txt").write_bytes(b"abc")
    m = om.write_manifest(tmp_path)
    assert list(m["files"]) == ["keep.txt"]
    assert m["files"]["keep.txt"]["sha256"] == hashlib.sha256(b"abc").hexdigest()
    assert list(om.build_manifest(tmp_path)["files"]) == ["keep.txt"]


def test_upload_out_writes_manifest_before_done_and_retries_short_upload(tmp_path: Path) -> None:
    (tmp_path / "adapter").mkdir()
    (tmp_path / "adapter/model.safetensors").write_bytes(GOOD_ST)
    (tmp_path / "DONE.json").write_text("{}")
    c = FakeS3({})
    c.short_uploads[f"runs/{RUN}/out/adapter/model.safetensors"] = 1
    om.upload_out(c, BUCKET, tmp_path, RUN, log=lambda _: None)
    assert c.uploads[-1] == f"runs/{RUN}/DONE.json"
    assert c.uploads.index(f"runs/{RUN}/out/{om.MANIFEST_NAME}") < len(c.uploads) - 1
    assert c.uploads.count(f"runs/{RUN}/out/adapter/model.safetensors") == 2
    assert c.objects[f"runs/{RUN}/out/adapter/model.safetensors"] == GOOD_ST


def test_upload_fails_when_size_never_matches(tmp_path: Path) -> None:
    f = tmp_path / "x"
    f.write_bytes(b"abc")
    c = FakeS3({})
    c.short_uploads["k"] = 99
    with pytest.raises(RuntimeError, match="大きさが合わない"):
        om.upload_file_verified(c, BUCKET, f, "k", log=lambda _: None)
