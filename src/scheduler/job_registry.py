"""背景ジョブ統一レジストリ (2026-07-06)。

**本質**: システムの定常仕事は 3 機構 (K1 スケジュールパイプライン / K2 bespoke ジョブ /
K3 reactive トリガ) に分裂して統治されていた。本モジュールは全ジョブを 1 つの ``JobDef``
抽象に畳み、単一のコントロールプレーン (可視・ON/OFF・時刻変更・ミス防止・観測) を成す。

- **メタデータ (id/kind/title/description/protection) はコードが所有** — user 定義不可 =
  安全 (callable はコード側にしか無い)。**mutable な enabled + schedule のみ DB が所有**。
- config_store (``app_config_versions``、key=``job_schedules``) を SSoT に版履歴つきで永続化。
  yaml/hardcode は seed。編集は DB に版保存され再起動で維持される (従来の pause は runtime
  のみで再起動消失していた欠陥を解消)。[[operational_config_db]] と同型。
- ``load_jobs`` = default_jobs (canonical) に DB override (enabled+schedule) を重ねる。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from src.logging_config import get_logger
from src.storage.config_store import get_config, save_config, seed_config_if_absent

_log = get_logger(__name__)

JOBS_KEY = "job_schedules"

JobKind = Literal["pipeline", "bespoke", "reactive"]
# protection = ミス防止ガードの強度。critical=停止でコア機能/観測が壊れる /
# important=機能を失う / optional=自由に切替。
Protection = Literal["critical", "important", "optional"]
ScheduleType = Literal["cron", "interval", "reactive"]

# interval の下限 (誤って極小値でシステムを溢れさせない)
MIN_INTERVAL_MINUTES = 5


class JobDef(BaseModel):
    """1 つの背景ジョブの宣言 (メタ=コード所有 / enabled+schedule=DB 可変)。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str
    kind: JobKind
    title: str
    description: str
    # 「止めると何が困るか」— ガードレールの確認ダイアログ文言に使う
    disable_impact: str = ""
    protection: Protection = "important"
    enabled: bool = True
    # 夜間解析帯 (analysis window) の間は fire しない収集ジョブか。True の interval collection
    # (rss/web-scraper) は重い夜間 synthesis と Ollama/ロックを奪い合うため窓の間停止する。
    respects_analysis_window: bool = False
    # LLM を長時間占有する重い処理か (synthesis/deep-dive 等)。タイムラインで「重い処理帯」の
    # 黄色バンドを **このジョブの実時刻から動的に描く** ため (時刻変更に追随)。静的 danger_windows
    # の置換元。reactive (auto-trigger) は時刻を持たないためバンドは描かない。
    heavy: bool = False
    # 定常キュー処理 (upkeep) か。時刻に運用上の意味がなく「仕事が残っていれば消化して
    # 即終了する」型の補助ジョブ (PIR 判定 / 本文翻訳 / 自動復旧 watchdog 等)。
    # UI タイムラインでは専用 swimlane を持たず「定常処理」集約 1 行に畳まれる
    # (2026-07-26 — 補助ジョブ増加でタイムラインが点列に支配される問題の対処。SSoT はこのフラグ)。
    upkeep: bool = False
    # 想定処理時間の目安 (分)。重い run 帯 (黄色バンド) の **幅** を [開始時刻, 開始+これ] で
    # 描くための SSoT。CLAUDE.md 記載の実測レンジと モデルサイズ から設定 (monthly が最長)。
    # subprocess timeout (pipeline_runner の hard cap) は常にこれ以上で安全余裕を持つ。
    max_runtime_minutes: int = 5
    schedule_type: ScheduleType = "cron"
    # cron
    hour: int | None = None
    minute: int | None = None
    day_of_week: str | None = None  # "mon" / "tue" ... (週次)
    day: str | None = None  # "1" (月初) 等
    # interval
    interval_minutes: int | None = None
    offset_minutes: int | None = None
    # reactive
    debounce_hours: float | None = None

    def schedule_label(self) -> str:
        """人間可読なスケジュール表記 (UI/ログ用)。"""
        if self.schedule_type == "interval":
            base = f"{self.interval_minutes}分ごと"
            return base + (f" (+{self.offset_minutes}分)" if self.offset_minutes else "")
        if self.schedule_type == "reactive":
            return f"reactive (debounce {self.debounce_hours}h)"
        dow = f"{self.day_of_week} " if self.day_of_week else ""
        dom = f"毎月{self.day}日 " if self.day else ""
        return f"{dow}{dom}{self.hour:02d}:{(self.minute or 0):02d}"


