"""失敗・取りこぼしジョブの自動リカバリ watchdog (2026-07-19)。

個人ノート PC 常駐ゆえ、スリープ・ネット切断で cron ジョブが失敗/欠落する。
- interval 収集 (毎時) は次周期で自己回復する → 対象外
- APScheduler の misfire 猶予は 1h → それ以内のズレはキャッチアップ済み
- **1h 超のスリープで飛んだ発火と、実行したが失敗した run** が本 watchdog の対象。
  日次/週次/月次は放置すると次周期 (最長 1 ヶ月) まで成果物が欠ける

設計 (イベント駆動でなく **状態ベース** = 収束型):
- 30 分ごとに「各 cron ジョブの直近周期に成功実績があるか」を runs / job_last_run の
  実績だけから決定論で検査する (再起動・プロセス跨ぎに頑健。失敗イベントの捕捉に依存しない)
- 衝突回避 (再実行は常に譲る側):
  (a) 実行中 run があれば延期 — 単一ロックの奪い合いをしない
  (b) 再実行区間 [now, now+max_runtime] が active な heavy cron の run 帯と重なるなら延期
  (c) 次の自然発火が近い (60 分以内) ならスケジュールに任せる
  「予定ジョブの一時停止」はしない — 停止は連鎖欠落を生む。譲る方が単純で安全
- 暴走防止: 周期内の自動再実行は最大 3 回 (process 内 cap) + 前回試行から 45 分の間隔
- 一度も実行実績の無いジョブは対象外 (新設ジョブの意図しない初回発火を防ぐ)
- 再実行・断念は ops チャンネルへ通知 (可観測性 — 静かな自動介入は事故のもと)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from src.logging_config import get_logger
from src.scheduler.job_registry import JobDef, _heavy_active_on, load_jobs

_log = get_logger(__name__)

Cadence = Literal["daily", "weekly", "monthly"]
Action = Literal["healthy", "retry", "defer", "give_up", "skip"]

RECOVERY_MAX_ATTEMPTS = 3
RETRY_SPACING = timedelta(minutes=45)
# 次の自然発火がこの範囲内なら再実行せずスケジュールに任せる
NATURAL_FIRE_YIELD = timedelta(minutes=60)
# 試行 cap の減衰窓。cap の目的は連打防止であって恒久断念ではない — 「起きたまま
# 数時間オフライン」で 3 回使い切っても、12h 経てば自動で再挑戦が復活する
# (週次/月次が次周期まで放置される穴を塞ぐ。壊れたジョブでも最大 3 回/12h に留まる)。
ATTEMPT_DECAY = timedelta(hours=12)

_PERIOD: dict[Cadence, timedelta] = {
    "daily": timedelta(days=1),
    "weekly": timedelta(days=7),
    "monthly": timedelta(days=31),
}
# 「遅れているだけ」を失敗扱いしない猶予 (発火予定時刻 + 実行時間 + ばらつき)
_GRACE: dict[Cadence, timedelta] = {
    "daily": timedelta(hours=3),
    "weekly": timedelta(hours=6),
    "monthly": timedelta(hours=12),
}


def job_cadence(job: JobDef) -> Cadence | None:
    """回復対象の周期区分。cron 以外 (interval=自己回復 / reactive) は None。"""
    if job.schedule_type != "cron" or job.hour is None:
        return None
    if job.day:
        return "monthly"
    if job.day_of_week:
        return "weekly"
    return "daily"


@dataclass(frozen=True)
class RecoveryDecision:
    job_id: str
    action: Action
    reason: str


def decide(
    job: JobDef,
    *,
    now: datetime,
    last_success_at: datetime | None,
    has_ever_run: bool,
    last_attempt_at: datetime | None,
    attempts_in_window: int,
    busy: bool,
    next_fire_at: datetime | None,
    heavy_conflict: bool,
) -> RecoveryDecision:
    """1 ジョブの回復判定 (純関数、テスト対象)。"""
    cadence = job_cadence(job)
    if cadence is None:
        return RecoveryDecision(job.id, "skip", "cron 以外は対象外")
    if not job.enabled:
        return RecoveryDecision(job.id, "skip", "無効化中 (ユーザー判断を尊重)")
    if not has_ever_run:
        return RecoveryDecision(job.id, "skip", "実行実績なし (新設ジョブは対象外)")

    stale_after = _PERIOD[cadence] + _GRACE[cadence]
    if last_success_at is not None and now - last_success_at < stale_after:
        return RecoveryDecision(job.id, "healthy", "直近周期に成功あり")

    # ここから「直近周期の成功が無い」= 回復候補
    if attempts_in_window >= RECOVERY_MAX_ATTEMPTS:
        return RecoveryDecision(
            job.id,
            "give_up",
            f"自動再実行 {RECOVERY_MAX_ATTEMPTS} 回失敗 — 12h 休止後に再挑戦 (手動対応も可)",
        )
    if next_fire_at is not None and timedelta(0) <= next_fire_at - now <= NATURAL_FIRE_YIELD:
        return RecoveryDecision(job.id, "skip", "次の自然発火が近い (スケジュールに任せる)")
    if busy:
        return RecoveryDecision(job.id, "defer", "他 run 実行中 (次 tick で再判定)")
    if heavy_conflict:
        return RecoveryDecision(job.id, "defer", "heavy ジョブ帯と重複 (次 tick で再判定)")
    if last_attempt_at is not None and now - last_attempt_at < RETRY_SPACING:
        return RecoveryDecision(job.id, "defer", "前回試行から間隔不足")
    return RecoveryDecision(job.id, "retry", f"直近 {cadence} 周期に成功なし → 自動再実行")


def heavy_window_conflict(job: JobDef, jobs: list[JobDef], now_jst: datetime) -> bool:
    """再実行区間 [now, now+max_runtime] が active な heavy cron の run 帯と重なるか。

    job_registry.is_collection_suppressed と同じ区間 overlap 原理 (収集抑止の回復版)。
    自分自身の帯は除外する (自分の予定時刻は自然発火 yield 側が扱う)。
    """
    weekday = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[now_jst.weekday()]
    start = now_jst.hour * 60 + now_jst.minute
    end = start + max(1, job.max_runtime_minutes)
    for h in jobs:
        if h.id == job.id or not h.heavy or h.schedule_type != "cron" or h.hour is None:
            continue
        if not _heavy_active_on(h, weekday=weekday, day_of_month=now_jst.day):
            continue
        h_start = h.hour * 60 + (h.minute or 0)
        h_end = h_start + max(1, h.max_runtime_minutes)
        if start < h_end and end > h_start:
            return True
    return False


# ---------- 実行層 (scheduler process 内から 30 分ごとに呼ばれる) ----------

# 周期内の自動再実行 timestamp (job_id → 発火時刻列)。process 内 cap — 再起動で
# リセットされるが、状態検査自体は永続実績ベースなので安全側 (最悪 cap が緩むだけ)。
_attempts: dict[str, list[datetime]] = {}


def _parse_ts(raw: object) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _job_history(job: JobDef, db_path: Any) -> tuple[datetime | None, bool, datetime | None]:
    """(last_success_at, has_ever_run, last_attempt_at) を実績テーブルから引く。

    pipeline は runs、bespoke は job_last_run (最新 1 行のみ保持) を見る。
    """
    from src.storage.db_backend import connect

    conn = connect(db_path)
    try:
        if job.kind == "pipeline":
            row = conn.execute(
                "SELECT MAX(CASE WHEN status='succeeded' THEN started_at END) AS ok,"
                " MAX(started_at) AS any_run, COUNT(*) AS n"
                " FROM runs WHERE pipeline = ?",
                (job.id,),
            ).fetchone()
            return _parse_ts(row["ok"]), int(row["n"] or 0) > 0, _parse_ts(row["any_run"])
        row = conn.execute(
            "SELECT last_run_at, status FROM job_last_run WHERE job_id = ?",
            (job.id,),
        ).fetchone()
        if row is None:
            return None, False, None
        ts = _parse_ts(row["last_run_at"])
        ok = ts if str(row["status"]) == "succeeded" else None
        return ok, True, ts
    finally:
        conn.close()


def _count_running(db_path: Any) -> int:
    from src.storage.db_backend import connect

    conn = connect(db_path)
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM runs WHERE status='running'").fetchone()
        return int(row["n"] or 0)
    finally:
        conn.close()


async def run_job_recovery(scheduler: Any, db_path: Any) -> list[RecoveryDecision]:
    """全 cron ジョブを検査し、必要なら再実行を trigger する (30 分ごとの watchdog 本体)。"""
    now = datetime.now(UTC)
    now_jst = now.astimezone(ZoneInfo("Asia/Tokyo"))
    jobs = load_jobs()
    busy = _count_running(db_path) > 0
    decisions: list[RecoveryDecision] = []

    for job in jobs:
        if job_cadence(job) is None:
            continue
        last_success, has_run, last_attempt = _job_history(job, db_path)
        # watchdog 自身の試行も間隔判定に含める (自然 run が記録される前の連打防止)。
        # 試行は ATTEMPT_DECAY で減衰する (周期全体でなく 12h — 長時間オフライン後の自動復帰)
        own = [t for t in _attempts.get(job.id, []) if now - t < ATTEMPT_DECAY]
        _attempts[job.id] = own
        effective_last_attempt = max(
            [t for t in (last_attempt, own[-1] if own else None) if t is not None],
            default=None,
        )
        try:
            next_fire = scheduler.next_run_at(job.id)
        except Exception:  # noqa: BLE001 — 未登録 job は yield 判定なしで進む
            next_fire = None

        d = decide(
            job,
            now=now,
            last_success_at=last_success,
            has_ever_run=has_run,
            last_attempt_at=effective_last_attempt,
            attempts_in_window=len(own),
            busy=busy,
            next_fire_at=next_fire,
            heavy_conflict=heavy_window_conflict(job, jobs, now_jst),
        )
        decisions.append(d)

        if d.action == "retry":
            try:
                scheduler.trigger_now(job.id)
            except Exception as e:  # noqa: BLE001 — 1 job の trigger 失敗で watchdog を止めない
                _log.warning("job_recovery_trigger_failed", job_id=job.id, error=str(e))
                continue
            _attempts.setdefault(job.id, []).append(now)
            busy = True  # 同 tick 内の多重 trigger を防ぐ (次 tick で残りを再判定)
            _log.warning(
                "job_recovery_triggered",
                job_id=job.id,
                attempt=len(_attempts[job.id]),
                last_success=str(last_success),
            )
            last_ok = (
                last_success.astimezone(ZoneInfo("Asia/Tokyo")).strftime("%m/%d %H:%M")
                if last_success
                else "記録なし"
            )
            await _notify(
                title=f"ジョブ自動リカバリ: {job.title} を再実行",
                body=(
                    f"{job.id} の直近周期に成功が無いため自動再実行します "
                    f"(試行 {len(_attempts[job.id])}/{RECOVERY_MAX_ATTEMPTS}、最終成功: {last_ok})"
                ),
            )
        elif d.action == "give_up" and len(own) == RECOVERY_MAX_ATTEMPTS:
            # cap 到達直後の 1 回だけ通知 (以降の tick では own が減衰するまで沈黙)
            _log.warning("job_recovery_gave_up", job_id=job.id, reason=d.reason)
            await _notify(
                title=f"ジョブ自動リカバリ断念: {job.title}",
                body=(
                    f"{job.id} の自動再実行が {RECOVERY_MAX_ATTEMPTS} 回失敗しました。"
                    "12 時間休止して再挑戦します (手動実行はジョブ管理から可能)"
                ),
                importance="medium",
            )

    counts: dict[str, int] = {}
    for d in decisions:
        counts[d.action] = counts.get(d.action, 0) + 1
    _log.info("job_recovery_checked", **counts)
    return decisions


async def _notify(*, title: str, body: str, importance: str = "low") -> None:
    """ops チャンネルへ通知 (fail-safe — 通知失敗で回復自体は妨げない)。"""
    try:
        from src.ui.services.ops_notify import post_ops_message

        await post_ops_message(title=title, body=body, importance=importance)
    except Exception as e:  # noqa: BLE001
        _log.warning("job_recovery_notify_failed", error=str(e))
