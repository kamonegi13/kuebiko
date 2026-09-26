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

JobKind = Literal["pipeline", "bespoke", "reactive", "chain"]
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
    # kind="chain" の段 (既存ジョブ id を宣言順に直列実行、2026-09-15 ジョブ見直し B)
    steps: tuple[str, ...] = ()

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
            enabled=False,  # 2026-09-15: 毎時チェーンの段として実行 (単独発火は既定 OFF)
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
            enabled=False,  # 2026-09-15: 毎時チェーンの段として実行 (単独発火は既定 OFF)
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
            enabled=False,  # 2026-09-15: 毎時チェーンの段として実行 (単独発火は既定 OFF)
            kind="pipeline",
            title="Grok レポート取込",
            description="Grok タスクのレポートを IMAP 通知経由で取込む。",
            disable_impact="Grok の深掘り/早期シグナルが入らない。",
            protection="important",
            schedule_type="cron",
            hour=6,
            minute=0,
            # 既定 5 分では実態と合わない (実測 2-6.2 分で既に超過)。2026-08-16 に
            # 収集元タスクを「窓 90 分 × 多数」→「窓 12 時間 × 少数」へ再設計したため
            # 1 通あたりの処理量が増える見込み。夕ブリーフ直前の回が食い込まないよう余裕を取る。
            max_runtime_minutes=12,
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
            # 台帳の増分 ACH 上限を 6 → 12 に上げたぶん (+6 × 32 秒 ≒ +3.2 分) を加算。
            max_runtime_minutes=20,  # 実測 p90 13 分 + 台帳 cap 引上げ分 (2026-09-15)
            title="朝ブリーフィング",
            description=(
                "毎朝の日次総括を生成し brief チャンネルへ配信。"
                "台帳の増分再評価 (cap 12、毎時段の残余) と "
                "新規開設 (ML 前段で候補を上位 30 件に絞り LLM が選ぶ、上限 8・ML との和集合で 12) "
                "はここで走る。"
                "standing 常設情報要求の収穫 (R1-R3) + 予約枠再評価 (staleness 7日) も同じ run。"
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
            # 台帳の増分 ACH 上限を 6 → 12 に上げたぶん (+6 × 32 秒 ≒ +3.2 分) を加算。
            max_runtime_minutes=20,  # 実測 p90 13 分 + 台帳 cap 引上げ分 (2026-09-15)
            title="夕ブリーフィング",
            description=(
                "夕方の日次状況更新を生成し brief チャンネルへ配信。"
                "台帳の増分再評価 (cap 12、毎時段の残余) と "
                "新規開設 (ML 前段で候補を上位 30 件に絞り LLM が選ぶ、上限 8・ML との和集合で 12) "
                "はここで走る。"
                "standing 常設情報要求の収穫 (R1-R3) + 予約枠再評価 (staleness 7日) も同じ run。"
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
            hour=0,
            minute=55,
        ),
        JobDef(
            id="weekly-status-synthesis",
            kind="pipeline",
            heavy=True,
            max_runtime_minutes=25,
            title="週次状況総括",
            description=(
                "前週の中期情勢を総括 (分析は reasoning ティア、散文は narrative ティア)。standing "
                "常設の週次対称"
                "反証 sweep (adversarial) もここで走る。"
            ),
            disable_impact="週次の中期情勢総括が出なくなる。",
            protection="important",
            schedule_type="cron",
            day_of_week="mon",
            hour=1,
            minute=10,
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
            hour=1,
            minute=40,
        ),
        JobDef(
            id="pir-spotlight",
            kind="pipeline",
            heavy=True,
            max_runtime_minutes=25,  # 実測 p90 20 分 (2026-09-15 見直し)
            title="PIR スポットライト",
            description=(
                "PIR 縦断の narrative を毎日 04:30 に直近 7 日窓で更新 (Intel Graph の Spotlight)。"
            ),
            disable_impact="PIR 別の追跡 narrative (直近 7 日窓) が更新されない。",
            protection="optional",
            schedule_type="cron",
            hour=4,
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
            day_of_week="sun",
            hour=1,
            minute=10,
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
            hour=1,
            minute=10,
        ),
        JobDef(
            id="weekly-goldset-eval",
            kind="bespoke",
            heavy=True,
            max_runtime_minutes=90,
            title="goldset 切替評価",
            description=(
                "rubric の版が変わった週だけ、凍結 gold set で旧版 vs 新版の同一入力"
                "比較 (対照 = 新版 2 実行) を自動実行する。"
            ),
            disable_impact=(
                "rubric 変更の入力凍結評価が自動で出なくなる (本番統計の切替検証は"
                " weekly-prompt-governance が継続)。"
            ),
            protection="important",
            schedule_type="cron",
            day_of_week="sat",
            # 深夜帯の空白 (旧 03:20 の台帳夜間精査 (2026-09-10 廃止) の後の帯、
            # 06:30 朝ブリーフまで)。LLM heavy 同士を重ねない。
            hour=4,
            minute=45,
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
            hour=0,
            minute=25,
        ),
        JobDef(
            id="pir-judge-hourly",
            enabled=False,  # 2026-09-15: 毎時チェーンの段として実行 (単独発火は既定 OFF)
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
            # 単独発火の時刻 — 毎時チェーンの段なので、チェーンを無効にした rollback 時だけ使う
            offset_minutes=45,
            upkeep=True,
        ),
        JobDef(
            id="public-reachability",
            enabled=False,  # 2026-09-15: 毎時チェーンの段として実行 (単独発火は既定 OFF)
            kind="bespoke",
            title="公開面 到達性チェック",
            description=(
                "公開 URL (Cloudflare Tunnel 経由) を **HTTP/2 で** 叩き、スマホ / PWA から "
                "実際に開けるかを確認する。2026-08-24 に cloudflared の QUIC 経路が壊れ、"
                "**HTTP/2・HTTP/3 のクライアントだけ**が応答途中で切断される障害が半日続いた。"
                "ローカルは 2ms で正常・HTTP/1.1 の curl も正常だったため、"
                "**利用者の報告が唯一の検知手段**になっていた。公開していなければ何もしない。"
            ),
            disable_impact=(
                "公開面 (スマホ / PWA) が落ちても気付けなくなる。ローカルの死活監視は "
                "origin しか見ておらず、トンネル経由の到達性は誰も見ていない。"
            ),
            protection="important",
            schedule_type="interval",
            interval_minutes=60,
            # 単独発火の時刻 — 毎時チェーンの段なので、チェーンを無効にした rollback 時だけ使う
            offset_minutes=50,
            upkeep=True,
        ),
        JobDef(
            id="embedding-backfill",
            enabled=False,  # 2026-09-15: 毎時チェーンの段として実行 (単独発火は既定 OFF)
            kind="bespoke",
            title="埋込の取りこぼし補完",
            description=(
                "埋込を持たない記事を毎時埋める。埋込は意味的 dedup の判定時にしか生成されず、"
                "保存は投稿確定の経路でしか行われないため、そこを通らない記事は埋込を永久に"
                "持たない。**Grok (x.com) は 906 記事すべてが該当** し、事象ニュースの群化に"
                "一度も参加できていなかった (既読化が親レポート URL で行われ、ツイート URL が"
                "dedup_seen_urls に入らないため、既存 backfill script も構造的に拾えない)。"
                "毎時収集チェーンで事象ニュース生成の前の段に置く。"
            ),
            disable_impact=(
                "Grok の投稿と、投稿経路を通らない記事が事象ニュースに合流できなくなる "
                "(単独記事としてしか出ない)。意味的 dedup の判定材料も欠ける。"
            ),
            protection="important",
            schedule_type="interval",
            interval_minutes=60,
            offset_minutes=10,
            upkeep=True,
        ),
        JobDef(
            id="eventnews-hourly",
            enabled=False,  # 2026-09-15: 毎時チェーンの段として実行 (単独発火は既定 OFF)
            max_runtime_minutes=25,  # 実測 18.6 分 (30 層モデルで 4 件生成、2026-09-15)
            kind="bespoke",
            title="事象ニュース 毎時更新",
            description=(
                "収集済み記事を事象単位に群化し、複数媒体が報じた事象について 1 本の "
                "ニュースを生成・更新する (docs/event_news_design.md)。生成はメンバー 2 件 "
                "以上のアイテムのみ (単独記事は per-article 要約をそのまま読ませる)。"
                "生成は narrative ティア。事象ニュースは UI の主導線 (2026-08-24〜)。"
                "EVENTNEWS_HOURLY=0 で完全停止。"
            ),
            disable_impact=(
                "事象の群化と更新が止まる。記事は従来どおり per-article で表示されるため "
                "配信への影響は無い (v1 時点では読み手向けの出口が無いため実害ゼロ)。"
            ),
            protection="optional",
            schedule_type="interval",
            interval_minutes=60,
            # 単独発火の時刻 — 毎時チェーンの段なので、チェーンを無効にした rollback 時だけ使う
            offset_minutes=20,
            upkeep=True,
        ),
        JobDef(
            id="body-translate-backlog",
            enabled=False,  # 2026-09-15: 毎時チェーンの段として実行 (単独発火は既定 OFF)
            kind="bespoke",
            title="本文自動翻訳",
            description=(
                "未訳記事 (body あり・body_ja なし) を新しい順に 40 件/時までローカル LLM "
                "(fast ティア) で全訳し body_ja へキャッシュする常設ジョブ。本務は新規流入 "
                "(~470 件/日) の当日中の自動翻訳で、過去分バックログは残余キャパで漸進消化 "
                "(消化後も新規向けに恒久稼働)。時間予算 12 分 (毎時保守チェーンの先頭の段)。"
                "無効化すると記事詳細の「日本語訳」ボタンによる on-demand 専用に戻る。"
            ),
            disable_impact=(
                "新規記事の日本語訳が事前生成されなくなる (記事詳細で押した時だけの "
                "on-demand 翻訳に戻る。既訳キャッシュはそのまま残る)。"
            ),
            protection="optional",
            schedule_type="interval",
            interval_minutes=60,
            # 単独発火の時刻 — 毎時チェーンの段なので、チェーンを無効にした rollback 時だけ使う
            offset_minutes=15,
            respects_analysis_window=True,  # 夜間解析帯は Ollama を奪い合わない
            max_runtime_minutes=15,  # 時間予算 12 分 + 余裕
            upkeep=True,
        ),
        JobDef(
            id="body-refetch-backlog",
            enabled=False,  # 2026-09-15: 毎時チェーンの段として実行 (単独発火は既定 OFF)
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
            # 単独発火の時刻 — 毎時チェーンの段なので、チェーンを無効にした rollback 時だけ使う
            offset_minutes=40,
            respects_analysis_window=True,  # 夜間解析帯は Ollama を奪い合わない
            max_runtime_minutes=15,
            upkeep=True,
        ),
        JobDef(
            id="eventnews-merge",
            enabled=False,  # 毎時収集チェーンの段として実行 (単独発火は既定 OFF)
            kind="bespoke",
            title="事象ニュース 事象どうしの統合 (毎時)",
            description=(
                "既にできた事象どうしを記事 × 記事で突き合わせ、同じ出来事なら最初に立った"
                "事象へ統合する (毎時の群化は「1 記事 × 1 事象」しか見ないため、長期化する"
                "事案ほど割れていた)。判定は ML のみ (LLM なし)、辺 2 本以上で結ぶ (一括勧告"
                "のハブ対策)。統合先の本文は消えるので、直後に上限つき (既定 5 件/時) で"
                "再生成する。EVENTNEWS_MERGE=0 で停止、EVENTNEWS_MERGE_REGEN_CAP で上限。"
            ),
            disable_impact=(
                "割れた事象が統合されず、続報が別事象として並び続ける。本文なしの事象"
                "(統合・分割の後始末) も再生成されない。"
            ),
            protection="optional",
            schedule_type="interval",
            interval_minutes=60,
            offset_minutes=25,
            upkeep=True,
            max_runtime_minutes=20,  # 読込 ≈ 2 分 (全事象 11k) + 再生成 5 件 × 40-600 秒 (中央 120)
        ),
        JobDef(
            id="severity-axes-hourly",
            enabled=False,  # 毎時保守チェーンの段として実行 (単独発火は既定 OFF)
            kind="bespoke",
            title="深刻度の軸 (毎時)",
            description=(
                "配信済み high/medium の記事に深刻度の軸 (被害の広がり・被害の性質・実害の確認・"
                "悪用状況・行為者・標的) を 40 件/時まで付ける。detect ML の特徴量で、日本の"
                "小さな事案と追うべき事案を分ける (2026-09-25)。朝夕の detect にも穴埋めはあるが、"
                "まとめて付けると数分かかるため毎時に分散させる。"
                "SEVERITY_AXES_HOURLY=0 で停止、SEVERITY_AXES_HOURLY_CAP で件数。"
            ),
            disable_impact=(
                "軸が朝夕の detect の穴埋め (40 件/run) だけになり、超過分は軸なしで採点される"
                " (日本の小さな事案を開きやすくなる)。"
            ),
            protection="important",
            schedule_type="interval",
            interval_minutes=60,
            offset_minutes=40,
            max_runtime_minutes=5,
            upkeep=True,
        ),
        JobDef(
            id="ledger-reassess-hourly",
            enabled=False,  # 毎時保守チェーンの段として実行 (単独発火は既定 OFF)
            kind="bespoke",
            title="台帳 増分再評価 (毎時)",
            description=(
                "証拠が付いた既存の台帳 (Situation) を優先度順に 6 件/時まで増分 ACH で再評価する "
                "(開設はしない)。朝夕の定時 run だけでは cap 12 で毎 run 20-75 件を繰越していた "
                "(2026-09-17 実測 backlog 30-87) ため、毎時で掃いて"
                "「更新すべき台帳は全部更新される」状態に近づける。"
                "LEDGER_REASSESS_HOURLY=0 で停止、LEDGER_REASSESS_HOURLY_CAP で件数。"
            ),
            disable_impact="台帳の再評価が朝夕の cap 12 だけに戻り、繰越が再び積み上がる。",
            protection="important",
            schedule_type="interval",
            interval_minutes=60,
            offset_minutes=45,
            respects_analysis_window=True,
            max_runtime_minutes=8,
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
            hour=1,
            minute=35,
        ),
        JobDef(
            id="nvd-cvss-refresh",
            enabled=False,  # 2026-09-15: 毎時チェーンの段として実行 (単独発火は既定 OFF)
            kind="bespoke",
            title="CVSS 補給 (NVD)",
            description=(
                "直近記事の CVE について NVD から CVSS を bounded に取得し cache を温める。"
                "routing の深刻度ゲート (max_cvss) と記事表示が参照する。"
                "従来は app 起動時に 8 件だけだったため未取得が 1,700 件超に滞留し、"
                "深刻度で alert を絞る判定が事実上機能していなかった (2026-08-15)。"
            ),
            disable_impact=(
                "CVSS が埋まらず、深刻度ゲートの alert 判定が「不明」に倒れて "
                "重大脆弱性を watch に落とす (取りこぼし方向の劣化)。"
            ),
            protection="optional",
            schedule_type="interval",
            # 未取得 CVE を毎時少しずつ消化する定常キュー処理。実行時刻に運用上の意味は
            # 無い (:50 は収集ジョブと衝突しないだけ) ので専用 swimlane は持たせない。
            upkeep=True,
            # 毎時 (NVD レート制限を尊重し 1 回あたり少量)。収集ジョブと衝突しない :50。
            interval_minutes=60,
            offset_minutes=50,
        ),
        JobDef(
            id="daily-maintenance",
            kind="bespoke",
            title="日次 DB 保守",
            description="ログ/dedup/body/detection の retention purge 一式 (夜間)。",
            disable_impact="DB が無限成長する (retention 停止)。",
            protection="critical",
            schedule_type="cron",
            hour=0,
            minute=10,
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
            id="weekly-triage-drift",
            kind="bespoke",
            heavy=True,
            max_runtime_minutes=60,
            title="triage ドリフト週次検知",
            description=(
                "凍結 goldset (150 記事・本番と同一プロンプト) を現在の fast ティア (triage 担当)"
                " に通し、"
                "day-0 (2026-09-03) の判定からの移動率で警告する (150 呼出)。"
            ),
            disable_impact=(
                "triage の静かな劣化 (プロンプト/PIR/モデル変更の副作用) を"
                "手動実行しない限り誰も見なくなる。day-0 実測: Sonnet 対比で"
                "見逃し方向 0 件の健全状態が基準。"
            ),
            protection="important",
            schedule_type="cron",
            day_of_week="sun",
            # 深夜帯の空白 (土 04:45 の goldset 評価と重ねない)。LLM heavy。
            hour=5,
            minute=0,
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
            id="weekly-prompt-governance",
            kind="bespoke",
            title="プロンプト統治 週次監査",
            description=(
                "判定基準の禁止事項が実データで守られているか (audit_prompt_prohibitions) と、"
                "最新 rubric 切替の合否 (verify_prompt_cutover、窓の純度ゲート込み) を"
                "週次で検査し ops へ 1 通投稿する (LLM 不使用の決定論)。"
            ),
            disable_impact=(
                "禁止事項の関門漏れ (victim_org ベンダ混入型) と rubric 切替の劣化を"
                "手動実行しない限り誰も見なくなる。"
            ),
            protection="important",
            schedule_type="cron",
            day_of_week="mon",
            hour=8,
            minute=20,  # weekly-fill-rate-audit (08:10) に続く月曜朝の ops 監査クラスタ
            max_runtime_minutes=10,
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
            hour=1,
            minute=45,
            max_runtime_minutes=5,
        ),
        # ---------- 毎時チェーン (2026-09-15 ジョブ見直し B) ----------
        # 11 本の interval ジョブをモデル順に並べた直列 2 本へ。段の失敗は隔離、段ごとに所要を記録。
        # 段の単独ジョブは registry に残す (rollback = 段を enabled、チェーンを disabled)。
        JobDef(
            id="hourly-collect",
            kind="chain",
            title="毎時収集チェーン",
            description=(
                "毎時の収集と派生処理を 1 本で直列実行: RSS → sitemap 監視 → Grok → 埋込 → "
                "PIR 判定 → 事象ニュース → 事象どうしの統合。fast ティアの段を先に、"
                "narrative ティアの段を最後に置き、Ollama のモデル切替を 1 時間に 1 回へ"
                "抑える (旧: 11 ジョブがオフセットで並び切替 34 回/日・重なりあり)。"
            ),
            disable_impact="毎時の収集・事象ニュースが全て止まる (段の単独ジョブは既定 OFF)。",
            protection="critical",
            schedule_type="interval",
            interval_minutes=60,
            offset_minutes=0,
            max_runtime_minutes=58,  # 段の合計 (実測 rss 12 + 事象 19 + 統合 12 + 他 4) + 余裕
            steps=(
                "direct-rss-fetch",
                "web-scraper-watchers",
                "grok-briefing",
                "embedding-backfill",
                "pir-judge-hourly",
                "eventnews-hourly",
                "eventnews-merge",  # 群化の後、同じ narrative ティアで再生成まで (2026-09-21)
            ),
        ),
        JobDef(
            id="hourly-upkeep",
            kind="chain",
            title="毎時保守チェーン",
            description=(
                "本文の翻訳バックログ → 本文の再取得 → 深刻度の軸 (40 件/時) → 台帳の増分再評価 "
                "(6 件/時) → NVD CVSS 補充 "
                "→ "
                "公開 URL の到達性を 1 本で直列実行 (fast ティアと I/O のみ)。収集チェーンの後半 "
                "(:30) に置く。"
            ),
            disable_impact="翻訳・再取得・CVSS・到達性監視が止まる。",
            protection="important",
            upkeep=True,
            schedule_type="interval",
            interval_minutes=60,
            offset_minutes=30,
            max_runtime_minutes=25,
            steps=(
                "body-translate-backlog",
                "body-refetch-backlog",
                "severity-axes-hourly",
                "ledger-reassess-hourly",
                "nvd-cvss-refresh",
                "public-reachability",
            ),
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


def chain_membership(jobs: list[JobDef]) -> dict[str, JobDef]:
    """段の job id → それを含むチェーン。チェーンに属さないジョブは含まない。

    段の単独ジョブは registry に残っているが (rollback 用)、チェーンが有効な間は
    時刻も ON/OFF もチェーンが持つ。画面・API・watchdog はこの対応で段を判定する。
    """
    out: dict[str, JobDef] = {}
    for j in jobs:
        if j.kind == "chain":
            for sid in j.steps:
                out.setdefault(sid, j)
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
    for j in jobs:
        if j.kind == "reactive":
            continue
        try:
            # pipeline は config/pipelines.yaml の schedule で登録されるため、registry の時刻を
            # **常に** 上書きする (2026-09-26)。旧実装は「既定値と違うときだけ」上書きしていて、
            # 既定値を実時刻に揃えると yaml の古い時刻が残る罠があった (registry が SSoT)。
            if j.kind in ("pipeline", "chain"):
                apply_schedule_to_scheduler(scheduler, j)
            if not j.enabled:
                scheduler.pause(job_id=j.id)
        except Exception as e:  # noqa: BLE001 — 1 job の失敗で全体を止めない
            _log.warning("job_registry_apply_failed", job_id=j.id, error=str(e))