# ---------- 既定ジョブ集合 (現行スケジュールを behavior-preserving に符号化) ----------


def default_jobs() -> list[JobDef]:
    """全背景ジョブの canonical 定義。DB seed の源であり、メタデータの SSoT。"""
    return [
        # ----- K1: 収集 -----
        JobDef(
            id="direct-rss-fetch",
            kind="pipeline",
            title="RSS 取得",
            description="100+ の RSS feed を並列取得し要約・投稿する主収集経路。",
            disable_impact="新規記事が一切入らなくなる (収集停止)。",
            protection="critical",
            schedule_type="interval",
            interval_minutes=60,
            respects_analysis_window=True,
            max_runtime_minutes=12,  # 動的抑止の C (heavy 開始前マージン)。実測 5-10 分の余裕込み
        ),
        JobDef(
            id="web-scraper-watchers",
            kind="pipeline",
            title="Web スクレイパ監視",
            description="RSS の無い一次ソース (ENISA/IPA/ISW 等) を sitemap 経由で監視。",
            disable_impact="RSS の無い一次ソースの新着を取りこぼす。",
            protection="important",
            schedule_type="interval",
            interval_minutes=60,
            respects_analysis_window=True,
            max_runtime_minutes=5,  # 動的抑止の C (実測 ~4 分)
        ),
        JobDef(
            id="grok-briefing",
            kind="pipeline",
            title="Grok レポート取込",
            description="Grok タスクのレポートを IMAP 通知経由で取込む。",
            disable_impact="Grok の深掘り/早期シグナルが入らない。",
            protection="important",
            schedule_type="cron",
            hour=6,
            minute=0,
        ),
        JobDef(
            id="ransomware-live-ingest",
            kind="bespoke",
            title="ランサム被害取込",
            description="ransomware.live の被害公表を取込 (JP=japan_watch 投稿 / 他=地図)。",
            disable_impact="ランサム被害の地図/日本監視が更新されない。",
            protection="optional",
            schedule_type="interval",
            interval_minutes=180,
            offset_minutes=25,
        ),
        # ----- K1: 配信 -----
        JobDef(
            id="morning-brief",
            kind="pipeline",
            heavy=True,
            max_runtime_minutes=8,
            title="朝ブリーフィング",
            description=(
                "毎朝の日次総括を生成し brief チャンネルへ配信。standing 常設情報要求の"
                "収穫 (R1-R3) + 予約枠再評価 (staleness 7日) もここで走る。"
            ),
            disable_impact="朝の通読ブリーフが出なくなる。",
            protection="critical",
            schedule_type="cron",
            hour=6,
            minute=30,
        ),
        JobDef(
            id="evening-brief",
            kind="pipeline",
            heavy=True,
            max_runtime_minutes=8,
            title="夕ブリーフィング",
            description=(
                "夕方の日次状況更新を生成し brief チャンネルへ配信。standing 常設情報要求の"
                "収穫 (R1-R3) + 予約枠再評価 (staleness 7日) もここで走る。"
            ),
            disable_impact="夕方の状況更新が出なくなる。",
            protection="important",
            schedule_type="cron",
            hour=19,
            minute=30,
        ),
        # ----- K1/K3: 分析・総括 -----
        JobDef(
            id="auto-trigger-synthesis",
            kind="reactive",
            heavy=True,
            max_runtime_minutes=25,
            title="総括の自動更新 (near-realtime)",
            description=(
                "RSS spike の後に日次 synthesis を自動更新する (debounce で頻度抑制)。"
                "standing 常設への証拠割当 (収穫) もここで走る (評価は定時ジョブのみ)。"
            ),
            disable_impact="日中の記事急増に総括が追随しなくなる (定時総括は残る)。",
            protection="important",
            schedule_type="reactive",
            debounce_hours=6.0,
        ),
        JobDef(
            # id は runs 履歴・DB override・リカバリ実績の継続性のため不変 (表示名のみ改称)
            id="weekly-recap",
            kind="pipeline",
            heavy=True,
            max_runtime_minutes=25,
            title="週次深掘りダイジェスト",
            description=(
                "その週に読み逃してはならない重要報道を選定し、深掘り解説つきで配信。"
                "選定は週中に蓄積したシグナル (importance/PIR/KEV 等) の決定論射影 + "
                "LLM の最終判断 (2026-07-20 再設計)。"
            ),
            disable_impact="週次の深掘りダイジェストが出なくなる。",
            protection="important",
            schedule_type="cron",
            day_of_week="mon",
            hour=2,
            minute=0,
        ),
        JobDef(
            id="weekly-status-synthesis",
            kind="pipeline",
            heavy=True,
            max_runtime_minutes=25,
            title="週次状況総括",
            description=(
                "前週の中期情勢を総括 (reasoning ティア)。standing 常設の週次対称"
                "反証 sweep (adversarial) もここで走る。"
            ),
            disable_impact="週次の中期情勢総括が出なくなる。",
            protection="important",
            schedule_type="cron",
            day_of_week="mon",
            hour=2,
            minute=45,
        ),
        JobDef(
            id="monthly-status-synthesis",
            kind="pipeline",
            heavy=True,
            max_runtime_minutes=45,
            title="月次状況総括",
            description="前月の長期情勢を総括。",
            disable_impact="月次の長期情勢総括が出なくなる。",
            protection="important",
            schedule_type="cron",
            day="1",
            hour=3,
            minute=0,
        ),
        JobDef(
            id="pir-spotlight",
            kind="pipeline",
            heavy=True,
            max_runtime_minutes=10,
            title="PIR スポットライト",
            description="PIR 縦断の週次 narrative (Intel Graph)。",
            disable_impact="PIR 別の週次追跡 narrative が更新されない。",
            protection="optional",
            schedule_type="cron",
            day_of_week="mon",
            hour=3,
            minute=30,
        ),
        # ----- K1: 学習・辞書 -----
        JobDef(
            id="weekly-taxonomy-review",
            kind="pipeline",
            heavy=True,
            max_runtime_minutes=10,
            title="週次タクソノミレビュー",
            description="分類辞書の改善提案を LLM 生成 (UI で承認)。",
            disable_impact="分類辞書の改善提案が溜まらない。",
            protection="important",
            schedule_type="cron",
            day_of_week="mon",
            hour=4,
            minute=30,
        ),
        JobDef(
            id="mitre-actor-sync",
            kind="pipeline",
            heavy=True,
            max_runtime_minutes=6,
            title="MITRE アクター同期",
            description="MITRE ATT&CK → アクター辞書の週次同期 + 新興候補 harvest。",
            disable_impact="アクター辞書が MITRE 更新に追随しない。",
            protection="important",
            schedule_type="cron",
            day_of_week="tue",
            hour=3,
            minute=0,
        ),
        # ----- K2/K3: 保守 -----
        JobDef(
            id="pir-entity-rebuild",
            kind="bespoke",
            heavy=True,
            max_runtime_minutes=15,
            title="PIR タグ夜間 reconcile",
            description=(
                "posted 記事 × 全 PIR を再評価し pir タグを整合させる夜間 reconcile。"
                "取込時 inline タグの取りこぼし救済 + 過去分 backfill (2026-07-06 に朝の"
                "副作用から夜間独立ジョブへ移設)。"
            ),
            disable_impact="PIR タグの過去分整合が取れなくなる (新規は inline で付く)。",
            protection="important",
            schedule_type="cron",
            hour=3,
            minute=5,
        ),
        JobDef(
            id="ledger-deep-review",
            kind="bespoke",
            heavy=True,
            max_runtime_minutes=75,
            title="台帳 ACH 夜間精査",
            description=(
                "当日更新された Situation の ACH 判定を、narrative ティアのモデルの"
                "拡張思考 (think) で夜間に再評価し精緻化する。昼の即時判定 (think なし) を"
                "上書きし、翌朝の報告・ACH 表示は精緻判定を自動継承。narrative ティアが"
                "ローカルモデル、または拡張思考が無効設定のときは何もしない。"
            ),
            disable_impact="ACH 判定が昼の即時品質のまま (仮説採点の深さが夜間精査分だけ低下)。",
            protection="optional",
            schedule_type="cron",
            hour=3,
            minute=30,
        ),
        JobDef(
            id="pir-judge-hourly",
            kind="bespoke",
            title="PIR 主題判定 毎時増分",
            description=(
                "概念 PIR の LLM 主題判定を新着候補のみ小 cap (20) で毎時実行し、"
                "適合を pir タグへ即時反映する。判定は数件/時 × ~3 秒/件の軽量処理。"
                "stale 再判定 (PIR 編集起因) と full reconcile は夜間 pir-entity-rebuild が担う。"
            ),
            disable_impact=(
                "日中取込分の judge PIR 適合が夜間バッチまで未確定になる "
                "(PIR 画面 / News facet / 夕方 synthesis で最大 24h 遅延)。"
            ),
            protection="optional",
            schedule_type="interval",
            interval_minutes=60,
            # 毎時 :45 — 朝夕ブリーフ (06:30/19:30) と深夜バッチ帯の主要 fire 時刻を避ける
            offset_minutes=45,
            upkeep=True,
        ),
        JobDef(
            id="body-translate-backlog",
            kind="bespoke",
            title="本文自動翻訳",
            description=(
                "未訳記事 (body あり・body_ja なし) を新しい順に 40 件/時までローカル LLM "
                "(fast ティア) で全訳し body_ja へキャッシュする常設ジョブ。本務は新規流入 "
                "(~470 件/日) の当日中の自動翻訳で、過去分バックログは残余キャパで漸進消化 "
                "(消化後も新規向けに恒久稼働)。時間予算 12 分で :30 の scraper 前に終わる。"
                "無効化すると記事詳細の「日本語訳」ボタンによる on-demand 専用に戻る。"
            ),
            disable_impact=(
                "新規記事の日本語訳が事前生成されなくなる (記事詳細で押した時だけの "
                "on-demand 翻訳に戻る。既訳キャッシュはそのまま残る)。"
            ),
            protection="optional",
            schedule_type="interval",
            interval_minutes=60,
            # 毎時 :15 — RSS 収集 (:00 台、実測 5-12 分) の後、scraper (:30) の前の隙間。
            offset_minutes=15,
            respects_analysis_window=True,  # 夜間解析帯は Ollama を奪い合わない
            max_runtime_minutes=15,  # 時間予算 12 分 + 余裕
            upkeep=True,
        ),
        JobDef(
            id="body-refetch-backlog",
            kind="bespoke",
            title="本文再取得",
            description=(
                "全文取得に失敗して feed 抜粋 (切り株) で保存された記事、または body NULL の "
                "記事を新しい順・高 importance 優先で 20 件/時まで再取得し、成功したら全文化 + "
                "再エンリッチ (entity 入れ直し / 主題再判定 / 分析列更新 / 再翻訳キュー投入) する "
                "常設ジョブ。Discord 再投稿はしない (web 側の silent enrichment)。UA 修正後の "
                "切り株救済 + 恒常的な全文化網。手動加速は scripts/refetch_stumps.py。"
            ),
            disable_impact=(
                "切り株記事が全文化されず、痩せた entity (IoC/TTP 欠落) のまま残る。"
                "手動再取得 (scripts/refetch_stumps.py) は引き続き可能。"
            ),
            protection="optional",
            schedule_type="interval",
            interval_minutes=60,
            # 毎時 :40 — RSS 収集 (:00 台) / 翻訳 (:15) / scraper (:30) の後の隙間。
            offset_minutes=40,
            respects_analysis_window=True,  # 夜間解析帯は Ollama を奪い合わない
            max_runtime_minutes=15,
            upkeep=True,
        ),
        JobDef(
            id="ua-health-check",
            kind="bespoke",
            title="UA 自己修復",
            description=(
                "本文取得の User-Agent が古くなって WAF に 403 で弾かれていないかを、直近 full "
                "取得できたドメイン (canary) への実 probe で週次検証する。劣化時は候補 UA "
                "(Chrome 版上げ) を実測検証し、現行を上回るものがあれば .env を自動更新 + "
                "ops へ通知する (採用前に必ず実測、全滅時はサイト側問題として手動レビュー通知)。"
                "UA 陳腐化による切り株量産 (GBHackers 型) の恒久予防。"
            ),
            disable_impact=(
                "UA が古くなっても自動更新されず、いずれ WAF ブロックで全文取得が沈黙する。"
                "手動で「設定 → システム」の CONTENT_EXTRACTOR_USER_AGENT を更新すれば復旧。"
            ),
            protection="optional",
            schedule_type="cron",
            # 週次 (月曜 02:40 JST)。深夜バッチ帯だが外部 GET 数件のみで軽量。
            day_of_week="mon",
            hour=2,
            minute=40,
        ),
        JobDef(
            id="daily-maintenance",
            kind="bespoke",
            title="日次 DB 保守",
            description="ログ/dedup/body/detection の retention purge 一式 (夜間)。",
            disable_impact="DB が無限成長する (retention 停止)。",
            protection="critical",
            schedule_type="cron",
            hour=3,
            minute=17,
        ),
        JobDef(
            id="daily-heartbeat",
            kind="bespoke",
            title="死活ハートビート",
            description=(
                "毎朝 08:00 に稼働サマリ + 沈黙 feed + 抽出 fill-rate 番兵 +"
                " standing 常設サマリを ops へ 1 通 (dead-man's switch)。"
            ),
            disable_impact="「届かない=異常」の死活監視が失われる。",
            protection="critical",
            schedule_type="cron",
            hour=8,
            minute=0,
        ),
        JobDef(
            id="job-recovery-watchdog",
            kind="bespoke",
            title="ジョブ自動リカバリ",
            description=(
                "日次/週次/月次ジョブの失敗・取りこぼし (1h 超スリープの misfire 含む) を"
                " 30 分ごとに検査し、静かな時間帯に自動再実行する (状態ベース watchdog)。"
            ),
            disable_impact="失敗/欠落した日次・週次・月次の成果物が次周期まで復旧しない。",
            protection="important",
            schedule_type="interval",
            interval_minutes=30,
            offset_minutes=12,  # :12/:42 発火 — 毎時収集 (:00/:30) と重ねない
            max_runtime_minutes=1,
            upkeep=True,
        ),
        JobDef(
            id="weekly-fill-rate-audit",
            kind="bespoke",
            title="抽出 fill-rate 週次監査",
            description=(
                "全分析列 + entity の週次被覆をカテゴリ内で監視し、前週の急落を"
                " WARN として ops へ投稿 (供給網ヘルス。LLM 不使用の決定論)。"
            ),
            disable_impact="抽出フィールドの沈黙崩壊 (intent/event_date 型) を数週間見逃す。",
            protection="important",
            schedule_type="cron",
            day_of_week="mon",
            hour=8,
            minute=10,
        ),
        JobDef(
            id="actor-history-distill",
            kind="bespoke",
            title="アクター行動史 月次蒸留",
            description=(
                "subject 記事の取込時判定を actor_observed_profile (月次期間行) へ"
                "決定論射影する (アクター辞書の永久行動史。LLM 不使用)。定常は当月+前月"
                "のみ再蒸留、初回はテーブル空検知で 2026-07 以降を全月 backfill。"
            ),
            disable_impact=(
                "アクター辞書の行動史 (月次タイムライン) が更新停止する。"
                "記事メタが retention で消える前に蒸留されないと当該期間の史が永久に欠ける。"
            ),
            protection="important",
            schedule_type="cron",
            day_of_week="mon",
            hour=4,
            minute=20,  # 深夜バッチ帯の末尾 — 朝ブリーフ (06:30) と重ねない
            max_runtime_minutes=5,
        ),
    ]


