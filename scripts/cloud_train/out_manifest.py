#!/usr/bin/env python3
"""学習の成果の照合 (2026-11-11)。学習サーバ (pod_run.sh) と手元 (runpod_ctl.py fetch) が使う。

過去に学習後の成果の取り出しが何度か失敗した。手元の取り出しが「大きさだけ」で取得済みを
判定し、中身を確かめていなかったため、欠けた・壊れたファイルを見逃す危険があった。

- サーバ側: 成果 (/root/out) の全ファイルの sha256 と大きさを SHA256SUMS.json に書き、成果と
  一緒に S3 窓口経由で送る (DONE.json の前)。送った後に head_object で大きさを確かめ、
  食い違えば取り直して書き直す。
- 手元側: SHA256SUMS.json を先に取り、各ファイルを取り出した後に sha256 を照合する。
  食い違えば消して取り直し (3 回まで)、それでも合わなければ ``FetchVerifyError``。

標準ライブラリだけで動く (サーバ側は pip の boto3 のみ追加)。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

MANIFEST_NAME = "SHA256SUMS.json"
MANIFEST_VERSION = 1
MAX_ATTEMPTS = 3
#: 成果に含めないもの (途中保存で、正常終了では adapter に置き換わる)
EXCLUDE_PREFIXES = ("adapter-last/",)
#: 取り出しで読まないもの (学習データ・コードは手元に原本がある)
FETCH_SKIP_PREFIXES = ("data/", "code/", "out/adapter-last/")
_CHUNK = 8 * 1024 * 1024
#: safetensors のヘッダ JSON の長さの上限 (これを超えたら壊れた長さとみなす)
_MAX_HEADER_BYTES = 100 * 1024 * 1024


class FetchVerifyError(RuntimeError):
    """照合に通らないファイルがある (メッセージに該当ファイルを列挙する)。"""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(out_dir: Path) -> dict[str, Any]:
    """成果ディレクトリ配下の全ファイルの {相対パス: {sha256, size}}。"""
    files: dict[str, dict[str, Any]] = {}
    for path in sorted(p for p in out_dir.rglob("*") if p.is_file()):
        rel = path.relative_to(out_dir).as_posix()
        if rel == MANIFEST_NAME or rel.startswith(EXCLUDE_PREFIXES):
            continue
        files[rel] = {"sha256": sha256_file(path), "size": path.stat().st_size}
    return {"version": MANIFEST_VERSION, "files": files}


def write_manifest(out_dir: Path) -> dict[str, Any]:
    manifest = build_manifest(out_dir)
    (out_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest


def parse_manifest(raw: bytes) -> dict[str, dict[str, Any]]:
    data = json.loads(raw)
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        raise ValueError("SHA256SUMS の形が不正")
    return {str(k): {"sha256": str(v["sha256"]), "size": int(v["size"])} for k, v in files.items()}


def safetensors_header_error(path: Path) -> str | None:
    """safetensors のヘッダ (先頭 8 バイトの長さ + JSON) が読めなければ理由、読めれば None。"""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            head = f.read(8)
            if len(head) < 8:
                return "ヘッダの長さ欄が読めない"
            n = int.from_bytes(head, "little")
            if n <= 0 or n > _MAX_HEADER_BYTES or n + 8 > size:
                return f"ヘッダの長さが不正 ({n})"
            meta = json.loads(f.read(n))
    except (OSError, ValueError) as exc:
        return f"ヘッダが読めない ({type(exc).__name__})"
    return None if isinstance(meta, dict) else "ヘッダが JSON の辞書でない"


# ---------------------------------------------------------------- サーバ側


def upload_file_verified(
    c: Any,
    bucket: str,
    path: Path,
    key: str,
    config: Any = None,
    log: Callable[[str], None] = print,
) -> None:
    """送って head_object で大きさを確かめ、食い違えば送り直す (最大 MAX_ATTEMPTS 回)。"""
    expected = path.stat().st_size
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if config is not None:
            c.upload_file(str(path), bucket, key, Config=config)
        else:
            c.upload_file(str(path), bucket, key)
        got = c.head_object(Bucket=bucket, Key=key)["ContentLength"]
        if got == expected:
            return
        log(f"⚠ 書き出しの大きさが不一致 {key}: 期待 {expected} / 実際 {got} ({attempt} 回目)")
    raise RuntimeError(f"書き出しの大きさが合わない: {key}")


def upload_out(
    c: Any,
    bucket: str,
    out_dir: Path,
    run_id: str,
    config: Any = None,
    log: Callable[[str], None] = print,
) -> None:
    """成果 + SHA256SUMS を送り、最後に完了の印 (DONE.json) を送る。"""
    manifest = write_manifest(out_dir)
    done = out_dir / "DONE.json"
    names = [MANIFEST_NAME, *(r for r in manifest["files"] if r != "DONE.json")]
    for rel in names:
        upload_file_verified(c, bucket, out_dir / rel, f"runs/{run_id}/out/{rel}", config, log)
        log(f"書き出し {rel}")
    # 完了の印は最後に書く (手元の watch はこれを見て取り出しを始める)
    upload_file_verified(c, bucket, done, f"runs/{run_id}/DONE.json", None, log)


# ---------------------------------------------------------------- 手元側


def _list_keys(c: Any, bucket: str, prefix: str) -> list[tuple[str, int]]:
    keys: list[tuple[str, int]] = []
    for page in c.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        keys += [(o["Key"], o["Size"]) for o in page.get("Contents", [])]
    return keys


def _load_remote_manifest(
    c: Any, bucket: str, prefix: str, log: Callable[[str], None]
) -> dict[str, dict[str, Any]] | None:
    try:
        raw = c.get_object(Bucket=bucket, Key=f"{prefix}out/{MANIFEST_NAME}")["Body"].read()
    except Exception:  # noqa: BLE001  (無い = 古い run)
        return None
    try:
        return parse_manifest(raw)
    except (ValueError, KeyError, TypeError) as exc:
        raise FetchVerifyError(f"{MANIFEST_NAME} が壊れている: {exc}") from exc


def _verify_local(local: Path, expect: dict[str, Any]) -> str | None:
    """合えば None、合わなければ理由。"""
    if not local.is_file():
        return "ファイルが無い"
    size = local.stat().st_size
    if size != expect["size"]:
        return f"大きさ不一致 (期待 {expect['size']} / 実際 {size})"
    if sha256_file(local) != expect["sha256"]:
        return "sha256 不一致"
    if local.suffix == ".safetensors":
        return safetensors_header_error(local)
    return None


def fetch_run(
    c: Any,
    bucket: str,
    run_id: str,
    dest: Path,
    transfer: Any = None,
    log: Callable[[str], None] = print,
) -> None:
    """成果を dest へ取り出し、SHA256SUMS で全ファイルを照合する。合わなければ FetchVerifyError。"""
    prefix = f"runs/{run_id}/"
    keys = _list_keys(c, bucket, prefix)
    manifest = _load_remote_manifest(c, bucket, prefix, log)
    if manifest is None:
        log(f"⚠ {MANIFEST_NAME} が無い (古い run)。大きさだけで判定する (中身は照合されない)")
    present = {k[len(prefix) :] for k, _ in keys}
    failures: list[str] = []
    for rel in sorted(manifest or {}):
        if f"out/{rel}" not in present:
            failures.append(f"out/{rel}: 保存領域に無い")
    for key, size in keys:
        rel = key[len(prefix) :]
        if rel.startswith(FETCH_SKIP_PREFIXES) or rel == f"out/{MANIFEST_NAME}":
            continue
        expect = manifest.get(rel[len("out/") :]) if manifest and rel.startswith("out/") else None
        reason = _fetch_one(c, bucket, key, size, dest / rel, expect, transfer, log)
        if reason:
            failures.append(f"{rel}: {reason}")
    if failures:
        raise FetchVerifyError("照合に通らないファイル:\n  " + "\n  ".join(failures))
    if manifest is not None:
        (dest / "out").mkdir(parents=True, exist_ok=True)
        (dest / "out" / MANIFEST_NAME).write_text(json.dumps({"files": manifest}), encoding="utf-8")
        log(f"全ファイル照合 OK ({len(manifest)} 件、sha256)")
    else:
        log("取り出し完了 (sha256 の照合なし: 大きさのみ)")


def _fetch_one(
    c: Any,
    bucket: str,
    key: str,
    size: int,
    local: Path,
    expect: dict[str, Any] | None,
    transfer: Any,
    log: Callable[[str], None],
) -> str | None:
    """1 ファイルを取り出して照合。失敗の理由 (3 回取り直しても合わない場合) か None。"""
    if expect is None:
        if local.exists() and local.stat().st_size == size:
            return None  # 取得済み (manifest の無いファイルは大きさのみ)
        err = _download(c, bucket, key, size, local, transfer, log)
        if err is None and local.suffix == ".safetensors":
            err = safetensors_header_error(local)
        return err
    if _verify_local(local, expect) is None:
        return None  # 取得済み (sha256 まで一致)
    reason: str | None = "未取得"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        local.unlink(missing_ok=True)
        reason = _download(c, bucket, key, size, local, transfer, log)
        reason = reason or _verify_local(local, expect)
        if reason is None:
            return None
        log(f"⚠ 照合失敗 {key}: {reason} → 消して取り直し ({attempt}/{MAX_ATTEMPTS})")
    local.unlink(missing_ok=True)
    return reason


def _download(
    c: Any, bucket: str, key: str, size: int, local: Path, transfer: Any, log: Callable[[str], None]
) -> str | None:
    local.parent.mkdir(parents=True, exist_ok=True)
    log(f"取得 {key} ({size / 1e6:.0f}MB)")
    try:
        if transfer is not None:
            c.download_file(bucket, key, str(local), Config=transfer)
        else:
            c.download_file(bucket, key, str(local))
    except Exception as exc:  # noqa: BLE001
        return f"取得エラー ({type(exc).__name__})"
    return None


def _pod_main() -> int:
    """学習サーバ上の入口: python out_manifest.py pod-upload (環境変数は pod_run.sh と同じ)。"""
    import boto3  # type: ignore[import-not-found,unused-ignore]
    from boto3.s3.transfer import TransferConfig  # type: ignore[import-not-found,unused-ignore]

    dc = os.environ["DATACENTER"]
    c = boto3.client(
        "s3",
        endpoint_url=f"https://s3api-{dc.lower()}.runpod.io/",
        region_name=dc,
        aws_access_key_id=os.environ["S3_KEY"],
        aws_secret_access_key=os.environ["S3_SECRET"],
    )
    cfg = TransferConfig(max_concurrency=8, multipart_chunksize=64 * 1024 * 1024)
    upload_out(c, os.environ["VOLUME_ID"], Path("/root/out"), os.environ["RUN_ID"], cfg)
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["pod-upload"]:
        sys.exit(_pod_main())
    raise SystemExit("usage: out_manifest.py pod-upload")
