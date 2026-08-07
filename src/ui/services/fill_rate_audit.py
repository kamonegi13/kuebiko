"""抽出 fill-rate の供給網ヘルス監査 (有機的結合監査 2026-07-12 の恒久対処 R1)。

「プロンプト/コード変更が抽出フィールドを沈黙崩壊させても数週間気づけない」病理
(intent 2 週間 / technical_axis 2 週間 / event_date 進行中 — すべて発見が人力・偶発)
への常設装置。audit_triage_calibration (importance 較正) と同じ「カテゴリ内で測る」
原則で、全分析列 + 主要 entity_type の週次被覆を監視し、直近週の急落を WARN する。

- 週次ジョブ (weekly-fill-rate-audit、月曜) が前週 vs 過去 4 週中央値で判定し
  ops チャンネルへ必ず 1 通投稿 (「届くこと」自体が監査の生存証明)
- 日次 heartbeat には番兵 3 指標 (intent / technical / event_date) の 7 日被覆を 1 行添付

判定は決定論 (LLM 不使用)。SQL は SQLite/PG 両対応 (substr + EXISTS + CASE WHEN のみ)。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from src.logging_config import get_logger

_log = get_logger(__name__)

# サイバー系カテゴリ (audit_triage_calibration / backfill と同一区分)
_CYBER = ("apt", "malware", "incident", "advisory", "vulnerability", "breach")
_VULN = ("vulnerability", "advisory")
_CYBER_EVENT = ("apt", "malware", "incident", "breach")

# WARN 判定: 過去週中央値がこの被覆以上のとき (=元々供給がある指標のみ監視)
_BASELINE_MIN_PCT = 10.0
# 直近週が中央値の半分未満に落ちたら WARN
_COLLAPSE_RATIO = 0.5
# 週の母数がこれ未満なら判定しない (ノイズ)
_MIN_WEEK_N = 30
# 比較対象の過去週数
_BASELINE_WEEKS = 4

# 緩慢劣化 (トレンド) 検知 (監査 2026-08-01): collapse 比 0.5 は「基準の 44% 未満」まで
# 沈黙するため、intent cyber 系が 6 週かけて 87%→68% (-20pt) 落ちても検知できなかった。
# 「直近 3 週すべてが遠い基準から -15pt 以上」で WARN する (単発の谷は騒がせない)。
# 基準は直近 3 週を除いた過去週の中央値 — rolling 基準だと基準自体がドリフトに追従して
# しまうため、劣化前の水準を保持する遠い窓で組む。
_DRIFT_RECENT_WEEKS = 3
_DRIFT_BASELINE_MAX_WEEKS = 8
_DRIFT_MIN_DROP_PCT = 15.0


@dataclass(frozen=True)
class FillMetric:
    """監視する抽出指標 1 つ (列 or entity)。"""

    key: str
    label: str
    condition: str  # articles alias `a` に対する SQL boolean 式
    categories: tuple[str, ...] | None  # None = posted 全件


def _entity_cond(entity_type: str) -> str:
    return (
        "EXISTS (SELECT 1 FROM article_entities e"
        f" WHERE e.article_id = a.article_id AND e.entity_type = '{entity_type}')"
    )


# 監視対象の SSoT。**新しい分析列 / entity_type を追加する PR はここにも 1 行追加する**
# (CLAUDE.md §7 の規約)。
METRICS: tuple[FillMetric, ...] = (
    FillMetric(
        "intent",
        "intent",
        "a.socio_political_intent IS NOT NULL AND a.socio_political_intent NOT IN ('', 'unknown')",
        None,
    ),
    # 主題アクター層 (2026-07-17): 取込時判定の実施率 (NULL = 判定が走っていない)。
    # 'none' (評価済み・主題なし) は正常値として被覆に数える — 監視対象は層の死活。
    FillMetric(
        "subject_actor",
        "主題actor評価",
        "a.subject_actor_source IS NOT NULL AND a.subject_actor_source <> ''",
        None,
    ),
    # アクター辞書 D1 (2026-07-26): 主題判定 LLM 層の生入力 (summarizer 出力) の供給監視。
    # 主題 311 件が全て title 層で LLM 層寄与ゼロと判明 — 生出力の充足率を常設監視して
    # 「summarizer が出していない/確度が低い/ゲートで落ちている」を切り分ける。
    # 非 cyber 記事は primary が出なくて正常のため cyber 系のみ対象。
    # C1 (2026-07-27) で judgment_classifier の ungated named_primary_actor 由来に置換し蘇生。
    FillMetric(
        "llm_primary_actor",
        "LLM主題出力",
        "a.llm_primary_actor_raw IS NOT NULL AND a.llm_primary_actor_raw <> ''",
        _CYBER,
    ),
    # 本文完全性 (2026-07-27, docs/body_extraction_and_entity_integrity_redesign.md §2.3):
    # 全文取得の成功率を常設監視。切り株 (feed_summary) が「生まれつき 0%」(GBHackers 型) の
    # 供給断や、UA 陳腐化による急落を検出する。full=全文取得済 (full/playwright/prefetch/scraper)。
    FillMetric(
        "full_body",
        "本文全文取得",
        "a.body_source IN ('full_extract','playwright_extract','prefetch','scraper')",
        _CYBER,
    ),
    FillMetric(
        "technical_axis",
        "技術結線",
        "a.technical_axis_summary IS NOT NULL AND a.technical_axis_summary <> ''",
        _CYBER,
    ),
    FillMetric(
        "event_date_vuln",
        "event_date(vuln)",
        "a.event_date IS NOT NULL AND a.event_date <> ''",
        _VULN,
    ),
    FillMetric(
        "event_date_cyber",
        "event_date(cyber)",
        "a.event_date IS NOT NULL AND a.event_date <> ''",
        _CYBER_EVENT,
    ),
    # scope は侵入事案系のみ (監査 2026-08-01): advisory/vulnerability には「初期侵害
    # 開始日」という概念が構造的に無く (CVE 公表に侵入日はない)、_CYBER 分母の 41% を
    # 占めて fill を 8% に希釈 → baseline 10% 未満で監視から永久に外れていた。
    # 侵入事案系では 18-30% 取れており、この scope なら沈黙崩壊を検知できる。
    FillMetric(
        "compromise_date",
        "compromise_date",
        "a.compromise_date IS NOT NULL AND a.compromise_date <> ''",
        _CYBER_EVENT,
    ),
    FillMetric(
        "remediation",
        "remediation",
        "a.remediation IS NOT NULL AND a.remediation <> ''",
        _VULN,
    ),
    FillMetric(
        "editorial_stance",
        "editorial_stance",
        "a.editorial_stance IS NOT NULL AND a.editorial_stance <> 'unknown'",
        None,
    ),
    FillMetric(
        "victim_sector",
        "victim_sector",
        "a.victim_sector_canonical IS NOT NULL"
        " AND a.victim_sector_canonical NOT IN ('', 'uncategorized')",
        _CYBER,
    ),
    FillMetric("ent_actor", "actor", _entity_cond("actor"), _CYBER),
    FillMetric("ent_cve", "cve", _entity_cond("cve"), _VULN),
    FillMetric("ent_ttp", "ttp", _entity_cond("ttp"), _CYBER),
    FillMetric("ent_malware_family", "malware_family", _entity_cond("malware_family"), _CYBER),
    FillMetric("ent_affected_vendor", "affected_vendor", _entity_cond("affected_vendor"), _VULN),
    FillMetric("ent_victim_org", "victim_org", _entity_cond("victim_org"), _CYBER),
    FillMetric(
        "ent_mentioned_country", "mentioned_country", _entity_cond("mentioned_country"), None
    ),
    FillMetric("ent_campaign", "campaign", _entity_cond("campaign"), _CYBER),
    # PMESII 7 軸 (監査 2026-07-16: T/I-infra が 6 週間沈黙しても検知できなかった盲点の
    # 閉鎖。T 軸は廃止済みのため登録しない)。boolean 軸の「付与 share」を fill として監視。
    FillMetric("pmesii_p", "PMESII P", "a.pmesii_p = 1", None),
    FillMetric("pmesii_m", "PMESII M", "a.pmesii_m = 1", None),
    FillMetric("pmesii_e", "PMESII E", "a.pmesii_e = 1", None),
    FillMetric("pmesii_s", "PMESII S", "a.pmesii_s = 1", None),
    FillMetric("pmesii_i_infra", "PMESII I-infra", "a.pmesii_i_infra = 1", None),
    FillMetric("pmesii_i_cyber", "PMESII I-cyber", "a.pmesii_i_cyber = 1", None),
    FillMetric("pmesii_p_env", "PMESII P-env", "a.pmesii_p_env = 1", None),
    # 監査 2026-08-01: 未登録列の沈黙断線 (article_type が 07-28 から 4 日間 0 件でも
    # 無警告) の実証を受けて登録。judgment_classifier 由来、posted なら常に付くはず。
    FillMetric(
        "article_type",
        "article_type",
        "a.article_type IS NOT NULL AND a.article_type <> ''",
        None,
    ),
    # victim_sector だけ登録され country が非対称に未登録だった穴 (監査 2026-08-01)。
    # scope 列 (global/regional/multi) も「国スコープが判定できた」として fill に数える。
    FillMetric(
        "victim_country",
        "victim_country",
        "((a.victim_country_iso IS NOT NULL AND a.victim_country_iso <> '')"
        " OR (a.victim_country_scope IS NOT NULL AND a.victim_country_scope <> ''))",
        _CYBER,
    ),
    # ランサム識別フラグ。breach カテゴリ内 share で監視 (全カテゴリでは希釈されて
    # baseline 10% を割り、永久 skip になるため)。
    FillMetric("is_ransomware", "is_ransomware(breach)", "a.is_ransomware = 1", ("breach",)),
)

# heartbeat 用の番兵 (今回の H1/H2 と同型の沈黙断線を 1 日で露見させる)
SENTINEL_KEYS: tuple[str, ...] = ("intent", "technical_axis", "event_date_cyber")


@dataclass(frozen=True)
class WeekCell:
    """(指標, 週) の被覆セル。"""

    metric_key: str
    week_start: date
    n: int
    filled: int

    @property
    def pct(self) -> float:
        return 100.0 * self.filled / self.n if self.n else 0.0


@dataclass(frozen=True)
class FillWarn:
    """急落 1 件。"""

    metric_key: str
    label: str
    baseline_pct: float
    current_pct: float
    current_n: int


def _week_start(d: date) -> date:
    """ISO 週の月曜を返す。"""
    return d - timedelta(days=d.weekday())


def fetch_daily_rows(con: Any, metric: FillMetric, since_iso: str) -> list[tuple[str, int, int]]:
    """指標 1 つの (day, n, filled) 日次行を返す (posted のみ、カテゴリ内)。"""
    cat_clause = ""
    params: list[object] = [since_iso]
    if metric.categories:
        placeholders = ", ".join("?" for _ in metric.categories)
        cat_clause = f" AND a.category IN ({placeholders})"
        params.extend(metric.categories)
    sql = (
        "SELECT substr(a.created_at, 1, 10) AS day, COUNT(*),"
        f" COUNT(CASE WHEN {metric.condition} THEN 1 END)"
        " FROM articles a"
        " WHERE a.status = 'posted' AND a.created_at >= ?"
        f"{cat_clause}"
        " GROUP BY substr(a.created_at, 1, 10)"
    )
    rows = con.execute(sql, params).fetchall()
    return [(str(r[0]), int(r[1]), int(r[2])) for r in rows]


def bucket_weekly(metric_key: str, daily_rows: list[tuple[str, int, int]]) -> list[WeekCell]:
    """日次行を ISO 週 (月曜開始) に束ねる (週開始日昇順)。"""
    acc: dict[date, tuple[int, int]] = {}
    for day_str, n, filled in daily_rows:
        try:
            d = date.fromisoformat(day_str[:10])
        except ValueError:
            continue
        wk = _week_start(d)
        cur = acc.get(wk, (0, 0))
        acc[wk] = (cur[0] + n, cur[1] + filled)
    return [
        WeekCell(metric_key=metric_key, week_start=wk, n=n, filled=filled)
        for wk, (n, filled) in sorted(acc.items())
    ]


def _median(values: list[float]) -> float:
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 == 1 else (s[mid - 1] + s[mid]) / 2.0


def detect_fill_collapse(
    cells: list[WeekCell], *, eval_week: date, label: str, min_week_n: int = _MIN_WEEK_N
) -> FillWarn | None:
    """直近完了週 (eval_week) の被覆が過去 4 週中央値の半分未満なら WARN を返す。

    - 中央値 < 10% の指標は監視対象外 (元々供給が無い列を騒がせない)
    - 直近週 n < min_week_n は判定しない (母数ノイズ)。feed 単位監査は標本が小さいため下げる。
    """
    current = next((c for c in cells if c.week_start == eval_week), None)
    if current is None or current.n < min_week_n:
        return None
    baseline_cells = [c for c in cells if c.week_start < eval_week and c.n >= min_week_n][
        -_BASELINE_WEEKS:
    ]
    if not baseline_cells:
        return None
    baseline = _median([c.pct for c in baseline_cells])
    if baseline < _BASELINE_MIN_PCT:
        return None
    if current.pct < baseline * _COLLAPSE_RATIO:
        return FillWarn(
            metric_key=current.metric_key,
            label=label,
            baseline_pct=baseline,
            current_pct=current.pct,
            current_n=current.n,
        )
    return None


def detect_fill_drift(
    cells: list[WeekCell], *, eval_week: date, label: str, min_week_n: int = _MIN_WEEK_N
) -> FillWarn | None:
    """緩慢劣化: 直近 3 週すべてが「劣化前の基準」から -15pt 以上低いとき WARN を返す。

    collapse (比 0.5) は基準の 44% 未満まで沈黙するため、intent cyber 系の
    87%→68% (-20pt / 6 週) 型の単調ドリフトを検知できなかった (監査 2026-08-01)。
    基準は直近 3 週を **除いた** 過去週の中央値 — rolling 基準は自身がドリフトに
    追従するため、劣化前の水準を保つ遠い窓で組む。単発の谷 (直近 1 週だけの低下)
    では発火しない。
    """
    recent_weeks = [eval_week - timedelta(weeks=i) for i in range(_DRIFT_RECENT_WEEKS)]
    recent = [next((c for c in cells if c.week_start == wk), None) for wk in recent_weeks]
    if any(c is None or c.n < min_week_n for c in recent):
        return None
    oldest_recent = recent_weeks[-1]
    baseline_cells = [c for c in cells if c.week_start < oldest_recent and c.n >= min_week_n][
        -_DRIFT_BASELINE_MAX_WEEKS:
    ]
    if len(baseline_cells) < 3:
        return None
    baseline = _median([c.pct for c in baseline_cells])
    if baseline < _BASELINE_MIN_PCT:
        return None
    # recent の None は上で除外済みだが、型 narrow のため再確認する
    recent_ok = [c for c in recent if c is not None]
    if all(c.pct <= baseline - _DRIFT_MIN_DROP_PCT for c in recent_ok):
        current = recent_ok[0]
        return FillWarn(
            metric_key=current.metric_key,
            label=label,
            baseline_pct=baseline,
            current_pct=current.pct,
            current_n=current.n,
        )
    return None


def build_drift_lines(drifts: list[FillWarn]) -> list[str]:
    """weekly 監査投稿に足す緩慢劣化セクション (OK 時は空 = 投稿を短く保つ)。"""
    if not drifts:
        return []
    lines = [
        f"fill 緩慢劣化: ⚠️ {len(drifts)} 指標が持続低下"
        f" (基準比 -{_DRIFT_MIN_DROP_PCT:.0f}pt 以上が {_DRIFT_RECENT_WEEKS} 週継続)"
    ]
    for w in sorted(drifts, key=lambda x: x.current_pct - x.baseline_pct):
        lines.append(f"- {w.label}: {w.baseline_pct:.0f}% → {w.current_pct:.0f}% (n={w.current_n})")
    return lines


def build_audit_report(
    warns: list[FillWarn], *, eval_week: date, metrics_checked: int
) -> tuple[str, str, str]:
    """週次監査の (title, body, importance) を組み立てる (純粋関数)。"""
    week_label = eval_week.strftime("%m/%d")
    if not warns:
        body = f"{week_label} 週: 全 {metrics_checked} 指標 OK (急落なし)"
        return ("📊 抽出 fill-rate 週次監査", body, "low")
    lines = [f"{week_label} 週: ⚠️ {len(warns)}/{metrics_checked} 指標が急落"]
    for w in warns:
        lines.append(f"- {w.label}: {w.baseline_pct:.0f}% → {w.current_pct:.0f}% (n={w.current_n})")
    lines.append("抽出プロンプト/コードの直近変更を確認 (沈黙断線の疑い)")
    return ("📊 抽出 fill-rate 週次監査", "\n".join(lines), "medium")


def _connect_default(db_path: Path | None = None) -> Any:
    from src.storage.db_backend import connect
    from src.storage.run_history import DEFAULT_DB_PATH

    return connect(db_path if db_path is not None else DEFAULT_DB_PATH)


def collect_weekly_cells(
    con: Any, *, now: datetime, weeks: int = _BASELINE_WEEKS + 2
) -> dict[str, list[WeekCell]]:
    """全指標の週次セルを収集する。"""
    since = (_week_start(now.date()) - timedelta(weeks=weeks)).isoformat()
    out: dict[str, list[WeekCell]] = {}
    for metric in METRICS:
        daily = fetch_daily_rows(con, metric, since)
        out[metric.key] = bucket_weekly(metric.key, daily)
    return out


# ---------- B4: source (feed) 単位の本文取得健全性 (body-health) 急落監査 ----------
# 列単位の full_body 監査 (METRICS) は category 横断で「どのソースが崩れたか」を切り分けられない。
# feed_title 単位で全文取得率の週次急落を検知し ops に通知する。denominator は posted でなく
# 全ステータス (抽出失敗=body NULL も分母) — 切り株率では見えない取得断を捉える (docs/
# body_source_state_machine_design.md §6)。
_FEED_MIN_WEEK_N = 20  # 週あたり標本下限 (小さいソースを騒がせない)
_FULL_BODY_COND = "body_source IN ('full_extract','playwright_extract','prefetch','scraper')"


def fetch_feed_body_health_rows(con: Any, since_iso: str) -> list[tuple[str, str, int, int]]:
    """feed_title 単位の (feed, day, n, full) 日次行 (全ステータス、grok/ransomware 除く)。"""
    # カウント列は別名必須: PG は COUNT(*)/COUNT(CASE) を両方 "count" と名付け、db_backend
    # の dict-row で列名衝突して潰れる (positional r[3] が範囲外) ため n_total/n_full と区別。
    sql = (
        "SELECT COALESCE(feed_title,'(不明)') AS feed, substr(created_at,1,10) AS day,"
        " COUNT(*) AS n_total,"
        f" COUNT(CASE WHEN {_FULL_BODY_COND} THEN 1 END) AS n_full"
        " FROM articles"
        " WHERE created_at >= ?"
        " AND (feed_title IS NULL OR LOWER(feed_title) NOT IN ('grok','ransomware.live'))"
        " GROUP BY COALESCE(feed_title,'(不明)'), substr(created_at,1,10)"
    )
    rows = con.execute(sql, [since_iso]).fetchall()
    return [(str(r[0]), str(r[1]), int(r[2]), int(r[3])) for r in rows]


def collect_feed_body_health_cells(
    con: Any, *, now: datetime, weeks: int = _BASELINE_WEEKS + 2
) -> dict[str, list[WeekCell]]:
    """feed 単位の body-health 週次セルを収集する。"""
    since = (_week_start(now.date()) - timedelta(weeks=weeks)).isoformat()
    by_feed: dict[str, list[tuple[str, int, int]]] = {}
    for feed, day, n, full in fetch_feed_body_health_rows(con, since):
        by_feed.setdefault(feed, []).append((day, n, full))
    return {feed: bucket_weekly(feed, daily) for feed, daily in by_feed.items()}


def detect_feed_body_health_collapses(
    cells_by_feed: dict[str, list[WeekCell]], *, eval_week: date
) -> list[FillWarn]:
    """feed 単位で全文取得率の急落を検知する (detect_fill_collapse を feed 別に適用)。"""
    warns: list[FillWarn] = []
    for feed, cells in cells_by_feed.items():
        w = detect_fill_collapse(
            cells, eval_week=eval_week, label=feed, min_week_n=_FEED_MIN_WEEK_N
        )
        if w is not None:
            warns.append(w)
    return warns


def build_feed_body_health_lines(warns: list[FillWarn], *, feeds_checked: int) -> list[str]:
    """weekly 監査投稿に足す source 別本文取得の急落セクション (OK 時は空=投稿を短く保つ)。"""
    if not warns:
        return []
    lines = [f"本文取得 (source別): ⚠️ {len(warns)}/{feeds_checked} ソースが急落"]
    for w in sorted(warns, key=lambda x: x.current_pct)[:8]:
        lines.append(f"- {w.label}: {w.baseline_pct:.0f}% → {w.current_pct:.0f}% (n={w.current_n})")
    return lines


async def run_weekly_fill_rate_audit() -> None:
    """週次 fill-rate 監査: 前週の被覆急落 + routing ルール発火を判定し ops へ必ず 1 通投稿する。

    ルール発火監査 (routing_rule_audit、2026-07-18) を同じ投稿の 1 セクションとして
    同梱する — routing rules は UI からいつでも編集できるため、編集起因の沈黙
    (R0 病理) を常設で検知する必要がある (編集時に監視を「同時導入」する機会は無い)。
    失敗しても例外は上げない (scheduler job を殺さない)。
    """
    try:
        now = datetime.now(UTC)
        eval_week = _week_start(now.date()) - timedelta(weeks=1)
        rule_lines: list[str] = []
        rule_warn_count = 0
        con = _connect_default()
        try:
            # drift 判定は「劣化前の基準」を要するため、collapse 用の 6 週より長い
            # 窓 (直近 3 週 + 基準最大 8 週 + 1) で収集する。collapse 判定は
            # eval_week 直前 4 週しか見ないため窓拡大の影響は受けない。
            by_metric = collect_weekly_cells(
                con, now=now, weeks=_DRIFT_BASELINE_MAX_WEEKS + _DRIFT_RECENT_WEEKS + 1
            )
            # routing ルール発火監査 (失敗しても fill 監査は生かす)
            try:
                from src.ui.services.routing_rule_audit import (
                    build_rule_section,
                    collect_rule_warns,
                )

                rule_warns, rules_checked = collect_rule_warns(
                    con, now_date=now.date(), eval_week=eval_week
                )
                rule_warn_count = len(rule_warns)
                rule_lines = build_rule_section(rule_warns, rules_checked=rules_checked)
            except Exception as e:  # noqa: BLE001
                _log.warning("routing_rule_audit_failed", error=str(e))
                rule_lines = ["ルール発火: 監査失敗 (ログ確認)"]
        finally:
            con.close()
        # 経路別主題被覆 (R3、2026-07-26): アクター言及ありなのに主題被覆が極端に低い
        # 取込経路を検出する。列単位の急落監査 (上の by_metric) は「生まれつき 0%」の
        # 新経路を捕らえられない (ransomware.live が 5 週沈黙した穴) — 絶対欠落を見張る。
        try:
            from src.storage.run_history import RunHistoryRepository

            repo = RunHistoryRepository()
            cov = repo.subject_coverage_by_pipeline(now - timedelta(days=14))
            for c in cov:
                if c["mentioned"] < 20:
                    continue  # 標本過少な経路は判定しない
                rate = c["with_subject"] / c["mentioned"]
                if rate < 0.05:
                    rule_lines.append(
                        f"⚠️ 経路 {c['pipeline']}: アクター言及 {c['mentioned']} 件中"
                        f" 主題 {c['with_subject']} 件 ({rate:.0%}) — 供給欠落の疑い"
                    )
                    rule_warn_count += 1
        except Exception as e:  # noqa: BLE001
            _log.warning("subject_coverage_audit_failed", error=str(e))
        # 概念 PIR の LLM 主題判定 backlog (規約 3 点セット: 消費者なき沈黙を常設検知)。
        # backlog が積み上がる = 夜間 judge バッチの沈黙 → 該当 PIR の照合が空白化する。
        try:
            from src.pir.llm_judge import judge_backlog_stats

            backlog = judge_backlog_stats()
            total_backlog = sum(v["backlog"] for v in backlog.values())
            if backlog:
                mark = " ⚠️" if total_backlog > 50 else ""
                rule_lines.append(
                    f"PIR LLM判定: 対象 {len(backlog)} PIR / "
                    f"未判定 backlog {total_backlog} 件{mark}"
                )
                if total_backlog > 50:
                    rule_warn_count += 1
            # 空振り・飢餓の両方向監視 (監査 2026-08-01 ③): match 率 < 5% = 候補ゲートが
            # 緩すぎて LLM を空振りさせている / 判定 < 10 件/週 = 候補側の飢餓。
            # backlog 監視だけではどちらも見えなかった。
            from src.storage.run_history import RunHistoryRepository as _Repo

            since7 = (now - timedelta(days=7)).isoformat()
            rates = _Repo().pir_judgment_rates_since(since7)
            for pir_id, (judged, matched) in sorted(rates.items()):
                if judged >= 30 and matched / judged < 0.05:
                    rule_lines.append(
                        f"⚠️ PIR {pir_id}: 7日 {judged} 判定中 適合 {matched} 件"
                        f" ({100.0 * matched / judged:.0f}%) — 候補ゲートが緩すぎる疑い"
                    )
                    rule_warn_count += 1
                elif pir_id in backlog and judged < 10:
                    rule_lines.append(f"⚠️ PIR {pir_id}: 7日 判定 {judged} 件のみ — 候補飢餓の疑い")
                    rule_warn_count += 1
        except Exception as e:  # noqa: BLE001
            _log.warning("pir_llm_judge_backlog_audit_failed", error=str(e))
        # B4: source 単位の本文取得急落 (feed_title 別 full_body 率の週次崩壊)。
        # 列単位の full_body 監査は「どのソースが崩れたか」を切り分けられないため補完する。
        try:
            con2 = _connect_default()
            try:
                feed_cells = collect_feed_body_health_cells(con2, now=now)
            finally:
                con2.close()
            feed_warns = detect_feed_body_health_collapses(feed_cells, eval_week=eval_week)
            feed_lines = build_feed_body_health_lines(feed_warns, feeds_checked=len(feed_cells))
            if feed_lines:
                rule_lines.extend(feed_lines)
                rule_warn_count += len(feed_warns)
        except Exception as e:  # noqa: BLE001
            _log.warning("feed_body_health_audit_failed", error=str(e))
        labels = {m.key: m.label for m in METRICS}
        warns = [
            w
            for key, cells in by_metric.items()
            if (w := detect_fill_collapse(cells, eval_week=eval_week, label=labels[key]))
        ]
        # 緩慢劣化 (トレンド)。collapse で既に警告した指標は二重報告しない
        # (collapse の方が重い信号)。
        collapsed_keys = {w.metric_key for w in warns}
        drifts = [
            d
            for key, cells in by_metric.items()
            if key not in collapsed_keys
            and (d := detect_fill_drift(cells, eval_week=eval_week, label=labels[key]))
        ]
        drift_lines = build_drift_lines(drifts)
        if drift_lines:
            rule_lines.extend(drift_lines)
            rule_warn_count += len(drifts)
        title, body, importance = build_audit_report(
            warns, eval_week=eval_week, metrics_checked=len(METRICS)
        )
        if rule_lines:
            body = body + "\n" + "\n".join(rule_lines)
        if rule_warn_count and importance == "low":
            importance = "medium"
        from src.ui.services.ops_notify import post_ops_message

        sent = await post_ops_message(title=title, body=body, importance=importance)
        _log.info("fill_rate_audit", sent=sent, warns=len(warns), rule_warns=rule_warn_count)
    except Exception as e:  # noqa: BLE001 — 監査自体の失敗で scheduler を汚さない
        _log.error("fill_rate_audit_failed", error=str(e))


def build_heartbeat_fill_line(db_path: Path | None = None) -> str | None:
    """heartbeat 用: 番兵 3 指標の直近 7 日被覆 1 行 (取得失敗は None)。

    直近 7 日が過去 28 日被覆の半分未満なら ⚠️ を付ける (沈黙断線を 1 日で露見させる)。
    """
    try:
        con = _connect_default(db_path)
        try:
            now = datetime.now(UTC)
            since = (now - timedelta(days=35)).date().isoformat()
            cutoff_recent = (now - timedelta(days=7)).date().isoformat()
            parts: list[str] = []
            warned = False
            for metric in METRICS:
                if metric.key not in SENTINEL_KEYS:
                    continue
                daily = fetch_daily_rows(con, metric, since)
                rec_n = sum(n for d, n, _ in daily if d >= cutoff_recent)
                rec_f = sum(f for d, _, f in daily if d >= cutoff_recent)
                base_n = sum(n for d, n, _ in daily if d < cutoff_recent)
                base_f = sum(f for d, _, f in daily if d < cutoff_recent)
                rec_pct = 100.0 * rec_f / rec_n if rec_n else 0.0
                base_pct = 100.0 * base_f / base_n if base_n else 0.0
                mark = ""
                if base_pct >= _BASELINE_MIN_PCT and rec_pct < base_pct * _COLLAPSE_RATIO:
                    mark = "⚠️"
                    warned = True
                parts.append(f"{metric.label} {rec_pct:.0f}%{mark}")
            prefix = "⚠️ 抽出7日" if warned else "抽出7日"
            return f"{prefix}: " + " / ".join(parts)
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001 — heartbeat 本体を壊さない
        _log.warning("heartbeat_fill_line_failed", error=str(e)[:80])
        return None