# ---------- load / save / seed ----------

_MUTABLE_FIELDS = (
    "enabled",
    "schedule_type",
    "hour",
    "minute",
    "day_of_week",
    "day",
    "interval_minutes",
    "offset_minutes",
    "debounce_hours",
)


def load_jobs(*, db_path: Path | None = None) -> list[JobDef]:
    """全ジョブ定義を返す。code の canonical に DB override (enabled+schedule) を重ねる。"""
    defaults = {j.id: j for j in default_jobs()}
    raw = get_config(JOBS_KEY, db_path=db_path)
    overrides: dict[str, dict[str, Any]] = {}
    if isinstance(raw, list):
        for d in raw:
            if isinstance(d, dict) and isinstance(d.get("id"), str):
                overrides[d["id"]] = d
    out: list[JobDef] = []
    for jid, dj in defaults.items():
        ov = overrides.get(jid)
        if not ov:
            out.append(dj)
            continue
        update = {f: ov[f] for f in _MUTABLE_FIELDS if f in ov}
        try:
            out.append(dj.model_copy(update=update))
        except Exception as e:  # noqa: BLE001 — 破損 override は canonical に degrade
            _log.warning("job_override_invalid", job_id=jid, error=str(e))
            out.append(dj)
    return out


def get_job(job_id: str, *, db_path: Path | None = None) -> JobDef | None:
    """1 ジョブの現在定義を返す。"""
    return next((j for j in load_jobs(db_path=db_path) if j.id == job_id), None)


