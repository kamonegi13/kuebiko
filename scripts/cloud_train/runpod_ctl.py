#!/usr/bin/env python3
"""RunPod での LoRA 学習を手元から操作する (2026-09-27)。data/cloud-venv/bin/python で動かす。

GPU の課金を学習の間だけに絞る:
- 学習データとコードは S3 API で保存領域へ先に置く (サーバ不要)
- サーバは学習が終わると自分で消える (pod_run.sh)。手元の watch も完了の印を見たら消す (二重の保証)
- 結果は S3 API で保存領域から直接取り出す (サーバ不要 = 計算の課金なし。途中で切れても再開できる)

    runpod_ctl.py upload <run_id> --data data/mlx/dataset_s20
    runpod_ctl.py launch <run_id> --gpu "NVIDIA H200" --max-hours 4 --train-args "--max-updates 30"
    runpod_ctl.py watch  <run_id>          # 完了まで待ち、サーバを確実に消し、結果を取り出す
    runpod_ctl.py status <run_id> / fetch <run_id> / volumes / pods / stop-all

.env に置く値 (値は表示しない): RUNPOD_API_KEY, RUNPOD_S3_ACCESS_KEY (user_…),
RUNPOD_S3_SECRET (rps_…), RUNPOD_VOLUME_ID, RUNPOD_DATACENTER (例 EU-RO-1)。
Gemma 4 は Apache 2.0・アクセス制限なしなので Hugging Face のトークンは不要。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
CODE = Path(__file__).resolve().parent
LOCAL_RUNS = REPO / "data" / "cloud-runs"
API = "https://rest.runpod.io/v1"
DEFAULT_IMAGE = "runpod/pytorch:2.8.0-py3.11-cuda12.8.1-cudnn-devel-ubuntu22.04"
_KEYS = (
    "RUNPOD_API_KEY",
    "RUNPOD_S3_ACCESS_KEY",
    "RUNPOD_S3_SECRET",
    "RUNPOD_VOLUME_ID",
    "RUNPOD_DATACENTER",
)


def env(required: tuple[str, ...] = _KEYS) -> dict[str, str]:
    vals: dict[str, str] = {}
    for line in (REPO / ".env").read_text(encoding="utf-8").splitlines():
        k, sep, v = line.partition("=")
        if sep and k.strip() in _KEYS:
            vals[k.strip()] = v.strip().strip('"').strip("'")
    missing = [k for k in required if not vals.get(k)]
    if missing:
        raise SystemExit(f".env に未設定: {', '.join(missing)}")
    return vals


def s3(e: dict[str, str]) -> Any:
    import boto3
    from botocore.config import Config

    dc = e["RUNPOD_DATACENTER"]
    return boto3.client(
        "s3",
        endpoint_url=f"https://s3api-{dc.lower()}.runpod.io/",
        region_name=dc,
        aws_access_key_id=e["RUNPOD_S3_ACCESS_KEY"],
        aws_secret_access_key=e["RUNPOD_S3_SECRET"],
        config=Config(signature_version="s3v4", retries={"max_attempts": 10}),
    )


class RunPodApiError(RuntimeError):
    """RunPod の REST API がエラーを返した (本文に理由がある)。"""


def api(e: dict[str, str], method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {e['RUNPOD_API_KEY']}",
            "Content-Type": "application/json",
            # Cloudflare が Python-urllib の既定の名乗りを 403 (error 1010) で弾く (2026-09-27 実測)
            "User-Agent": "kuebiko-runpod-ctl/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
    except urllib.error.HTTPError as exc:
        # 400 の理由 (どの項目が不正か) は本文にしか無い。キーは本文に含まれない
        detail = exc.read().decode(errors="replace")[:800]
        raise RunPodApiError(f"RunPod API {method} {path} → {exc.code}: {detail}") from exc
    return json.loads(raw) if raw else {}


def _exists(c: Any, bucket: str, key: str) -> bool:
    try:
        c.head_object(Bucket=bucket, Key=key)
        return True
    except Exception:  # noqa: BLE001
        return False


def cmd_upload(a: argparse.Namespace) -> None:
    e = env()
    c = s3(e)
    b = e["RUNPOD_VOLUME_ID"]
    for name in ("train.jsonl", "valid.jsonl"):
        src = a.data / name
        if src.exists():
            c.upload_file(str(src), b, f"runs/{a.run_id}/data/{name}")
            print(f"送信 {name} ({src.stat().st_size / 1e6:.1f}MB)")
    for name in ("train_lora.py", "pod_run.sh"):
        c.upload_file(str(CODE / name), b, f"runs/{a.run_id}/code/{name}")
    print("送信 コード")


def cmd_launch(a: argparse.Namespace) -> None:
    e = env()
    body = {
        "name": f"kuebiko-train-{a.run_id}"[:60],
        "imageName": a.image,
        "gpuTypeIds": [a.gpu],
        "gpuCount": a.gpu_count,
        "cloudType": "SECURE",
        "networkVolumeId": e["RUNPOD_VOLUME_ID"],
        # dataCenterIds は指定しない: 保存領域のデータセンターに作られる。API の仕様書の列挙が古く、
        # US-NE-1 等を 400 で弾く (2026-09-27 実測)
        "volumeMountPath": "/workspace",
        "containerDiskInGb": a.disk_gb,
        "env": {
            "RUN_ID": a.run_id,
            "TRAIN_ARGS": a.train_args,
            "MAX_HOURS": str(a.max_hours),
        },
        "dockerStartCmd": ["bash", "-c", f"bash /workspace/runs/{a.run_id}/code/pod_run.sh"],
    }
    pod = api(e, "POST", "/pods", body)
    LOCAL_RUNS.joinpath(a.run_id).mkdir(parents=True, exist_ok=True)
    (LOCAL_RUNS / a.run_id / "pod.json").write_text(
        json.dumps({"id": pod.get("id"), "launched": time.time()})
    )
    print(f"起動 pod={pod.get('id')} GPU={a.gpu}×{a.gpu_count} 上限 {a.max_hours} 時間")


def _pod_id(run_id: str) -> str | None:
    p = LOCAL_RUNS / run_id / "pod.json"
    return json.loads(p.read_text()).get("id") if p.exists() else None


def _delete_pod(e: dict[str, str], pod_id: str | None) -> None:
    if not pod_id:
        return
    try:
        api(e, "DELETE", f"/pods/{pod_id}")
        print(f"サーバを削除 pod={pod_id}")
    except Exception as exc:  # noqa: BLE001 — 既に自分で消えていれば 404
        print(f"削除の応答: {type(exc).__name__} (自分で消えていれば問題なし)")


def cmd_status(a: argparse.Namespace) -> str:
    e = env()
    c = s3(e)
    b = e["RUNPOD_VOLUME_ID"]
    state = (
        "done"
        if _exists(c, b, f"runs/{a.run_id}/DONE.json")
        else ("failed" if _exists(c, b, f"runs/{a.run_id}/FAILED.txt") else "running")
    )
    print(f"run={a.run_id} 状態={state}")
    try:
        log = (
            c.get_object(Bucket=b, Key=f"runs/{a.run_id}/pod.log")["Body"]
            .read()
            .decode(errors="replace")
        )
        print("\n".join(log.splitlines()[-8:]))
    except Exception:  # noqa: BLE001
        print("(ログはまだ無い)")
    return state


def cmd_fetch(a: argparse.Namespace) -> None:
    e = env()
    c = s3(e)
    b = e["RUNPOD_VOLUME_ID"]
    dest = LOCAL_RUNS / a.run_id
    prefix = f"runs/{a.run_id}/"
    keys = []
    for page in c.get_paginator("list_objects_v2").paginate(Bucket=b, Prefix=prefix):
        keys += [(o["Key"], o["Size"]) for o in page.get("Contents", [])]
    for key, size in keys:
        rel = key[len(prefix) :]
        if rel.startswith(("data/", "code/", "out/adapter-last/")):
            continue
        local = dest / rel
        if local.exists() and local.stat().st_size == size:
            continue  # 取得済み (途中で切れたら、残りだけを取り直す)
        local.parent.mkdir(parents=True, exist_ok=True)
        print(f"取得 {rel} ({size / 1e6:.0f}MB)", flush=True)
        c.download_file(b, key, str(local))
    print(f"取り出し完了 → {dest}")


def cmd_watch(a: argparse.Namespace) -> None:
    e = env()
    while True:
        state = cmd_status(a)
        if state != "running":
            _delete_pod(e, _pod_id(a.run_id))  # 自分で消えていなくても確実に止める
            if state == "done":
                cmd_fetch(a)
            return
        time.sleep(a.interval)


def cmd_volumes(_: argparse.Namespace) -> None:
    """保存領域の一覧 (ID・データセンター・容量)。.env には RUNPOD_API_KEY だけあればよい。"""
    for v in api(env(("RUNPOD_API_KEY",)), "GET", "/networkvolumes") or []:
        dc, size = v.get("dataCenterId"), v.get("size")
        print(f"ID={v.get('id')}  データセンター={dc}  {size}GB  {v.get('name')}")


def cmd_pods(_: argparse.Namespace) -> None:
    for p in api(env(), "GET", "/pods") or []:
        print(p.get("id"), p.get("name"), p.get("desiredStatus"), p.get("costPerHr"))


def cmd_stop_all(_: argparse.Namespace) -> None:
    e = env()
    for p in api(e, "GET", "/pods") or []:
        if str(p.get("name", "")).startswith("kuebiko-train-"):
            _delete_pod(e, p.get("id"))


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("upload")
    p.add_argument("run_id")
    p.add_argument("--data", type=Path, required=True)
    p = sub.add_parser("launch")
    p.add_argument("run_id")
    p.add_argument("--gpu", default="NVIDIA H200")
    p.add_argument("--gpu-count", type=int, default=1)
    p.add_argument("--max-hours", type=float, default=4)
    p.add_argument("--disk-gb", type=int, default=150)
    p.add_argument("--image", default=DEFAULT_IMAGE)
    p.add_argument("--train-args", default="")
    for name in ("status", "fetch", "watch"):
        p = sub.add_parser(name)
        p.add_argument("run_id")
        p.add_argument("--interval", type=int, default=120)
    sub.add_parser("volumes")
    sub.add_parser("pods")
    sub.add_parser("stop-all")
    a = ap.parse_args()
    {
        "upload": cmd_upload,
        "launch": cmd_launch,
        "status": cmd_status,
        "fetch": cmd_fetch,
        "watch": cmd_watch,
        "volumes": cmd_volumes,
        "pods": cmd_pods,
        "stop-all": cmd_stop_all,
    }[a.cmd](a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
