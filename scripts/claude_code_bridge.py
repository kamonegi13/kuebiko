"""Claude Code サブスク bridge — ``claude -p`` を HTTP 化する。

外部 LLM を API クレジットでなく **Claude サブスクリプション (Pro/Max)** で使うための
サービス。コンテナ内の ``ClaudeCodeClient`` (src/tools/claude_code_client.py) が呼ぶ。

実行形態 (2026-07-24 に sidecar 化 — LaunchAgent 常駐の解消):
    - **推奨: sidecar コンテナ (claude-bridge)** — CLI は永続 volume に自己
      インストール/更新、認証は UI で貼付した CLAUDE_CODE_OAUTH_TOKEN (.env)。
      compose がライフサイクルを一元管理する。
    - 旧方式 (rollback 受け皿): ホストで ``uv run python scripts/claude_code_bridge.py``
      (LaunchAgent、ホストのログイン状態で認証)。

認証の解決順 (呼出ごと):
    1. ``BRIDGE_ENV_FILE`` (共有 .env) の CLAUDE_CODE_OAUTH_TOKEN — UI 保存が
       bridge 再起動なしで即時反映される
    2. プロセス環境変数の CLAUDE_CODE_OAUTH_TOKEN
    3. どちらも無ければ CLI 既定 (ホストのログイン状態 — 旧方式のみ有効)

セキュリティ:
    - 既定 bind は 127.0.0.1 (旧方式互換)。sidecar は --host 0.0.0.0 だが
      compose 内部ネットワークのみでホストにポート公開しない
    - model は許可リスト (sonnet/haiku/opus + claude-*) + 中華系 denylist で検証
    - 実行は引数リスト (shell=False)・空の作業ディレクトリ・--max-turns 1 で
      純粋なテキスト生成に限定する (ツール実行の余地を最小化)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

# スクリプト単体実行 (uv run python scripts/...) でも src を import できるよう repo root を追加
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.tools.llm_client import LLMForbiddenModelError, validate_model_name  # noqa: E402

DEFAULT_PORT = 8010
DEFAULT_TIMEOUT_SECONDS = 300.0
MAX_TIMEOUT_SECONDS = 1200.0
_STDERR_CLIP = 400

# claude CLI の探索場所 (PATH に無い環境向け。native installer は ~/.local/bin に置く)
_CLI_CANDIDATES = ("claude", str(Path.home() / ".local" / "bin" / "claude"))

# model 引数の許可形式: 短縮 alias か claude-* 系のみ (CLI への引数注入を防ぐ)
_MODEL_RE = re.compile(r"^(sonnet|haiku|opus|claude-[a-z0-9.-]+)$")


def version_tuple(v: str) -> tuple[int, ...]:
    """ "2.1.218 (Claude Code)" → (2, 1, 218)。数値化できない部分は打ち切り。"""
    out: list[int] = []
    for part in v.strip().split(" ")[0].split("."):
        if not part.isdigit():
            break
        out.append(int(part))
    return tuple(out)


# 最新版の照合 (npm registry — native installer と同一の版番号系列)。6h キャッシュ。
_LATEST_CACHE: dict[str, Any] = {"ts": 0.0, "version": ""}
_LATEST_TTL_SECONDS = 6 * 3600
_NPM_LATEST_URL = "https://registry.npmjs.org/@anthropic-ai/claude-code/latest"


async def fetch_latest_version() -> str:
    """公開レジストリから最新版番号を取得 (失敗は空 = 照合不能として扱う)。"""
    now = time.time()
    if _LATEST_CACHE["version"] and now - float(_LATEST_CACHE["ts"]) < _LATEST_TTL_SECONDS:
        return str(_LATEST_CACHE["version"])
    try:
        import httpx

        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(_NPM_LATEST_URL)
            r.raise_for_status()
            version = str(r.json().get("version") or "")
    except Exception:  # noqa: BLE001 — 照合不能でも bridge 本務は続行
        return str(_LATEST_CACHE["version"] or "")
    _LATEST_CACHE.update(ts=now, version=version)
    return version


def resolve_oauth_token() -> str:
    """UI 管理トークンを解決する (.env → プロセス env の順。無ければ空 = CLI 既定認証)。

    .env は app コンテナと共有のファイル (sidecar には read-only mount)。呼出ごとに
    読むため、UI での保存・削除が bridge 再起動なしで即時反映される。
    """
    env_file = os.environ.get("BRIDGE_ENV_FILE", "")
    if env_file and Path(env_file).exists():
        try:
            for line in Path(env_file).read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("CLAUDE_CODE_OAUTH_TOKEN="):
                    return line.split("=", 1)[1].strip()
        except OSError:
            pass  # 読取失敗は env fallback へ
    return os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()


def find_claude_cli() -> str | None:
    """claude CLI の実体 path を返す (見つからなければ None)。"""
    for cand in _CLI_CANDIDATES:
        is_path = os.sep in cand
        found = (cand if Path(cand).exists() else None) if is_path else shutil.which(cand)
        if found:
            return found
    return None


def validate_bridge_model(model: str) -> str:
    """model 引数を検証して返す。不正形式・中華系は ValueError。"""
    m = model.strip()
    if not _MODEL_RE.match(m):
        raise ValueError(f"model は sonnet/haiku/opus または claude-* のみ: {m!r}")
    try:
        validate_model_name(m)
    except LLMForbiddenModelError as e:
        raise ValueError(str(e)) from e
    return m


def parse_cli_output(stdout: str) -> dict[str, Any]:
    """``claude -p --output-format json`` の出力を整形する。

    出力例: {"type":"result","subtype":"success","is_error":false,"result":"...",
             "usage":{"input_tokens":N,"output_tokens":N,...},"total_cost_usd":F, ...}
    cost_usd はサブスクでは請求されないが「API 換算でいくら相当を使ったか」の指標になる。
    """
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise ValueError(f"CLI 出力が JSON ではありません: {stdout[:200]!r}") from e
    if not isinstance(data, dict):
        raise ValueError("CLI 出力が JSON object ではありません")
    if data.get("is_error"):
        raise ValueError(f"CLI がエラーを返しました: {str(data.get('result'))[:200]}")
    usage = data.get("usage") or {}
    return {
        "text": str(data.get("result") or ""),
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
        "cache_creation_tokens": int(usage.get("cache_creation_input_tokens") or 0),
        "cost_usd": float(data.get("total_cost_usd") or 0.0),
    }


# ---------- 使用状況トラッキング (レート窓の自己観測) ----------
#
# サブスクのレート残量は CLI からも API からも取れないため、bridge 自身が全 call を
# 記録して 5h 窓 (サブスク律速の単位) / 今日 / 7日 の消費量を可視化する。
# 記録は JSONL (data/claude_bridge_usage.jsonl) に永続化し再起動を跨いで保持する。

# sidecar では BRIDGE_USAGE_FILE=/data/... (永続 volume) を指定して再作成を跨いで保持する
USAGE_FILE = Path(os.environ.get("BRIDGE_USAGE_FILE", "data/claude_bridge_usage.jsonl"))
_USAGE_KEEP_SECONDS = 7 * 86400
_usage_records: list[dict[str, Any]] = []


def summarize_usage(records: list[dict[str, Any]], now_epoch: float) -> dict[str, Any]:
    """使用記録から 5h 窓 / 今日 (JST) / 7日 の集計を返す (純関数、テスト対象)。"""
    from zoneinfo import ZoneInfo

    def agg(since: float) -> dict[str, Any]:
        rs = [r for r in records if float(r.get("ts") or 0) >= since]
        return {
            "calls": len(rs),
            "input_tokens": sum(int(r.get("in") or 0) for r in rs),
            "output_tokens": sum(int(r.get("out") or 0) for r in rs),
            "cache_read_tokens": sum(int(r.get("cache_read") or 0) for r in rs),
            "cost_usd_equivalent": round(sum(float(r.get("cost") or 0.0) for r in rs), 4),
        }

    import datetime

    jst = ZoneInfo("Asia/Tokyo")
    now_dt = datetime.datetime.fromtimestamp(now_epoch, tz=jst)
    day_start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return {
        "window_5h": agg(now_epoch - 5 * 3600),
        "today": agg(day_start),
        "days7": agg(now_epoch - 7 * 86400),
        "last_call_at": (
            datetime.datetime.fromtimestamp(
                max(float(r["ts"]) for r in records), tz=jst
            ).isoformat()
            if records
            else None
        ),
    }


def _load_usage() -> None:
    """起動時に JSONL から直近 7 日分を読み込む (古い行はここで切り捨て再書込)。"""
    _usage_records.clear()
    if not USAGE_FILE.exists():
        return
    cutoff = time.time() - _USAGE_KEEP_SECONDS
    for line in USAGE_FILE.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and float(rec.get("ts") or 0) >= cutoff:
            _usage_records.append(rec)
    USAGE_FILE.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in _usage_records),
        encoding="utf-8",
    )


def _record_usage(model: str, parsed: dict[str, Any], duration: float) -> None:
    rec = {
        "ts": round(time.time(), 1),
        "model": model,
        "in": parsed.get("input_tokens", 0),
        "out": parsed.get("output_tokens", 0),
        "cache_read": parsed.get("cache_read_tokens", 0),
        "cost": parsed.get("cost_usd", 0.0),
        "sec": round(duration, 1),
    }
    _usage_records.append(rec)
    try:
        USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with USAGE_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 記録失敗は生成自体を妨げない


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1)
    model: str = "sonnet"
    system: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    # think=False で extended thinking を無効化 (MAX_THINKING_TOKENS=0)。
    # CLI 既定は thinking ON で、複雑タスクでは思考が 6-8k tok/call = 時間の ~8 割を
    # 占める (実測 71s→8s、2026-07-19)。None/True は CLI 既定に委譲。
    think: bool | None = None


app = FastAPI(title="claude-code-bridge")

# 純テキスト生成用の空作業ディレクトリ (リポジトリ文脈を CLI に見せない)
_WORKDIR = Path(tempfile.mkdtemp(prefix="claude-bridge-"))


@app.get("/health")
async def health() -> dict[str, Any]:
    """CLI の存在だけでなく実行可能性 (--version) まで確認する。

    npm 版の native binary 欠損など「バイナリはあるが動かない」状態を偽陽性にしない
    (UI はこの ok で claudecode 選択肢の表示可否を決める)。
    """
    cli = find_claude_cli()
    if cli is None:
        return {"ok": False, "claude_cli": "", "detail": "claude CLI が見つかりません"}
    try:
        proc = await asyncio.create_subprocess_exec(
            cli,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
    except (TimeoutError, OSError) as e:
        return {"ok": False, "claude_cli": cli, "detail": f"CLI 実行不可: {e}"}
    if proc.returncode != 0:
        detail = (stderr.decode("utf-8", "replace") or stdout.decode("utf-8", "replace"))[
            :_STDERR_CLIP
        ]
        return {
            "ok": False,
            "claude_cli": cli,
            "detail": f"CLI 異常 (exit {proc.returncode}): {detail}",
        }
    token = resolve_oauth_token()
    current = stdout.decode("utf-8", "replace").strip()
    latest = await fetch_latest_version()
    return {
        "ok": True,
        "claude_cli": cli,
        "version": current,
        # CLI 更新の照合 (npm registry と比較)。available=True で UI が更新ボタンを出す
        "update": {
            "current": current,
            "latest": latest,
            "available": bool(latest) and version_tuple(latest) > version_tuple(current),
        },
        # 認証モード: UI 管理トークン or CLI 既定 (ホストログイン)。UI 表示用
        "auth": {"token_set": bool(token), "mode": "token" if token else "cli-default"},
        # サブスク消費の自己観測 (5h 窓 = レート律速の単位 / 今日 / 7日)
        "usage": summarize_usage(_usage_records, time.time()),
    }


@app.post("/v1/update")
async def update_cli() -> dict[str, Any]:
    """claude CLI を自己更新する (volume 内バイナリの差し替え)。

    生成呼出は毎回 subprocess を新規 spawn するため、bridge 再起動なしで
    次の呼出から新版が使われる。UI の「更新」ボタンから呼ばれる。
    """
    cli = find_claude_cli()
    if cli is None:
        raise HTTPException(status_code=503, detail="claude CLI が見つかりません")
    before = ""
    try:
        proc = await asyncio.create_subprocess_exec(
            cli, "--version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        before = out.decode("utf-8", "replace").strip()
    except (TimeoutError, OSError):
        pass
    try:
        proc = await asyncio.create_subprocess_exec(
            cli, "update", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=180)
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail="claude update timeout (180s)") from e
    except OSError as e:
        raise HTTPException(status_code=502, detail=f"claude update 実行不可: {e}") from e
    if proc.returncode != 0:
        detail = (err.decode("utf-8", "replace") or out.decode("utf-8", "replace"))[:_STDERR_CLIP]
        raise HTTPException(status_code=502, detail=f"claude update 失敗: {detail}")
    after = ""
    try:
        proc = await asyncio.create_subprocess_exec(
            cli, "--version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        after = out.decode("utf-8", "replace").strip()
    except (TimeoutError, OSError):
        pass
    _LATEST_CACHE["ts"] = 0.0  # 次回 health で最新版を再照合
    return {"updated": before != after, "before": before, "after": after}


@app.post("/v1/generate")
async def generate(req: GenerateRequest) -> dict[str, Any]:
    cli = find_claude_cli()
    if cli is None:
        raise HTTPException(
            status_code=503,
            detail="claude CLI が見つかりません (https://claude.ai/install.sh でインストール)",
        )
    try:
        model = validate_bridge_model(req.model)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    timeout = max(10.0, min(MAX_TIMEOUT_SECONDS, req.timeout_seconds))

    # --tools "" = 全ツール無効 (純テキスト生成に限定)。これが無いと長大な構造化
    # プロンプトでモデルがツール使用を試み、--max-turns 1 と衝突して error_max_turns
    # で落ちる (2026-07-19 spotlight 実測)。
    args = [
        cli,
        "-p",
        "--output-format",
        "json",
        "--model",
        model,
        "--max-turns",
        "1",
        "--tools",
        "",
    ]
    if req.system:
        args += ["--append-system-prompt", req.system]

    env = dict(os.environ)
    if req.think is False:
        env["MAX_THINKING_TOKENS"] = "0"
    # UI 管理トークン (.env) を CLI 認証に注入 (未設定なら CLI 既定 = ホストログイン)
    token = resolve_oauth_token()
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token

    start = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(_WORKDIR),
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=req.prompt.encode("utf-8")), timeout=timeout
        )
    except TimeoutError as e:
        proc.kill()
        raise HTTPException(status_code=504, detail=f"claude CLI timeout ({timeout}s)") from e
    duration = time.monotonic() - start

    if proc.returncode != 0:
        # 認証切れ・レート上限等は stderr/stdout に載る。秘密は含まれない想定だが clip する
        detail = (stderr.decode("utf-8", "replace") or stdout.decode("utf-8", "replace"))[
            :_STDERR_CLIP
        ]
        raise HTTPException(
            status_code=502, detail=f"claude CLI 失敗 (exit {proc.returncode}): {detail}"
        )
    try:
        parsed = parse_cli_output(stdout.decode("utf-8", "replace"))
    except ValueError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    _record_usage(model, parsed, duration)
    return {**parsed, "duration_seconds": round(duration, 3), "model": model}


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Claude Code subscription bridge")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    # sidecar は 0.0.0.0 (compose 内部ネットワークのみ・ホスト非公開)。既定はホスト互換
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    _load_usage()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