def save_jobs(jobs: list[JobDef], *, note: str = "UI 編集", db_path: Path | None = None) -> int:
    """全ジョブ定義を新 version として保存。返り値 = version。"""
    return save_config(
        JOBS_KEY, [j.model_dump(mode="json") for j in jobs], note=note, db_path=db_path
    )


def seed_jobs_if_absent(*, db_path: Path | None = None) -> bool:
    """DB 未投入なら default_jobs を version 1 として取り込む (起動時)。"""
    return seed_config_if_absent(
        JOBS_KEY,
        [j.model_dump(mode="json") for j in default_jobs()],
        note="初期 seed (default_jobs)",
        db_path=db_path,
    )


# ---------- 動的な収集抑止 (heavy 処理との重なりを避ける) ----------
#
# 固定の夜間解析帯 (旧 AnalysisWindow) を廃止 (2026-07-07)。固定窓は heavy が飛び飛びに
# 走る隙間でも収集を止める過剰抑制だった。代わりに、収集の発火 [now, now+C] が active な
# heavy ジョブの run 区間 [開始, 開始+max_runtime] と重なる時だけ、その発火を抑止する
# (「被った部分のみ自動停止」)。heavy を動かせば抑止も自動追随する。


def _heavy_active_on(job: JobDef, *, weekday: str, day_of_month: int) -> bool:
    """heavy cron が指定 JST 曜日/日 に発火するか (weekly=曜日 / monthly=日 / それ以外=毎日)。"""
    if job.day_of_week:
        return weekday in {d.strip().lower() for d in job.day_of_week.split(",")}
    if job.day:
        try:
            return int(job.day.strip()) == day_of_month
        except ValueError:
            return False
    return True


