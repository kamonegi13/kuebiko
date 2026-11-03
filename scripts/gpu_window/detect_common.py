"""GPU-B (DETECT-JUDGE) の共通部品。仕様は docs/replay_prereg/detect_decompose.md (変更しない)。

純粋部分 (窓の列挙・抽出・腕の定義・順序・書き込みの封) と、凍結した入力から本番の部品へ
渡すための読み取り専用の代役 (``FrozenRepo``)。標準ライブラリ + numpy。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

PREREG_NAME = "detect_decompose"
SEED = 20261104
RANGE_START = datetime(2026, 10, 27, tzinfo=UTC)
RANGE_END = datetime(2026, 11, 4, tzinfo=UTC)
BIN_HOURS = 6
MAX_PER_DAY = 3
N_WINDOWS = 20
RUNS_PER_ARM = 3
MIN_CANDIDATES = 5  # §2.1 (1) 候補が 5 件未満の窓は除外
DETECT_MODEL_SHA256 = "9a0a644f0e8b6a52f3440da317dc06c59de8c0555b2a87996ed2869c075a8ff0"
DETECT_MODEL_PATH = Path("config/models/detect_model.json")
OUT_DIR = Path("data/eval/detect_decompose")
INPUTS_NAME = "inputs.json"
INPUTS_SHA_NAME = "inputs.sha256.json"
BODY_PREFIX_CHARS = 1000  # claim の照合に使う本文冒頭 (detect_decompose.BODY_PREFIX_CHARS と同値)
#: 本番コードのうち結果に効くファイル (manifest に sha256 を残す。claim_grounding を含む)
CODE_FILES = (
    "src/assessment/detect_decompose.py",
    "src/assessment/detect_step.py",
    "src/assessment/claim_grounding.py",
    "src/assessment/claim_bundle.py",
    "src/synthesis/grounded/detect_ml.py",
    "src/synthesis/grounded/incremental.py",
    "prompts/synthesis/detect_new.j2",
    "prompts/synthesis/detect_cluster_claim.j2",
    "prompts/synthesis/detect_ml_select.j2",
)
SIM_STEPS = ("synthesis_detect", "detect_claim_write")


class WriteGuardError(RuntimeError):
    """本番 DB への書き込みを許す状態を検出した。"""


@dataclass(frozen=True)
class Arm:
    name: str
    model: str
    decompose_mode: str  # DETECT_DECOMPOSE の値 ('shadow' | '1')
    expected_variant: str  # 採用する開設の variant (§5.1)


CURRENT_ARM = Arm("current", "kuebiko-sft:s23i", "shadow", "current")
SHIP_ARM = Arm("ship", "kuebiko-sft:s24ci65", "1", "decompose")
ARMS = (CURRENT_ARM, SHIP_ARM)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def file_shas(root: Path, rels: Sequence[str]) -> dict[str, str]:
    """無いファイルは空文字 (食い違いとして manifest に残る)。"""
    return {r: sha256_bytes((root / r).read_bytes()) if (root / r).is_file() else "" for r in rels}


def verify_detect_model(path: Path = DETECT_MODEL_PATH) -> str:
    """detect ML の sha256 が prereg と違えば中止 (§9)。"""
    sha = sha256_bytes(path.read_bytes())
    if sha != DETECT_MODEL_SHA256:
        raise SystemExit(
            f"detect ML の sha256 {sha[:8]} が prereg の {DETECT_MODEL_SHA256[:8]} と違う"
        )
    return sha


# ---- 窓 ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Bin:
    start: datetime
    end: datetime

    @property
    def day(self) -> str:
        return self.start.strftime("%Y-%m-%d")

    @property
    def key(self) -> str:
        return self.start.strftime("%Y%m%dT%H00Z")


def build_bins(
    start: datetime = RANGE_START, end: datetime = RANGE_END, hours: int = BIN_HOURS
) -> list[Bin]:
    """UTC 6 時間の区間 (prereg §2.1: 32 区間)。"""
    out: list[Bin] = []
    cur = start
    while cur < end:
        out.append(Bin(cur, cur + timedelta(hours=hours)))
        cur += timedelta(hours=hours)
    return out


def sample_windows(
    eligible: Sequence[Bin], *, seed: int = SEED, n: int = N_WINDOWS, max_per_day: int = MAX_PER_DAY
) -> list[Bin]:
    """§4: 開始時刻の昇順 → ``Random(seed)`` で shuffle → 先頭から n 窓 (同じ日は上限あり)。"""
    ordered = sorted(eligible, key=lambda b: b.start)
    random.Random(seed).shuffle(ordered)
    per_day: dict[str, int] = {}
    picked: list[Bin] = []
    for b in ordered:
        if len(picked) >= n:
            break
        if per_day.get(b.day, 0) >= max_per_day:
            continue
        per_day[b.day] = per_day.get(b.day, 0) + 1
        picked.append(b)
    return picked


def arm_order(window_index: int, run: int, *, seed: int = SEED) -> list[Arm]:
    """§4: (窓, run) ごとに ``Random(seed + 窓の通し番号 * 10 + run)`` で腕の順序を入れ替える。"""
    arms = list(ARMS)
    random.Random(seed + window_index * 10 + run).shuffle(arms)
    return arms


def plan_units(window_ids: Sequence[str], runs: int = RUNS_PER_ARM) -> list[tuple[str, int, str]]:
    """実行順の (窓 id, run, 腕名)。窓の順に、run ごとに腕の順を入れ替える。"""
    out: list[tuple[str, int, str]] = []
    for i, wid in enumerate(window_ids):
        for run in range(1, runs + 1):
            out.extend((wid, run, a.name) for a in arm_order(i, run))
    return out


def arm_env(arm: Arm) -> dict[str, str]:
    """腕ごとに固定する環境変数 (他の旗は本番の値のまま)。"""
    return {"DETECT_DECOMPOSE": arm.decompose_mode, "LLM_USAGE_LOCAL": "0"}


# ---- 書き込みの封 -------------------------------------------------------------------------

READONLY_PGOPTION = "-c default_transaction_read_only=on"


def pgoptions_read_only(existing: str | None) -> str:
    """PGOPTIONS に read-only を足す (重複して足さない)。"""
    cur = (existing or "").strip()
    return cur if READONLY_PGOPTION in cur else f"{cur} {READONLY_PGOPTION}".strip()


def assert_read_only_setting(value: object) -> None:
    """``SHOW default_transaction_read_only`` の値が on でなければ中止。"""
    if str(value).strip().lower() != "on":
        raise WriteGuardError(f"default_transaction_read_only={value!r} (on でない)")


def install_read_only_env() -> None:
    os.environ["PGOPTIONS"] = pgoptions_read_only(os.environ.get("PGOPTIONS"))


def install_read_only_connect() -> None:
    """``db_backend.connect`` を、PG なら read-only セッションにして返すものに差し替える。

    repo の ``_connect`` は呼び出しのたびに ``db_backend.connect`` を引くので、これ以降
    このプロセスの DB 接続はすべて read-only (書き込み文は DB が拒否する)。
    """
    from scripts.readonly_conn import open_read_only
    from src.storage import db_backend

    orig = db_backend.connect

    def ro_connect(db_path: Any = None, **_kw: Any) -> Any:
        return open_read_only(orig, db_path)

    db_backend.connect = ro_connect


# ---- 凍結入力 -----------------------------------------------------------------------------


def encode_vec(vec: Any) -> str:
    import numpy as np

    return base64.b64encode(np.asarray(vec, dtype=np.float32).tobytes()).decode("ascii")


def decode_vec(text: str) -> Any:
    import numpy as np

    return np.frombuffer(base64.b64decode(text), dtype=np.float32).copy()


class FrozenRepo:
    """``run_decompose`` / ``_unbundle_detected`` が repo に求める読み取り 4 口の代役。

    DB には一切触れない (凍結した ``frozen`` の辞書だけを返す)。
    """

    def __init__(self, frozen: Mapping[str, Any]) -> None:
        self._f = frozen

    def get_articles_by_ids(self, article_ids: Sequence[str]) -> dict[str, Any]:
        arts = self._f["articles"]
        return {
            a: SimpleNamespace(
                summary=arts[a].get("summary", ""),
                feed_title=arts[a].get("feed_title", ""),
                created_at=arts[a].get("created_at", ""),
            )
            for a in article_ids
            if a in arts
        }

    def get_article_bodies(self, article_ids: Sequence[str]) -> dict[str, str]:
        arts = self._f["articles"]
        return {a: str(arts[a].get("body_prefix", "")) for a in article_ids if a in arts}

    def event_item_ids_by_article(self, article_ids: Sequence[str]) -> dict[str, set[str]]:
        items = self._f["event_items"]
        return {a: set(items[a]) for a in article_ids if a in items}

    def load_summary_embeddings(self, article_ids: Sequence[str]) -> dict[str, Any]:
        emb = self._f["embeddings"]
        return {a: decode_vec(emb[a]) for a in article_ids if a in emb}


def merge_scores_from_frozen(
    frozen: Mapping[str, Any],
) -> tuple[dict[tuple[str, str], tuple[bool, float | None]], list[str]]:
    """凍結した合流の採点 (``score_merge_pairs`` の戻り値と同じ形)。"""
    m = frozen.get("merge") or {}
    scores = {(str(s), str(a)): (bool(ok), p) for s, a, ok, p in m.get("scores", [])}
    return scores, [str(x) for x in m.get("sids", [])]


def write_json_with_sha(out_dir: Path, name: str, sha_name: str, payload: Any) -> dict[str, str]:
    """原子的に書き、sha256 を別ファイルに残す。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / name
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    hashes = {"inputs_sha256": sha256_bytes(path.read_bytes()), "bytes": str(path.stat().st_size)}
    (out_dir / sha_name).write_text(json.dumps(hashes, indent=2), encoding="utf-8")
    return hashes


def verify_inputs(out_dir: Path) -> str:
    """inputs.json が固定時の sha256 のままであることを確かめて sha256 を返す。"""
    path, sha_path = out_dir / INPUTS_NAME, out_dir / INPUTS_SHA_NAME
    if not path.is_file() or not sha_path.is_file():
        raise SystemExit(f"{path} / {sha_path} が無い (detect_snapshot.py を先に)")
    want = json.loads(sha_path.read_text(encoding="utf-8"))["inputs_sha256"]
    got = sha256_bytes(path.read_bytes())
    if got != want:
        raise SystemExit("inputs.json が固定後に変わっている (sha256 不一致)")
    return got