def is_collection_suppressed(
    job_id: str,
    *,
    now_minute_jst: int,
    weekday: str,
    day_of_month: int,
    db_path: Path | None = None,
) -> bool:
    """収集発火 [now, now+C] が active な heavy ジョブの run 区間と重なるなら True (抑止)。

    C = 収集自身の想定処理時間 (max_runtime_minutes)。重い処理の開始直前 (収集が食い込み
    heavy をブロック) と最中の双方を、被る発火だけ精密に止める。隙間では収集は走る。
    reactive heavy (時刻なし) や max_runtime 超過は単一ロックが安全網として拾う。

    Args:
        now_minute_jst: 現在の JST その日の分 (0-1439)
        weekday: 現在の JST 曜日短名 ("mon".."sun")
        day_of_month: 現在の JST 日 (1-31)
    """
    job = get_job(job_id, db_path=db_path)
    if job is None or not job.respects_analysis_window:
        return False
    coll_start = now_minute_jst
    coll_end = now_minute_jst + max(1, job.max_runtime_minutes)
    for h in load_jobs(db_path=db_path):
        if not h.heavy or h.schedule_type != "cron" or h.hour is None:
            continue
        if not _heavy_active_on(h, weekday=weekday, day_of_month=day_of_month):
            continue
        h_start = h.hour * 60 + (h.minute or 0)
        h_end = h_start + max(1, h.max_runtime_minutes)
        if coll_start < h_end and coll_end > h_start:  # 区間 overlap
            return True
    return False


def set_job_enabled(job_id: str, enabled: bool, *, db_path: Path | None = None) -> JobDef | None:
    """1 ジョブの enabled を更新し版保存。返り値 = 更新後 JobDef (不在なら None)。"""
    jobs = load_jobs(db_path=db_path)
    target = next((j for j in jobs if j.id == job_id), None)
    if target is None:
        return None
    updated = target.model_copy(update={"enabled": enabled})
    new_jobs = [updated if j.id == job_id else j for j in jobs]
    save_jobs(new_jobs, note=f"{job_id} enabled={enabled}", db_path=db_path)
    return updated


def update_job_schedule(
    job_id: str, patch: dict[str, Any], *, db_path: Path | None = None
) -> JobDef | None:
    """1 ジョブの schedule フィールドを更新し版保存。返り値 = 更新後 JobDef。"""
    jobs = load_jobs(db_path=db_path)
    target = next((j for j in jobs if j.id == job_id), None)
    if target is None:
        return None
    allowed = {k: v for k, v in patch.items() if k in _MUTABLE_FIELDS and k != "enabled"}
    updated = target.model_copy(update=allowed)
    new_jobs = [updated if j.id == job_id else j for j in jobs]
    save_jobs(new_jobs, note=f"{job_id} reschedule", db_path=db_path)
    return updated


# ---------- 検証 / ガードレール (ミス防止) ----------

_DANGER_PROXIMITY_MINUTES = 15


def _fires_every_day(j: JobDef) -> bool:
    """曜日・日指定のない cron は毎日発火する。"""
    return j.day_of_week is None and j.day is None


def _may_share_day(a: JobDef, b: JobDef) -> bool:
    """2 つの cron が同じ暦日に走りうるか (別曜日の週次同士は競合しない)。"""
    if _fires_every_day(a) or _fires_every_day(b):
        return True
    if a.day_of_week is not None and b.day_of_week is not None:
        return a.day_of_week == b.day_of_week
    if a.day is not None and b.day is not None:
        return a.day == b.day
    # 週次 vs 月次 — 稀な暦一致では警告しない
    return False


def _heavy_cron(jobs: list[JobDef]) -> list[JobDef]:
    """固定時刻を持つ重い LLM ジョブ (バンド/警告の動的ソース)。"""
    return [j for j in jobs if j.heavy and j.schedule_type == "cron" and j.hour is not None]


def validate_schedule(job: JobDef) -> str | None:
    """schedule の不正を検出しエラー文言を返す (正常なら None)。"""
    if job.schedule_type == "interval":
        if not job.interval_minutes or job.interval_minutes < MIN_INTERVAL_MINUTES:
            return f"interval は {MIN_INTERVAL_MINUTES} 分以上にしてください"
    elif job.schedule_type == "cron":
        if job.hour is None or not (0 <= job.hour <= 23):
            return "hour は 0〜23 で指定してください"
        if job.minute is None or not (0 <= job.minute <= 59):
            return "minute は 0〜59 で指定してください"
    elif job.schedule_type == "reactive" and (
        job.debounce_hours is None or job.debounce_hours <= 0
    ):
        return "debounce_hours は正の値にしてください"
    return None


def danger_window_note(job: JobDef, jobs: list[JobDef]) -> str | None:
    """cron 時刻が他の重いジョブと近接し同日に走りうるなら警告 (ブロックしない)。

    静的テーブルではなく **現在の heavy ジョブ実時刻** と突合するため、重いジョブを
    移動すると警告の基準も自動追随する (黄色バンドと同一ソース)。
    """
    if job.schedule_type != "cron" or job.hour is None:
        return None
    for other in _heavy_cron(jobs):
        if other.id == job.id:
            continue
        close = abs((job.minute or 0) - (other.minute or 0)) <= _DANGER_PROXIMITY_MINUTES
        if job.hour == other.hour and close and _may_share_day(job, other):
            return (
                f"{other.hour:02d}:{other.minute or 0:02d} 付近は「{other.title}」と"
                "重なります (重い run の相互 kill 注意)"
            )
    return None


def danger_windows(jobs: list[JobDef]) -> list[dict[str, Any]]:
    """重い LLM ジョブの現在時刻を UI の危険帯シェード用に返す (時刻変更に追随)。"""
    return [
        {
            "hour": j.hour,
            "minute": j.minute or 0,
            "label": j.title,
            "job_id": j.id,
            "day_of_week": j.day_of_week,
            "day": j.day,
            # 帯の幅 = 想定処理時間 (分)。開始時刻 (先頭) から右へこの長さ描く。
            "max_runtime_minutes": j.max_runtime_minutes,
        }
        for j in _heavy_cron(jobs)
    ]


# ---------- スケジューラへの反映 (起動時 / 編集時) ----------


def _schedule_changed(j: JobDef, d: JobDef) -> bool:
    keys = ("schedule_type", "hour", "minute", "day_of_week", "day", "interval_minutes")
    return any(getattr(j, k) != getattr(d, k) for k in keys)


def apply_schedule_to_scheduler(scheduler: Any, j: JobDef) -> None:
    """1 ジョブの schedule を稼働中スケジューラに reschedule する (cron/interval)。"""
    if j.schedule_type == "interval" and j.interval_minutes:
        scheduler.update_interval(
            j.interval_minutes, job_id=j.id, offset_minutes=j.offset_minutes or 0
        )
    elif j.schedule_type == "cron" and j.hour is not None:
        scheduler.update_cron(
            j.hour, j.minute or 0, job_id=j.id, day_of_week=j.day_of_week, day=j.day
        )


def apply_job_registry(scheduler: Any, jobs: list[JobDef]) -> None:
    """registry の enabled + schedule override を稼働中スケジューラへ反映する。

    bespoke は attach 時に registry schedule で登録済 (呼出側)。ここでは pipeline の
    schedule override 反映 + 全 scheduler job の enabled 反映 (無効=pause) を行う。
    reactive は scheduler job でないため trigger 側が enabled を見る (対象外)。
    """
    defaults = {j.id: j for j in default_jobs()}
    for j in jobs:
        if j.kind == "reactive":
            continue
        try:
            if j.kind == "pipeline":
                d = defaults.get(j.id)
                if d is not None and _schedule_changed(j, d):
                    apply_schedule_to_scheduler(scheduler, j)
            if not j.enabled:
                scheduler.pause(job_id=j.id)
        except Exception as e:  # noqa: BLE001 — 1 job の失敗で全体を止めない
            _log.warning("job_registry_apply_failed", job_id=j.id, error=str(e))
