"""残りの全 Jinja page を置き換える JSON API (history / prompts / config /
schedule / subscriptions / health / stix / taxonomy-review / editorial-quality)。"""

from __future__ import annotations

import contextlib
import csv
import importlib
import io
import re
from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Form, HTTPException, Query, Request, Response
from pydantic import BaseModel, ValidationError

from src.config_loader import (
    PipelineConfig,
    ScraperEntry,
    load_app_config,
    load_pipelines,
)
from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository
from src.ui.services.env_editor import (
    EnvEditError,
    allowed_env_keys,
    displayed_env_keys,
    env_key_descriptions,
    mask_value,
    parse_env,
    secret_env_keys,
    update_env,
)
from src.ui.services.file_editor import EditError, FileEditor
from src.ui.services.health import run_all_checks
from src.ui.services.pipelines_editor import PipelinesEditError, get_pipelines_editor
from src.ui.services.subscription_analytics import fetch_all_feed_stats

_log = get_logger(__name__)

pages_api = APIRouter(prefix="/api/v1", tags=["pages"])


# ---------- /history ----------


@pages_api.get("/history")
async def history_list(
    request: Request,
    importance: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    repo: RunHistoryRepository = request.app.state.repo
    articles = repo.list_articles(
        importance=importance or None,
        status=status or None,
        limit=max(1, min(limit, 500)),
    )
    recent = repo.list_runs(limit=10)
    return {
        "articles": [
            {
                "id": a.id,
                "run_id": a.run_id,
                "article_id": a.article_id,
                "title": a.title,
                "url": a.url,
                "feed_title": a.feed_title,
                "importance": a.importance,
                "category": a.category,
                "status": a.status,
                "posted_channel": a.posted_channel,
                "published_at": a.published_at.isoformat() if a.published_at else None,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in articles
        ],
        "recent_runs": [
            {
                "id": r.id,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "pipeline": r.pipeline,
                "status": r.status,
                "posted": r.posted,
                "error_count": r.error_count,
                "dry_run": r.dry_run,
            }
            for r in recent
        ],
        "filters": {"importance": importance or "", "status": status or ""},
    }


@pages_api.post("/history/{run_id}/delete")
async def history_delete(request: Request, run_id: int) -> dict[str, Any]:
    repo: RunHistoryRepository = request.app.state.repo
    deleted = repo.delete_run(run_id)
    if not deleted:
        raise HTTPException(
            status_code=409,
            detail=f"実行 {run_id} は削除できません (存在しないか実行中です)",
        )
    return {"deleted": True, "run_id": run_id}


@pages_api.post("/history/purge")
async def history_purge(request: Request, days: int = Form(default=30)) -> dict[str, Any]:
    """N 日より古い run_logs (ライブログ) を purge する (Phase 0 F3, non-destructive)。

    旧実装は delete_runs_older_than で runs を削除し articles を FK CASCADE で連鎖削除して
    いた (1 クリックで過去参照コーパスが全損)。bulky な run_logs のみ削除し、runs/articles
    は保持する。記憶 (articles) は明示削除以外では消えない。
    """
    if days < 0:
        raise HTTPException(status_code=400, detail="days は 0 以上必須")
    repo: RunHistoryRepository = request.app.state.repo
    count = repo.purge_old_logs(days)
    return {"deleted_count": count, "days": days, "target": "run_logs"}


# ---------- /access-audit (認証の監査証跡、2026-08-02) ----------


@pages_api.get("/access-audit")
async def access_audit(limit: int = 200) -> dict[str, Any]:
    """Cloudflare Access (Tier1) の認証イベントを新しい順に返す。

    公開 instance からは READ_ONLY_GET_DENYLIST で遮断され、ローカル full instance と
    認証済み Tier1 からのみ読める (監査証跡自体が公開面に出ないようにする)。
    email は保存していないため、識別は subject_hash (SHA-256 先頭 12 桁)。
    """
    from src.storage.run_history import RunHistoryRepository
    from src.ui.services.cf_access import load_access_config

    # Access の設定状態も返す (UI が「未設定なので履歴が無い」と「設定済みで未使用」を
    # 区別できるようにする。**AUD は秘密ではないが値は返さない** — 設定済み boolean のみ)。
    cfg = load_access_config()
    auth = {
        "configured": cfg is not None,
        "team_domain": cfg.team_domain if cfg else "",
    }
    try:
        rows = RunHistoryRepository().list_access_audit(limit=min(max(limit, 1), 1000))
    except Exception as e:  # noqa: BLE001 — 監査参照の失敗で画面を落とさない
        _log.warning("access_audit_list_failed", error=str(e))
        return {"events": [], "count": 0, "auth": auth, "error": "監査証跡を読み出せませんでした"}
    return {"events": rows, "count": len(rows), "auth": auth}


# ---------- /ops-notices (ops 通知の永続化、2026-08-21) ----------


@pages_api.get("/ops-notices")
async def ops_notices(request: Request, limit: int = 50) -> dict[str, Any]:
    """post_ops_message が送った ops 通知を新しい順に返す (設定 → 履歴・監査タブ用)。

    webhook 不達・未設定でも DB には必ず 1 行残る (src/ui/services/ops_notify.py)。
    運用系の観測データのため公開 instance からは READ_ONLY_GET_DENYLIST で遮断される。
    """
    repo: RunHistoryRepository = request.app.state.repo
    try:
        notices = repo.list_ops_notices(limit=min(max(limit, 1), 500))
    except Exception as e:  # noqa: BLE001 — 参照の失敗で画面を落とさない
        _log.warning("ops_notices_list_failed", error=str(e))
        return {"notices": [], "count": 0, "error": "ops 通知を読み出せませんでした"}
    return {"notices": notices, "count": len(notices)}


# ---------- /runtime-flags (Phase Diamond verify-mobile) ----------


@pages_api.get("/runtime-flags")
async def runtime_flags(request: Request) -> dict[str, Any]:
    """フロントが起動時に取得する runtime config。

    - ``read_only``: write button を hide する (2 instance 構成の公開側)。
    - ``authenticated`` / ``auth_available``: Cloudflare Access の Tier1 状態
      (2026-08-01)。認証済みなら fullOnly ページの閲覧と即時実行を UI で解放する。
      判定の実体は middleware (src/ui/read_only_policy.py) 側にあり、ここは表示用。
    """
    import os as _os

    from src.ui.read_only_policy import request_auth_state

    read_only = _os.environ.get("READ_ONLY", "0").strip() in ("1", "true", "yes", "on")
    authenticated, auth_available = request_auth_state(request)
    return {
        "read_only": read_only,
        "authenticated": authenticated,
        "auth_available": auth_available,
    }


# ---------- /mobile-tunnel (Phase Diamond verify-mobile) ----------
# tunnel sidecar との flag file 経由通信。
# 詳細は docker-compose.yml の tunnel service コメント参照。

from src.tools import mobile_tunnel_files as mtf  # noqa: E402 (mobile tunnel 契約 SSoT)

# named tunnel token / hostname の境界検証 (data/ ファイルに保存する前に検証する)。
_TUNNEL_TOKEN_RE = re.compile(r"^[A-Za-z0-9+/=._-]+$")
_TUNNEL_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9.-]+$")
_TUNNEL_TOKEN_MIN = 40
_TUNNEL_TOKEN_MAX = 4096
_TUNNEL_HOSTNAME_MAX = 253


def _mobile_tunnel_state() -> dict[str, Any]:
    """flag / URL / named-config の状態を返す。

    **token の生値は決して含めない**: readonly instance がこの GET を外部公開するため、
    boolean (``token_set``) と public hostname のみ返す (過去の secret leak 教訓)。
    """
    url: str | None = None
    if mtf.URL_FILE.exists():
        try:
            content = mtf.URL_FILE.read_text(encoding="utf-8").strip()
            url = content or None
        except OSError:
            url = None
    return {
        "enabled": mtf.is_enabled(),
        "url": url,
        "mode": mtf.mode(),  # "named" | "quick" | "off"
        "hostname": mtf.read_hostname(),  # public hostname (非秘密)
        "token_set": mtf.is_token_set(),  # boolean のみ (生値は返さない)
    }


@pages_api.get("/mobile-tunnel/status")
async def mobile_tunnel_status() -> dict[str, Any]:
    """現在の tunnel 状態を返す。

    Returns:
        {"enabled": bool, "url": str|None, "mode": str, "hostname": str|None, "token_set": bool}
        - mode: "named" (固定 URL) / "quick" (URL 変動) / "off"
        - token_set: named tunnel token が設定済みか (**生値は返さない**)
    """
    return _mobile_tunnel_state()


@pages_api.post("/mobile-tunnel/enable")
async def mobile_tunnel_enable() -> dict[str, Any]:
    """flag file を作成 → supervisor が cloudflared を起動する。"""
    mtf.ENABLED_FLAG_FILE.parent.mkdir(parents=True, exist_ok=True)
    mtf.ENABLED_FLAG_FILE.touch()
    return _mobile_tunnel_state()


@pages_api.post("/mobile-tunnel/disable")
async def mobile_tunnel_disable() -> dict[str, Any]:
    """flag file を削除 → supervisor が cloudflared を停止する。"""
    with contextlib.suppress(FileNotFoundError):
        mtf.ENABLED_FLAG_FILE.unlink()
    return _mobile_tunnel_state()


# ---------- /host-watchdog (ホスト常駐 watchdog、2026-08-02) ----------


def _host_watchdog_state() -> dict[str, Any]:
    """watchdog の導入状況と直近の稼働状態を返す。

    `installed` は LaunchAgent の有無ではなく **watchdog が一度でも動いた証拠**
    (state.json) で判定する — UI はコンテナ内なので launchctl を見られないため。
    """
    from src.tools import host_watchdog_files as hwf

    state = hwf.read_state()
    return {
        "enabled": hwf.is_enabled(),
        "installed": bool(state),  # 一度でも実行された = LaunchAgent 導入済み
        "checked_at": state.get("checked_at", ""),
        "status": state.get("status", ""),  # healthy / degraded / recovered / ...
        "detail": state.get("detail", ""),
        "consecutive_failures": state.get("consecutive_failures", 0),
        "last_recovery_at": state.get("last_recovery_at", ""),
        "last_recovery_result": state.get("last_recovery_result", ""),
        "log_tail": hwf.read_log_tail(limit=20),
    }


@pages_api.get("/host-watchdog/status")
async def host_watchdog_status() -> dict[str, Any]:
    """ホスト復旧 watchdog の状態 (導入済みか / 有効か / 直近の稼働と復旧履歴)。"""
    return _host_watchdog_state()


@pages_api.post("/host-watchdog/enable")
async def host_watchdog_enable() -> dict[str, Any]:
    """flag file を作成 → ホスト側 watchdog が次回起動時に有効と判断する。"""
    from src.tools import host_watchdog_files as hwf

    hwf.set_enabled(True)
    return _host_watchdog_state()


@pages_api.post("/host-watchdog/disable")
async def host_watchdog_disable() -> dict[str, Any]:
    """flag file を削除 → ホスト側 watchdog は即 no-op で終了する。"""
    from src.tools import host_watchdog_files as hwf

    hwf.set_enabled(False)
    return _host_watchdog_state()


class _NamedTunnelConfigIn(BaseModel):
    """named tunnel 設定の保存リクエスト。token None/空 は既存維持 (secret 空送信 skip 規約)。"""

    token: str | None = None
    hostname: str | None = None


@pages_api.post("/mobile-tunnel/named-config")
async def mobile_tunnel_set_named_config(body: _NamedTunnelConfigIn) -> dict[str, Any]:
    """named tunnel の token / hostname を data/ ファイルに保存する。

    - **full instance 限定**: readonly instance は write middleware が全 POST を 403。
    - token: None または空文字は「既存維持」(secret の空送信 skip 規約)。値は 0600 で保存し
      **API では二度と返さない** (mode / token_set / hostname のみ返す)。
    - 保存後、launcher がファイル mtime を検知して cloudflared を自動再起動 (再デプロイ不要)。
    """
    if body.token is not None and body.token.strip():
        token = body.token.strip()
        if not (_TUNNEL_TOKEN_MIN <= len(token) <= _TUNNEL_TOKEN_MAX) or not _TUNNEL_TOKEN_RE.match(
            token
        ):
            raise HTTPException(status_code=400, detail="tunnel token の形式が不正です")
        mtf.write_token(token)
    if body.hostname is not None:
        host = body.hostname.strip().removeprefix("https://").removeprefix("http://").strip("/")
        if host and (len(host) > _TUNNEL_HOSTNAME_MAX or not _TUNNEL_HOSTNAME_RE.match(host)):
            raise HTTPException(status_code=400, detail="hostname の形式が不正です")
        mtf.write_hostname(host)  # 空文字は clear
    return _mobile_tunnel_state()


@pages_api.post("/mobile-tunnel/named-config/clear")
async def mobile_tunnel_clear_named_config() -> dict[str, Any]:
    """named tunnel 設定 (token + hostname) を削除して quick tunnel に戻す。"""
    mtf.clear_token()
    mtf.clear_hostname()
    return _mobile_tunnel_state()


# ---------- /health ----------


@pages_api.get("/health-status")
async def health_status() -> dict[str, Any]:
    """run_all_checks の結果を JSON で返す。"""
    try:
        config = load_app_config()
        checks = await run_all_checks(config)
    except Exception as e:  # noqa: BLE001
        return {"load_error": str(e), "checks": []}
    return {
        "checks": [c.model_dump() for c in checks],
        "load_error": None,
    }


# ---------- /export (Phase 2.5 A1: CSV export) ----------

_EXPORT_CSV_COLUMNS = [
    "article_id",
    "created_at",
    "published_at",
    "importance",
    "category",
    "feed_title",
    "posted_channel",
    "victim_sector",
    "victim_country",
    "socio_political_intent",
    "editorial_stance",
    "title",
    "url",
    "summary",
]
_EXPORT_MAX_ROWS = 10000
_EXPORT_SUMMARY_MAX = 1000


@pages_api.get("/export/articles.csv")
async def export_articles_csv(
    request: Request,
    since_days: int = Query(default=90, ge=0, le=3650),
    importance: str | None = Query(default=None),
    category: str | None = Query(default=None),
    status: str = Query(default="posted"),
    limit: int = Query(default=_EXPORT_MAX_ROWS, ge=1, le=_EXPORT_MAX_ROWS),
) -> Response:
    """記事 (enrichment 付き) を CSV で download する (Phase 2.5 A1)。

    Excel / pandas での二次分析用。本文 (body) は重いので除外し、要約・Diamond 軸・
    被害・チャンネル等の表形式メタを出力。read-only (readonly instance でも安全)。
    STIX (機械取込) と CSV (人/表計算) で export 経路を補完する。
    """
    repo: RunHistoryRepository = request.app.state.repo
    since = datetime.now(UTC) - timedelta(days=since_days) if since_days > 0 else None
    imp = importance if importance in ("high", "medium", "low") else None
    cat = category.strip() if category and category.strip() else None

    articles = repo.list_articles(
        importance=imp,
        category=cat,
        status=status or None,
        since=since,
        limit=limit,
    )

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_EXPORT_CSV_COLUMNS)
    for a in articles:
        writer.writerow(
            [
                a.article_id,
                a.created_at.isoformat() if a.created_at else "",
                a.published_at.isoformat() if a.published_at else "",
                a.importance or "",
                a.category or "",
                a.feed_title or "",
                a.posted_channel or "",
                a.victim_sector_canonical or "",
                a.victim_country_iso or "",
                a.socio_political_intent or "",
                a.editorial_stance or "",
                a.title or "",
                a.url or "",
                (a.summary or "")[:_EXPORT_SUMMARY_MAX],
            ]
        )
    csv_text = buf.getvalue()
    ts = datetime.now(UTC).strftime("%Y%m%d")
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="cti_articles_{ts}.csv"'},
    )


# ---------- /subscriptions ----------


@pages_api.get("/subscriptions")
async def subscriptions_list() -> dict[str, Any]:
    """購読ソース list を返す (全 transport + 無効ソースも含む)。

    Phase F+: _source_manager で feeds.yaml(rss) / scrapers.yaml(html_scraper) /
    watchers.yaml(sitemap) を transport 透過に列挙。enable/disable を機能させる
    ため無効ソースも返し、frontend で enabled フラグにより区別表示する。
    """
    from dataclasses import asdict

    from src.cti.source_basis import classify_source_tier
    from src.ui.api._source_manager import list_sources

    subs: list[dict[str, Any]] = []
    feeds_error: str | None = None
    try:
        for s in list_sources():
            subs.append(
                {
                    "feed_id": s.feed_id,
                    "title": s.title,
                    "url": s.url,
                    "html_url": None,
                    "folder_labels": [s.folder] if s.folder else [],
                    "transport": s.transport,
                    "enabled": s.enabled,
                    # S3: briefing 信頼度ティア (override → pattern の実効値)
                    "reliability_tier": classify_source_tier(s.title or "", s.url or ""),
                }
            )
    except Exception as e:  # noqa: BLE001
        _log.warning("subscriptions_list_failed", error=f"{type(e).__name__}: {e}")
        feeds_error = "ソース一覧の読み込みに失敗しました"
    stats_map = fetch_all_feed_stats(lookback_days=30)
    # Phase C: quality_score + low_contrib_labels (computed property) を含める
    serialized: dict[str, dict[str, Any]] = {}
    for k, v in stats_map.items():
        d = asdict(v)
        d["quality_score"] = v.quality_score
        d["low_contrib_labels"] = v.low_contrib_labels
        d["dup_rate"] = round(v.dup_rate, 3)
        d["watch_only_rate"] = round(v.watch_only_rate, 3)
        # 本文完全性 (2026-07-27): ソース別の全文取得率 / 切り株率。
        d["full_body_rate"] = round(v.full_body_rate, 3)
        d["stump_rate"] = round(v.stump_rate, 3)
        serialized[k] = d

    # 結合キーの救済 (2026-07-12): 記事に記録された feed_url が購読 URL と異なる source
    # (例: NICTER = sitemap 購読だが記事は index.xml + 別大文字小文字の title で記録) を、
    # feed_title の大文字小文字無視一致で購読 URL キーへ別名として張る。frontend は不変。
    try:
        from src.ui.services.subscription_analytics import feed_title_key_index

        title_index = feed_title_key_index(lookback_days=30)
        for s_row in subs:
            if s_row["url"] in serialized or (s_row["title"] or "") in serialized:
                continue
            skey = title_index.get((s_row["title"] or "").strip().lower())
            if skey and skey in serialized:
                serialized[s_row["url"]] = serialized[skey]
    except Exception as e:  # noqa: BLE001 — 別名付与は補助、失敗しても一覧は返す
        _log.warning("subscriptions_stats_alias_failed", error=str(e))

    # 死活 (source_fetch_health) を購読単位で結合 (source_key = 購読 URL)。
    # 「stats なし」を『取得エラー / 取得OK・新着なし』に分解表示するための観測点
    # (2026-07-12: stats なし = 5 実態の混合で誤解を生んでいた根治)。
    try:
        repo = RunHistoryRepository()
        health_by_key = {h.source_key: h for h in repo.list_source_fetch_health()}
        for s_row in subs:
            h = health_by_key.get(s_row["url"])
            s_row["health"] = (
                {
                    "consecutive_failures": h.consecutive_failures,
                    "last_ok_at": h.last_ok_at or "",
                    "last_error_at": h.last_error_at or "",
                    "last_article_count": h.last_article_count,
                    "last_error": (h.last_error or "")[:160],
                }
                if h is not None
                else None
            )
    except Exception as e:  # noqa: BLE001
        _log.warning("subscriptions_health_join_failed", error=str(e))

    return {
        "subscriptions": subs,
        "sources_error": feeds_error,
        "stats": serialized,
    }


@pages_api.post("/subscriptions/reliability")
async def subscriptions_set_reliability(
    request: Request,
    feed_url: str = Form(default=""),
    title: str = Form(default=""),
    tier: str = Form(default="auto"),  # official/research/news/social or auto(=解除)
) -> dict[str, Any]:
    """S3: source の信頼度ティア上書きを設定/解除する (SubscriptionsPage 専用、raw YAML 不要)。"""
    from src.cti.source_basis import classify_source_tier, set_source_reliability_override

    key = feed_url.strip() or title.strip()
    if not key:
        raise HTTPException(status_code=400, detail="feed_url または title が必要です")
    try:
        applied = set_source_reliability_override(key, tier)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {
        "applied": applied,  # 適用後の上書き状態 (auto=解除)
        "reliability_tier": classify_source_tier(title, feed_url),  # 実効ティア
    }


# ---------- /prompts ----------


def _list_prompts(editor: FileEditor) -> list[str]:
    """raw 編集対象の一覧。**FileEditor.list_prompts に委譲** (骨格除外の SSoT を複製しない)。

    2026-08-20: 旧実装はここで独自 rglob しており、file_editor 側の skeleton 除外が
    効かず一覧に骨格が混入していた (列挙が 2 箇所 = 関門の複製漏れの典型)。
    """
    base = editor.env_path().parent
    return sorted(str(p.relative_to(base)) for p in editor.list_prompts())


@pages_api.get("/prompts")
async def prompts_list(request: Request) -> dict[str, Any]:
    editor: FileEditor = request.app.state.file_editor
    from src.ui.services.file_catalog import (
        PROMPT_CATALOG,
        PROMPT_CATEGORY_ORDER,
        annotate_files,
    )

    files = _list_prompts(editor)
    return {
        "files": files,
        "groups": annotate_files(files, PROMPT_CATALOG, PROMPT_CATEGORY_ORDER),
    }


@pages_api.get("/prompts/file")
async def prompts_file(request: Request, path: str) -> dict[str, Any]:
    editor: FileEditor = request.app.state.file_editor
    try:
        content = editor.read_file(path, kind="prompt")
        backup_path = (editor.env_path().parent / path).with_suffix(".j2.bak")
        return {
            "path": path,
            "content": content,
            "backup_exists": backup_path.exists(),
        }
    except EditError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


_SUMMARIZER_TEMPLATE_PATH = "prompts/briefing/summarizer.j2"


def _summarizer_rollback_notice(path: str) -> dict[str, Any]:
    """summarizer.j2 を保存しても構造化編集が有効な間は無効であることを警告する。

    判定基準 (rubric) の層分け (2026-08-16) 以降、判定基準の実体は DB 側の合成
    テンプレートであり、この .j2 は rollback 用の据置コピーにすぎない。合成が
    実際に有効か (``active_source``) を判定してから notice を出す — env flag だけを
    見ると、合成が壊れて legacy にフォールバック中の状態を誤って「無効」と伝えてしまう。
    """
    if path != _SUMMARIZER_TEMPLATE_PATH:
        return {}
    from src.prompts.rubric_store import build_summarizer_template
    from src.prompts.summarizer_composer import LEGACY_TEMPLATE_PATH

    if build_summarizer_template(LEGACY_TEMPLATE_PATH) is None:
        return {}
    return {
        "effective": False,
        "notice": "構造化編集が有効なため、このファイルは現在 LLM に使われません (rollback 用)",
    }


@pages_api.post("/prompts/save")
async def prompts_save(
    request: Request,
    path: str = Form(...),
    content: str = Form(...),
) -> dict[str, Any]:
    editor: FileEditor = request.app.state.file_editor
    try:
        editor.write_file(
            path,
            content,
            kind="prompt",
            commit_message=f"chore(ui): edit prompt {Path(path).name}",
        )
    except EditError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"saved": True, "path": path, **_summarizer_rollback_notice(path)}


# ---------- /config ----------


def _list_yaml_files(editor: FileEditor) -> list[str]:
    base = editor.env_path().parent / "config"
    if not base.exists():
        return []
    return sorted(str(p.relative_to(base.parent)) for p in base.rglob("*.yaml"))


def _yaml_groups(editor: FileEditor) -> list[dict[str, Any]]:
    from src.ui.services.file_catalog import (
        CONFIG_CATALOG,
        CONFIG_CATEGORY_ORDER,
        annotate_files,
    )

    return annotate_files(_list_yaml_files(editor), CONFIG_CATALOG, CONFIG_CATEGORY_ORDER)


# secret 名パターン: env_editor の secret 集合に未登録でも、名前が secret 風なら
# レスポンスでマスクする二重防御 (config_get で使用)。
_SECRET_NAME_RE = re.compile(
    r"(TOKEN|KEY|SECRET|PASSWORD|PASS|WEBHOOK|AUTH|CREDENTIAL)", re.IGNORECASE
)


def _is_secret_env_key(key: str, secret_keys: Collection[str]) -> bool:
    """env キーが secret 扱いか (登録済み secret 集合 or 名前が secret 風)。"""
    return key in secret_keys or bool(_SECRET_NAME_RE.search(key))


@pages_api.get("/config")
async def config_get(request: Request) -> dict[str, Any]:
    editor: FileEditor = request.app.state.file_editor
    env_text = ""
    if editor.env_path().exists():
        env_text = editor.env_path().read_text(encoding="utf-8")
    parsed = parse_env(env_text)
    # C2: allowlist / secret 集合はチャンネルレジストリ駆動 (custom channel の
    # webhook キーも自動で編集欄に出る)。
    secret_keys = secret_env_keys()
    displayed = list(displayed_env_keys(parsed))
    # CLAUDE.md §12/§4: secret は API レスポンスに平文で含めない。
    # 【重要 (2026-07-30 セキュリティ修正)】env_values は **displayed キーのみ** を返す
    # (allowlist 駆動)。旧実装は parsed 全キーを走査し secret 集合外を平文で返していたため、
    # env_editor に未登録の orphan キー (廃止チャンネルの webhook 等) が公開 readonly
    # instance の GET /config から平文漏洩していた ([[config_endpoint_secret_leak]] の再発)。
    # displayed キー内でも secret 集合 or 名前が secret 風のものはマスクする (二重防御)。
    safe_values = {
        k: (mask_value(parsed[k]) if _is_secret_env_key(k, secret_keys) else parsed[k])
        for k in displayed
        if k in parsed
    }
    return {
        # 表示は displayed (未登録の接続先系キーの枠を出さない)。書込許可は allowed のまま
        "env_keys": displayed,
        "env_descriptions": env_key_descriptions(),
        "secret_keys": list(secret_keys),
        "env_values": safe_values,
        "env_masked": {k: mask_value(parsed.get(k, "")) for k in secret_keys},
        "yaml_files": _list_yaml_files(editor),
        "yaml_groups": _yaml_groups(editor),
    }


@pages_api.get("/config/yaml")
async def config_yaml_get(request: Request, path: str) -> dict[str, Any]:
    editor: FileEditor = request.app.state.file_editor
    try:
        return {"path": path, "content": editor.read_file(path, kind="yaml")}
    except EditError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@pages_api.post("/config/env")
async def config_env_save(request: Request) -> dict[str, Any]:
    editor: FileEditor = request.app.state.file_editor
    form = await request.form()
    secret_keys = secret_env_keys()
    updates: dict[str, str] = {}
    for key in allowed_env_keys():
        raw = form.get(key)
        if raw is None or not isinstance(raw, str):
            continue
        if key in secret_keys and not raw:
            continue
        if not raw and key not in secret_keys:
            updates[key] = ""
        else:
            updates[key] = raw
    try:
        update_env(editor.env_path(), updates)
    except EnvEditError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"saved": True, "count": len(updates)}


_LOG_LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR")


class SaveSystemConfigRequest(BaseModel):
    log_level: str
    timezone: str


@pages_api.post("/config/system")
async def config_system_save(request: Request, req: SaveSystemConfigRequest) -> dict[str, Any]:
    """システム設定 (LOG_LEVEL / TIMEZONE) を .env に保存する (設定・死活の画面統合 P3)。

    - LOG_LEVEL: logging_config は os.environ を直読みするため、稼働中プロセスの
      ログ出力は再起動まで変わらない。os.environ にも同期するので、保存後に起動する
      パイプライン subprocess には反映される。
    - TIMEZONE: 表示系 (to_local) は即時反映。スケジューラの cron 解釈は
      Asia/Tokyo 固定 (scheduler.DEFAULT_TIMEZONE) で本設定の影響を受けない。
    """
    import os
    from zoneinfo import ZoneInfo

    log_level = req.log_level.strip().upper()
    if log_level not in _LOG_LEVELS:
        raise HTTPException(
            status_code=400,
            detail=f"ログレベルは {' / '.join(_LOG_LEVELS)} のいずれかを指定してください",
        )
    timezone = req.timezone.strip()
    try:
        ZoneInfo(timezone)
    except Exception as e:  # noqa: BLE001 — 未知 TZ 名 / 空文字はここで弾く
        raise HTTPException(
            status_code=400,
            detail=f"タイムゾーン名が不正です (例: Asia/Tokyo): {timezone or '(空)'}",
        ) from e

    editor: FileEditor = request.app.state.file_editor
    try:
        update_env(editor.env_path(), {"LOG_LEVEL": log_level, "TIMEZONE": timezone})
    except EnvEditError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    # 以後に起動する subprocess へ即時反映するため process env にも同期する
    # (pydantic-settings は os.environ 優先のため、書込元をこの API に一本化して整合を保つ)。
    os.environ["LOG_LEVEL"] = log_level
    os.environ["TIMEZONE"] = timezone
    _log.info("system_config_saved", log_level=log_level, timezone=timezone)
    return {"saved": True, "log_level": log_level, "timezone": timezone}


def _validate_yaml_schema(path: str, content: str) -> str | None:
    """YAML を pydantic スキーマで検証。エラー時はメッセージ、OK は None。"""
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as e:
        return f"YAML 構文エラー: {e}"
    name = Path(path).name
    try:
        if name == "pipelines.yaml":
            if not isinstance(data, dict) or "pipelines" not in data:
                return "pipelines.yaml は { pipelines: [...] } の形式である必要があります"
            for item in data["pipelines"]:
                PipelineConfig(**item)
    except ValidationError as e:
        return f"スキーマ違反: {e}"
    return None


@pages_api.post("/config/yaml")
async def config_yaml_save(
    request: Request,
    path: str = Form(...),
    content: str = Form(...),
) -> dict[str, Any]:
    editor: FileEditor = request.app.state.file_editor
    schema_error = _validate_yaml_schema(path, content)
    if schema_error:
        raise HTTPException(status_code=400, detail=schema_error)
    try:
        editor.write_file(
            path,
            content,
            kind="yaml",
            commit_message=f"chore(ui): edit yaml {Path(path).name}",
        )
    except EditError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"saved": True, "path": path}


# ---------- /config/source-quality (ブリーフ設定: 構造化フォーム) ----------


@pages_api.get("/config/source-quality")
async def source_quality_get(request: Request) -> dict[str, Any]:
    """brief routing 設定を構造化して返す (raw YAML でなくフォーム編集用)。"""
    from src.config_loader import KNOWN_ARTICLE_CATEGORIES
    from src.cti.router import get_source_quality

    sq = get_source_quality()
    return {
        "brief_cap_24h": sq.brief_cap_24h,
        "high_threat_brief_categories": list(sq.high_threat_brief_categories),
        "available_categories": list(KNOWN_ARTICLE_CATEGORIES),
    }


@pages_api.post("/config/source-quality")
async def source_quality_save(
    request: Request,
    brief_cap_24h: int = Form(...),
    categories: str = Form(default=""),  # カンマ区切りの category 群
) -> dict[str, Any]:
    """brief routing 設定を保存 (既知カテゴリのみ許可 → typo 不可)。DB config_store に版保存。"""
    from src.config_loader import KNOWN_ARTICLE_CATEGORIES, SourceQualityConfig
    from src.cti.router import SOURCE_QUALITY_CONFIG_KEY, get_source_quality
    from src.storage.config_store import save_config

    if brief_cap_24h < 0:
        raise HTTPException(status_code=400, detail="brief_cap_24h は 0 以上で指定してください")
    cats = tuple(dict.fromkeys(c.strip() for c in categories.split(",") if c.strip()))
    unknown = [c for c in cats if c not in KNOWN_ARTICLE_CATEGORIES]
    if unknown:
        raise HTTPException(status_code=400, detail=f"未知のカテゴリ: {', '.join(unknown)}")

    config = SourceQualityConfig(brief_cap_24h=brief_cap_24h, high_threat_brief_categories=cats)
    save_config(SOURCE_QUALITY_CONFIG_KEY, config.model_dump(mode="json"), note="UI 編集")
    get_source_quality(force_reload=True)  # routing キャッシュを即更新
    return {
        "saved": True,
        "brief_cap_24h": brief_cap_24h,
        "high_threat_brief_categories": list(cats),
    }


# ---------- /actors (Actor 辞書: 構造化編集) ----------


class ActorEditRequest(BaseModel):
    """actor の編集可能 field (id は path で指定、不変)。"""

    canonical: str
    aliases: list[str] = []
    mitre_group: str | None = None
    nation: str | None = None
    sponsor: str | None = None
    family: str | None = None
    # Actors Stage 5: 実体種別 (group/organization/contractor) + 親機関 actor id
    kind: str | None = None
    sponsor_org: str | None = None
    description: str | None = None
    # F6 (2026-07-26): 照合ゲートを編集可能化 (origin は死にフィールドのため撤去)
    ambiguous: bool = False
    context_cues: list[str] = []
    # reference 用詳細 (Stage 1)
    summary: str | None = None
    motivation: str | None = None
    first_seen: str | None = None
    target_sectors: list[str] = []
    target_regions: list[str] = []
    associated_malware: list[str] = []
    notable_campaigns: list[str] = []
    references: list[str] = []


@pages_api.get("/actors")
async def actors_list(request: Request) -> dict[str, Any]:
    """Actor 辞書の一覧 (raw YAML でなく構造化表示・編集用)。"""
    from src.cti.actor_editor import list_actors, list_families

    return {"actors": list_actors(), "families": list_families()}


@pages_api.post("/actors/{actor_id}")
async def actors_update(request: Request, actor_id: str, body: ActorEditRequest) -> dict[str, Any]:
    """既存 actor を更新。alias 重複 (誤帰属) は 400 で拒否。"""
    from src.cti.actor_editor import (
        apply_actor_edit,
        load_actors_raw,
        render_actors_yaml,
        validate_actor_edit,
    )

    data = load_actors_raw()
    err = validate_actor_edit(
        data, actor_id, body.canonical, body.aliases, ambiguous=body.ambiguous
    )
    if err:
        raise HTTPException(status_code=400, detail=err)
    # 編集前の名称集合 (canonical + aliases、小文字) — 新規追加名の検出用
    _old = next(
        (a for a in data["actors"] if isinstance(a, dict) and str(a.get("id")) == actor_id),
        None,
    )
    old_names = {
        str(n).strip().lower()
        for n in ((_old or {}).get("aliases") or []) + [(_old or {}).get("canonical") or ""]
        if str(n).strip()
    }
    try:
        new_data = apply_actor_edit(data, actor_id, body.model_dump())
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"アクターが見つかりません: {actor_id}") from e

    content = render_actors_yaml(new_data)
    editor: FileEditor = request.app.state.file_editor
    try:
        editor.write_file(
            "config/cti/actor_aliases.yaml",
            content,
            kind="yaml",
            commit_message=f"chore(ui): edit actor {actor_id}",
        )
    except EditError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    _invalidate_actor_caches()
    # 承認時有界再帰属: 名称 (canonical/alias) が増えた場合のみ ≤90d 記事を再判定
    new_names = {s.strip().lower() for s in [body.canonical, *body.aliases] if s.strip()}
    reattributed: dict[str, int] = {}
    if new_names - old_names:
        try:
            from src.ui.services.actor_reattribution import reattribute_actor

            reattributed = reattribute_actor(RunHistoryRepository(), actor_id)
        except Exception as e:  # noqa: BLE001 — 再帰属失敗で保存を巻き戻さない
            _log.warning("actor_reattribution_failed", actor_id=actor_id, error=str(e))
    # retro-cleanup (再帰属の対称形、2026-08-06): 名称が **減った** 場合は、現行辞書で
    # 照合できなくなった既存 entity 行を掃除する。これが無いと一般語 alias の除去後も
    # 「言及された組織・関係者」に幽霊アクターが残り続ける (hunters/CHROMIUM の実害)。
    retro_cleaned = 0
    if old_names - new_names:
        try:
            from src.ui.services.entity_retro_cleanup import cleanup_stale_actor_entities

            retro_cleaned = cleanup_stale_actor_entities(
                RunHistoryRepository(), actor_ids=[actor_id]
            ).deleted
        except Exception as e:  # noqa: BLE001 — 掃除失敗で保存を巻き戻さない
            _log.warning("actor_retro_cleanup_failed", actor_id=actor_id, error=str(e))
    return {
        "saved": True,
        "actor_id": actor_id,
        "reattributed": reattributed,
        "retro_cleaned": retro_cleaned,
    }


def _invalidate_actor_caches() -> None:
    """辞書編集の反映を TTL (10 分) 待ちにしない — snapshot / 脅威評価 cache を即破棄。"""
    from src.ui.services.actor_threat import invalidate_assessment_cache
    from src.ui.services.threat_operations import invalidate_threat_snapshot_cache

    invalidate_threat_snapshot_cache()
    invalidate_assessment_cache()


# ---------- /actors/sync (Actors Stage 4: MITRE 同期レビュー) ----------


def _proposal_to_dict(p: Any) -> dict[str, Any]:
    """ActorUpdateProposalRecord → UI 用 dict (payload は JSON parse 済で返す)。"""
    import json

    try:
        payload = json.loads(p.payload)
    except (TypeError, ValueError):
        payload = {}
    return {
        "id": p.id,
        "proposal_type": p.proposal_type,
        "mitre_group": p.mitre_group,
        "actor_id": p.actor_id,
        "payload": payload,
        "rationale": p.rationale,
        "status": p.status,
        "created_at": p.created_at.isoformat(),
    }


@pages_api.get("/actors/sync")
async def actors_sync_status(request: Request) -> dict[str, Any]:
    """MITRE 同期のレビュー待ち提案一覧 + 同期カバレッジ。"""
    from src.cti.actor_editor import load_actors_raw

    repo = RunHistoryRepository()
    proposals = repo.list_actor_update_proposals(status="pending")
    actors = [a for a in load_actors_raw()["actors"] if isinstance(a, dict)]
    synced = sum(1 for a in actors if a.get("mitre_summary_sha1"))
    return {
        "proposals": [_proposal_to_dict(p) for p in proposals],
        "pending_count": len(proposals),
        "synced_actors": synced,
        "total_actors": len(actors),
    }


@pages_api.post("/actors/sync/proposals/{proposal_id}/approve")
async def actors_sync_approve(request: Request, proposal_id: int) -> dict[str, Any]:
    """MITRE 同期提案を承認して actor_aliases.yaml に適用する。

    - mitre_new_actor: alias 衝突を再検証してから新規 actor を追加
    - mitre_alias_conflict: alias を MITRE 側の actor に付け替え
    """
    import hashlib
    import json

    from src.cti.actor_editor import (
        append_new_actor,
        load_actors_raw,
        move_alias,
        render_actors_yaml,
        validate_actor_edit,
    )

    repo = RunHistoryRepository()
    proposal = repo.get_actor_update_proposal(proposal_id)
    if proposal is None or proposal.status != "pending":
        raise HTTPException(status_code=404, detail="承認待ちの提案が見つかりません")
    try:
        payload = json.loads(proposal.payload)
    except (TypeError, ValueError) as e:
        _log.warning("actor_sync_proposal_payload_broken", proposal_id=proposal_id, error=str(e))
        raise HTTPException(status_code=400, detail="提案データが壊れています") from e

    data = load_actors_raw()
    if proposal.proposal_type == "mitre_new_actor":
        actor = {k: v for k, v in payload.items() if k != "summary_en"}
        # 提案生成後に辞書側が変わっている可能性があるため承認時にも衝突再検証
        err = validate_actor_edit(
            data,
            str(actor.get("id", "")),
            str(actor.get("canonical", "")),
            [str(x) for x in actor.get("aliases") or []],
        )
        if err:
            raise HTTPException(status_code=409, detail=f"衝突のため適用できません: {err}")
        summary_en = str(payload.get("summary_en") or "")
        if summary_en:
            actor["mitre_summary_sha1"] = hashlib.sha1(summary_en.encode("utf-8")).hexdigest()
        try:
            new_data = append_new_actor(data, actor)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        commit_msg = f"chore(sync): MITRE 提案承認 — 新規 actor {actor.get('id')}"
    elif proposal.proposal_type == "mitre_alias_conflict":
        # 陳腐化ガード (2026-07-16 実害の再発防止): 提案の前提「この mitre_group は
        # to_id の actor」が **現在の辞書** と食い違うなら適用しない。修正前の同期バグ
        # (cd28c93a 以前の別グループ誤マッチ) が生成した stale 提案が後日承認され、
        # APT35 の全別名が The Lamberts へ移動する辞書汚染が実際に起きた。
        to_id = str(payload.get("mitre_actor_id", ""))
        mitre_group = str(proposal.mitre_group or "")
        if mitre_group:
            owner = next(
                (
                    str(a.get("id", ""))
                    for a in data.get("actors", [])
                    if isinstance(a, dict) and str(a.get("mitre_group", "")) == mitre_group
                ),
                "",
            )
            if owner and owner != to_id:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"提案が陳腐化しています: 辞書では {mitre_group} は"
                        f" '{owner}' に帰属しており、提案の '{to_id}' と矛盾します。"
                        "この提案は却下してください (同期バグ期の生成物の可能性)。"
                    ),
                )
        try:
            new_data = move_alias(
                data,
                str(payload.get("alias", "")),
                from_id=str(payload.get("current_owner_id", "")),
                to_id=to_id,
            )
        except (KeyError, ValueError) as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        commit_msg = (
            f"chore(sync): MITRE 提案承認 — alias『{payload.get('alias')}』を"
            f" {payload.get('mitre_actor_id')} に付け替え"
        )
    elif proposal.proposal_type == "corpus_emerging_actor":
        # Actor Recall Layer: コーパス由来の新興アクター候補を辞書化 (_evidence は除外)。
        actor = {k: v for k, v in payload.items() if not k.startswith("_")}
        err = validate_actor_edit(
            data,
            str(actor.get("id", "")),
            str(actor.get("canonical", "")),
            [str(x) for x in actor.get("aliases") or []],
        )
        if err:
            raise HTTPException(status_code=409, detail=f"衝突のため適用できません: {err}")
        try:
            new_data = append_new_actor(data, actor)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        commit_msg = f"chore(actor): 新興アクター提案承認 — {actor.get('id')}"
    elif proposal.proposal_type == "news_alias":
        # F3 (アクター辞書 Phase2): 報道 aka 併記由来の別名を既存アクターに追加。
        from src.cti.actor_editor import apply_actor_edit

        target_id = str(payload.get("actor_id", "")).strip()
        alias = str(payload.get("alias", "")).strip()
        cur = next(
            (a for a in data["actors"] if isinstance(a, dict) and str(a.get("id")) == target_id),
            None,
        )
        if cur is None or not alias:
            raise HTTPException(
                status_code=409, detail=f"対象アクターが見つかりません: {target_id}"
            )
        new_aliases = [str(x) for x in (cur.get("aliases") or [])]
        if alias.lower() not in {x.lower() for x in new_aliases}:
            new_aliases.append(alias)
        err = validate_actor_edit(data, target_id, str(cur.get("canonical", "")), new_aliases)
        if err:
            raise HTTPException(status_code=409, detail=f"衝突のため適用できません: {err}")
        new_data = apply_actor_edit(data, target_id, {"aliases": new_aliases})
        commit_msg = f"chore(actor): 報道由来 alias 承認 — {target_id} +『{alias}』"
    else:
        raise HTTPException(status_code=400, detail=f"未知の提案種別: {proposal.proposal_type}")

    editor: FileEditor = request.app.state.file_editor
    try:
        editor.write_file(
            "config/cti/actor_aliases.yaml",
            render_actors_yaml(new_data),
            kind="yaml",
            commit_message=commit_msg,
        )
    except EditError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    _invalidate_actor_caches()
    repo.decide_actor_update_proposal(proposal_id, status="accepted")
    # 新興アクター承認時は暗定 entity を確定 actor に backfill (歴史記事の帰属も昇格)。
    backfilled = 0
    if proposal.proposal_type == "corpus_emerging_actor":
        key = str(payload.get("_evidence", {}).get("key", "")).strip()
        actor_id = str(payload.get("id", "")).strip()
        if key and actor_id:
            backfilled = repo.promote_provisional_actor(key, actor_id)
    # 承認時有界再帰属 (アクター辞書 Phase1): 新名称で body 現存 (≤90d) 記事を再判定し
    # 影響月の行動史を再蒸留する。失敗しても承認自体は成立させる (warning のみ)。
    if proposal.proposal_type in ("mitre_new_actor", "corpus_emerging_actor"):
        _reattr_target = str(payload.get("id", "")).strip()
    elif proposal.proposal_type == "mitre_alias_conflict":
        _reattr_target = str(payload.get("mitre_actor_id", "")).strip()
    elif proposal.proposal_type == "news_alias":
        _reattr_target = str(payload.get("actor_id", "")).strip()
    else:  # pragma: no cover — 未知種別は上で 400 済み
        _reattr_target = ""
    reattributed: dict[str, int] = {}
    if _reattr_target:
        try:
            from src.ui.services.actor_reattribution import reattribute_actor

            reattributed = reattribute_actor(repo, _reattr_target)
        except Exception as e:  # noqa: BLE001 — 再帰属失敗で承認を巻き戻さない
            _log.warning("actor_reattribution_failed", actor_id=_reattr_target, error=str(e))
    # retro-cleanup (2026-08-06): alias 付け替えで名前を**失った**側の actor は、
    # 現行辞書で照合できなくなった既存 entity 行を掃除する (幽霊アクター防止)。
    retro_cleaned = 0
    if proposal.proposal_type == "mitre_alias_conflict":
        _lost_id = str(payload.get("current_owner_id", "")).strip()
        if _lost_id:
            try:
                from src.ui.services.entity_retro_cleanup import (
                    cleanup_stale_actor_entities,
                )

                retro_cleaned = cleanup_stale_actor_entities(repo, actor_ids=[_lost_id]).deleted
            except Exception as e:  # noqa: BLE001 — 掃除失敗で承認を巻き戻さない
                _log.warning("actor_retro_cleanup_failed", actor_id=_lost_id, error=str(e))
    return {
        "approved": True,
        "proposal_id": proposal_id,
        "backfilled": backfilled,
        "reattributed": reattributed,
        "retro_cleaned": retro_cleaned,
    }


@pages_api.post("/actors/sync/proposals/{proposal_id}/reject")
async def actors_sync_reject(request: Request, proposal_id: int) -> dict[str, Any]:
    """MITRE 同期提案を却下する (dedup_key により同一提案は再生成されない)。"""
    repo = RunHistoryRepository()
    if not repo.decide_actor_update_proposal(proposal_id, status="rejected"):
        raise HTTPException(status_code=404, detail="承認待ちの提案が見つかりません")
    return {"rejected": True, "proposal_id": proposal_id}


# ---------- /schedule ----------


# Phase 5T-D: watcher name → module path (旧 schedule router の長い elif chain を dict 化)。
# 新しい watcher を追加した際はここに 1 行追加するだけで cluster member 表示に出る。
_WATCHER_MODULE_MAP: dict[str, str] = {
    "ccdcoe": "src.watchers.ccdcoe",
    "wilson-center": "src.watchers.wilson_center",
    "acled": "src.watchers.acled",
    "ifri": "src.watchers.ifri",
    "ncsc-nz": "src.watchers.ncsc_nz",
    "orf-india": "src.watchers.orf_india",
    "bsi-germany": "src.watchers.bsi_germany",
    "csa-singapore": "src.watchers.csa_singapore",
    "isw": "src.watchers.isw",
    "38north": "src.watchers.north_38",
    "hudson": "src.watchers.hudson",
    "nozomi": "src.watchers.nozomi",
    "enisa": "src.watchers.enisa",
    "ipa": "src.watchers.ipa",
    "lookout": "src.watchers.lookout",
    "nicter": "src.watchers.nicter",
    "nicter-blog": "src.watchers.nicter_blog",
}


def _read_watcher_state(name: str) -> Any:
    """name → watcher の ``read_state()`` を呼ぶ (失敗時 None)。"""
    mod_path = _WATCHER_MODULE_MAP.get(name)
    if mod_path is None:
        return None
    try:
        mod = importlib.import_module(mod_path)
        return mod.read_state()
    except Exception:  # noqa: BLE001
        return None


def _collect_cluster_members(scrapers: list[ScraperEntry]) -> list[dict[str, Any]]:
    """Phase 5T-D: cluster 内 watcher の seen 状態を集約。"""
    out: list[dict[str, Any]] = []
    for entry in scrapers:
        seen_count: int | None = None
        last_modified: datetime | None = None
        state_exists = False
        state = _read_watcher_state(entry.name)
        if state is not None:
            seen_count = getattr(state, "seen_count", None)
            last_modified = getattr(state, "last_modified", None)
            state_exists = bool(getattr(state, "state_file_exists", False))
        out.append(
            {
                "name": entry.name,
                "enabled": entry.enabled,
                "seen_count": seen_count,
                "last_modified": last_modified.isoformat() if last_modified else None,
                "state_exists": state_exists,
            }
        )
    return out


@pages_api.get("/schedule")
async def schedule_get(request: Request) -> dict[str, Any]:
    # readonly instance では scheduler が None → 空 dict で fallback。
    # scheduler_available=False の時は next_run/is_paused は算出不能 (main instance で稼働中)
    # なので frontend は設定上のスケジュール (cron) を代替表示する。
    scheduler = request.app.state.scheduler
    scheduler_available = scheduler is not None
    job_runs = dict(scheduler.list_jobs()) if scheduler is not None else {}
    try:
        pipelines = load_pipelines()
    except Exception as e:  # noqa: BLE001
        return {"pipelines": [], "load_error": str(e)}
    result: list[dict[str, Any]] = []
    for p in pipelines:
        sched = p.schedule
        proc = p.processor
        next_run = job_runs.get(p.name)
        cluster_members: list[dict[str, Any]] = []
        if p.source.type == "web_scraper_cluster":
            cluster_members = _collect_cluster_members(p.source.scrapers)
        result.append(
            {
                "name": p.name,
                "source_type": p.source.type,
                "max_articles": p.source.max_articles,
                "schedule_enabled": bool(sched and sched.enabled),
                "hour": sched.hour if sched else None,
                "minute": sched.minute if sched else None,
                "interval_minutes": sched.interval_minutes if sched else None,
                "day_of_week": sched.day_of_week if sched else None,
                "triage_enabled": proc.triage_enabled,
                "triage_keep_importance": list(proc.triage_keep_importance),
                "triage_max_keep": proc.triage_max_keep,
                "think_enabled": proc.think_enabled,
                "similarity_threshold_hard": proc.similarity_threshold_hard,
                "similarity_threshold_cluster": proc.similarity_threshold_cluster,
                "dedup_window_hours_hard": proc.dedup_window_hours_hard,
                "dedup_window_hours_cluster": proc.dedup_window_hours_cluster,
                "cluster_members": cluster_members,
                "next_run_at": next_run.isoformat() if next_run else None,
                # is_paused は scheduler が動いている instance でのみ意味を持つ。readonly
                # (scheduler 無し) では「全件 paused」誤判定を避けるため常に False。
                "is_paused": scheduler_available
                and next_run is None
                and bool(sched and sched.enabled),
            }
        )
    return {"pipelines": result, "scheduler_available": scheduler_available}


@pages_api.post("/schedule/{job_id}/pause")
async def schedule_pause(request: Request, job_id: str) -> dict[str, Any]:
    scheduler = request.app.state.scheduler
    try:
        scheduler.pause(job_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"paused": True, "job_id": job_id}


@pages_api.post("/schedule/{job_id}/resume")
async def schedule_resume(request: Request, job_id: str) -> dict[str, Any]:
    scheduler = request.app.state.scheduler
    try:
        scheduler.resume(job_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"resumed": True, "job_id": job_id}


@pages_api.post("/schedule/{job_id}/trigger")
async def schedule_trigger(request: Request, job_id: str) -> dict[str, Any]:
    scheduler = request.app.state.scheduler
    try:
        scheduler.trigger_now(job_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"triggered": True, "job_id": job_id}


@pages_api.post("/schedule/{job_id}/cron")
async def schedule_update_cron(
    request: Request,
    job_id: str,
    hour: int = Form(...),
    minute: int = Form(...),
) -> dict[str, Any]:
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise HTTPException(status_code=400, detail="不正な時刻")
    scheduler = request.app.state.scheduler
    try:
        scheduler.update_cron(hour=hour, minute=minute, job_id=job_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"updated": True, "hour": hour, "minute": minute}


def _reload_scheduler_for_pipeline(request: Request, job_id: str) -> None:
    """yaml の最新値を読み込んで job を reschedule (旧 schedule router から移植)。"""
    scheduler = request.app.state.scheduler
    pipelines = load_pipelines()
    target = next((p for p in pipelines if p.name == job_id), None)
    if target is None or target.schedule is None:
        return
    sched = target.schedule
    if not sched.enabled:
        try:
            scheduler.pause(job_id)
        except Exception as e:  # noqa: BLE001
            _log.warning("scheduler_reload_failed", job_id=job_id, error=str(e))
            raise HTTPException(
                status_code=500,
                detail="スケジュールの再読み込みに失敗しました (コンテナの再起動が必要です)",
            ) from e
        return
    if sched.is_interval:
        assert sched.interval_minutes is not None
        scheduler.update_interval(
            interval_minutes=sched.interval_minutes,
            job_id=job_id,
            offset_minutes=sched.interval_offset_minutes,
        )
    else:
        scheduler.update_cron(
            hour=sched.hour,
            minute=sched.minute,
            job_id=job_id,
            day_of_week=sched.day_of_week,
        )
    scheduler.resume(job_id)


@pages_api.post("/schedule/{job_id}/update_schedule")
async def schedule_update_full(
    request: Request,
    job_id: str,
    mode: str = Form(...),
    enabled: str | None = Form(default=None),
    hour: int | None = Form(default=None),
    minute: int | None = Form(default=None),
    interval_minutes: int | None = Form(default=None),
    day_of_week: str | None = Form(default=None),
) -> dict[str, Any]:
    """schedule (cron / interval / enabled / day_of_week) を yaml 永続化 + reload。"""
    editor = get_pipelines_editor(request.app.state)
    try:
        editor.update_schedule(
            job_id,
            enabled=(enabled == "1"),
            mode=mode,
            hour=hour,
            minute=minute,
            interval_minutes=interval_minutes,
            day_of_week=day_of_week or None,
        )
    except PipelinesEditError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    _reload_scheduler_for_pipeline(request, job_id)
    return {"updated": True, "job_id": job_id}


@pages_api.post("/schedule/{job_id}/update_source")
async def schedule_update_source(
    request: Request,
    job_id: str,
    max_articles: int = Form(...),
) -> dict[str, Any]:
    """source.max_articles を yaml 永続化 (scheduler 影響なし)。"""
    editor = get_pipelines_editor(request.app.state)
    try:
        editor.update_max_articles(job_id, max_articles)
    except PipelinesEditError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"updated": True, "max_articles": max_articles}


@pages_api.post("/schedule/{job_id}/update_dedup")
async def schedule_update_dedup(
    request: Request,
    job_id: str,
    similarity_threshold_hard: float = Form(...),
    similarity_threshold_cluster: float = Form(...),
    dedup_window_hours_hard: int = Form(...),
    dedup_window_hours_cluster: int = Form(...),
) -> dict[str, Any]:
    editor = get_pipelines_editor(request.app.state)
    try:
        editor.update_dedup_thresholds(
            job_id,
            threshold_hard=similarity_threshold_hard,
            threshold_cluster=similarity_threshold_cluster,
            window_hours_hard=dedup_window_hours_hard,
            window_hours_cluster=dedup_window_hours_cluster,
        )
    except PipelinesEditError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"updated": True}


@pages_api.post("/schedule/{job_id}/update_think")
async def schedule_update_think(
    request: Request,
    job_id: str,
    think_enabled: str | None = Form(default=None),
) -> dict[str, Any]:
    editor = get_pipelines_editor(request.app.state)
    try:
        editor.update_think(job_id, think_enabled=(think_enabled == "1"))
    except PipelinesEditError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"updated": True, "think_enabled": think_enabled == "1"}


@pages_api.post("/schedule/{job_id}/update_triage")
async def schedule_update_triage(
    request: Request,
    job_id: str,
    max_keep: int = Form(...),
) -> dict[str, Any]:
    form = await request.form()
    enabled = str(form.get("enabled") or "") == "1"
    raw_keep = form.getlist("keep_importance")
    keep_importance = [str(v) for v in raw_keep if isinstance(v, str)]
    editor = get_pipelines_editor(request.app.state)
    try:
        editor.update_triage(
            job_id,
            enabled=enabled,
            keep_importance=keep_importance,
            max_keep=max_keep,
        )
    except PipelinesEditError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"updated": True}


@pages_api.post("/schedule/{job_id}/scrapers/{scraper_name}/toggle")
async def schedule_toggle_scraper(
    request: Request,
    job_id: str,
    scraper_name: str,
    enabled: str = Form(...),
) -> dict[str, Any]:
    """Phase 5T-E: cluster pipeline 内の個別 watcher を on/off。"""
    new_enabled = enabled == "1"
    editor = get_pipelines_editor(request.app.state)
    try:
        editor.set_cluster_scraper_enabled(job_id, scraper_name, enabled=new_enabled)
    except PipelinesEditError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"updated": True, "enabled": new_enabled}


# ---------- /intel-graph/taxonomy-review ----------


@pages_api.get("/taxonomy-review")
async def taxonomy_review(request: Request) -> dict[str, Any]:
    from src.storage.run_history import RunHistoryRepository

    repo = RunHistoryRepository()
    pending = repo.list_taxonomy_proposals(status="pending", limit=200)
    recent = (
        repo.list_taxonomy_proposals(status="accepted", limit=20)
        + repo.list_taxonomy_proposals(status="rejected", limit=20)
        + repo.list_taxonomy_proposals(status="deferred", limit=20)
    )
    recent.sort(key=lambda p: p.reviewed_at or p.created_at, reverse=True)

    def _evidence_ids(raw: str | None) -> list[str]:
        # 証拠記事 id (保存済みだが従来 UI 未表示だった — 2026-08-22 から表示する)。
        # JSON array 以外の破損値は空扱い (証拠が出ないだけで提案表示は壊さない)。
        import json

        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return []
        if not isinstance(parsed, list):
            return []
        return [str(v) for v in parsed if v][:5]

    def _to(p: object) -> dict[str, Any]:
        from src.storage.run_history import TaxonomyProposalRecord

        if not isinstance(p, TaxonomyProposalRecord):
            return {}
        return {
            "id": p.id,
            "proposal_type": p.proposal_type,
            "tier": p.tier,
            "target_yaml": p.target_yaml,
            "target_canonical": p.target_canonical or "",
            "proposed_change": p.proposed_change,
            "rationale": p.rationale,
            "confidence": p.confidence,
            "evidence_count": p.evidence_count,
            "evidence_ids": _evidence_ids(p.evidence_ids),
            "status": p.status,
            # Phase Diamond fix: ISO 完全形 (tz info 込み) で返却、frontend formatJst で JST 表示
            "reviewed_at": p.reviewed_at.isoformat() if p.reviewed_at else "",
        }

    return {
        "tier_1": [_to(p) for p in pending if p.tier == "tier_1_auto"],
        "tier_2": [_to(p) for p in pending if p.tier == "tier_2_review"],
        "tier_3": [_to(p) for p in pending if p.tier == "tier_3_strategic"],
        "recent_reviewed": [_to(p) for p in recent[:30]],
    }


@pages_api.post("/taxonomy-review/{proposal_id}/{action}")
async def taxonomy_action(
    request: Request,
    proposal_id: int,
    action: str,
) -> dict[str, Any]:
    """提案の決定。accept は対象 yaml へ**適用してから** status を変える (fail-closed)。

    2026-07-31 運用レビュー完全調査の本質修正: 従来の accept は status 変更のみで
    適用処理が無く、「採用」した提案が辞書に入らないまま残っていた (UI は「ワン
    クリック承認」と適用を約束)。actor 辞書化は actors/sync 承認と同じ終端処理
    (cache 破棄 + 暫定 entity 昇格 + 有界再帰属) まで行う。
    """
    import json

    if action not in ("accept", "reject", "defer"):
        raise HTTPException(status_code=400, detail="不正な操作です")
    repo = RunHistoryRepository()
    status_map = {"accept": "accepted", "reject": "rejected", "defer": "deferred"}
    applied: dict[str, Any] = {}
    backfilled = 0
    reattributed: dict[str, int] = {}
    if action == "accept":
        from pathlib import Path

        from src.cti.actor_editor import load_actors_raw
        from src.taxonomy.apply import (
            SECTORS_YAML_RELPATH,
            TaxonomyApplyError,
            apply_taxonomy_proposal,
        )

        proposal = repo.get_taxonomy_proposal(proposal_id)
        if proposal is None or proposal.status != "pending":
            raise HTTPException(status_code=404, detail="承認待ちの提案が見つかりません")
        editor: FileEditor = request.app.state.file_editor

        def _write_yaml(relpath: str, content: str) -> None:
            editor.write_file(
                relpath,
                content,
                kind="yaml",
                commit_message=f"chore(taxonomy): 提案承認 #{proposal_id}",
            )

        try:
            applied = apply_taxonomy_proposal(
                proposal,
                write_yaml=_write_yaml,
                sectors_text_loader=lambda: Path(SECTORS_YAML_RELPATH).read_text(encoding="utf-8"),
                actors_data_loader=load_actors_raw,
            )
        except TaxonomyApplyError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        except EditError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        actor_id = str(applied.get("actor_id") or "")
        if applied.get("applied") == "yaml_updated" and actor_id:
            _invalidate_actor_caches()
            # pattern_5 の canonical と同名の暫定 entity があれば確定 actor に昇格
            try:
                change = json.loads(proposal.proposed_change)
                prov_key = str(change.get("canonical", "")).strip().lower()
            except (TypeError, ValueError):
                prov_key = ""
            if prov_key:
                backfilled = repo.promote_provisional_actor(prov_key, actor_id)
            try:
                from src.ui.services.actor_reattribution import reattribute_actor

                reattributed = reattribute_actor(repo, actor_id)
            except Exception as e:  # noqa: BLE001 — 再帰属失敗で承認を巻き戻さない
                _log.warning("actor_reattribution_failed", actor_id=actor_id, error=str(e))
    ok = repo.update_taxonomy_proposal_status(proposal_id, status=status_map[action])
    return {
        "updated": ok,
        "id": proposal_id,
        "status": status_map[action],
        "applied": applied,
        "backfilled": backfilled,
        "reattributed": reattributed,
    }


# ---------- /tuning-labels (凍結ラベル資産 + goldset 評価履歴) ----------


@pages_api.get("/tuning-labels")
async def tuning_labels_summary() -> dict[str, Any]:
    """遅延正解ラベルと goldset 切替評価の履歴 — 運用タブの件数カード用。

    ラベル台帳は 2026-08-22 の較正格子撤収により **凍結資産** (新規収穫なし)。
    goldset 評価 (evals) のみ weekly-goldset-eval で更新が続く。

    denylist 対象 (運用系 read API は公開面に出さない — read_only_policy)。
    """
    repo = RunHistoryRepository()
    return {
        "summary": repo.summarize_tuning_labels(),
        "recent": repo.list_tuning_labels(limit=20),
        "evals": repo.list_tuning_evals(limit=10),
    }


# ---------- /intel-graph/editorial-quality ----------


_VALID_STANCES = ("factual_report", "analytical", "opinion", "propaganda", "unknown")


@pages_api.get("/editorial-quality")
async def editorial_quality(
    request: Request,
    lookback_days: int = 14,
    stance_filter: str | None = None,
    feed_filter: str | None = None,
) -> dict[str, Any]:
    repo = RunHistoryRepository()
    # M3 窓バグ修正: クロス集計が lookback_days を無視して 30 固定だったため、同一
    # レスポンス内で記事リスト (param 準拠) と集計 (30 日) の窓が矛盾していた。
    crosstab_raw = repo.count_editorial_stance_by_feed(lookback_days=lookback_days)
    feed_stance_map: dict[str, dict[str, int]] = {}
    stance_totals: dict[str, int] = dict.fromkeys(_VALID_STANCES, 0)
    for row in crosstab_raw:
        feed = str(row["feed_title"]) or "(unknown)"
        stance = str(row["stance"])
        n = int(row["n"])  # type: ignore[call-overload]
        feed_stance_map.setdefault(feed, dict.fromkeys(_VALID_STANCES, 0))[stance] = n
        stance_totals[stance] = stance_totals.get(stance, 0) + n
    feed_rows: list[dict[str, Any]] = []
    for feed, counts in feed_stance_map.items():
        total = sum(counts.values())
        feed_rows.append(
            {
                "feed": feed,
                "counts": counts,
                "total": total,
                "propaganda_ratio": (counts.get("propaganda", 0) / total) if total else 0.0,
            }
        )
    feed_rows.sort(
        key=lambda r: (-float(r["propaganda_ratio"]), -int(r["total"])),
    )

    if stance_filter not in _VALID_STANCES:
        stance_filter = None
    articles = repo.list_recent_articles_with_stance(
        stance_filter=stance_filter,
        feed_filter=feed_filter,
        lookback_days=lookback_days,
        limit=120,
    )
    article_view: list[dict[str, Any]] = []
    for art in articles:
        review = repo.get_editorial_stance_review(art.article_id)
        article_view.append(
            {
                "article_id": art.article_id,
                "title": art.title or "",
                "feed_title": art.feed_title or "",
                "importance": art.importance or "",
                "category": art.category or "",
                "posted_channel": art.posted_channel or "",
                "stance": art.editorial_stance or "unknown",
                # Phase Diamond fix: ISO 形式で返却 (UTC tz info 込み)。
                # frontend formatJst で JST 表示。
                "created_at": (
                    art.created_at.isoformat() if hasattr(art.created_at, "isoformat") else ""
                ),
                "url": art.url or "",
                "review": review,
            }
        )
    return {
        "feed_rows": feed_rows,
        "stance_totals": stance_totals,
        "stances": list(_VALID_STANCES),
        "articles": article_view,
        "lookback_days": lookback_days,
        "stance_filter": stance_filter or "",
        "feed_filter": feed_filter or "",
        "total_articles": sum(stance_totals.values()),
    }


@pages_api.post("/editorial-quality/review")
async def editorial_review(request: Request) -> dict[str, Any]:
    form = await request.form()
    article_id = str(form.get("article_id") or "").strip()
    corrected = str(form.get("corrected_stance") or "").strip()
    original = str(form.get("original_stance") or "").strip() or None
    comment = str(form.get("comment") or "").strip()
    if not article_id or corrected not in _VALID_STANCES:
        raise HTTPException(status_code=400, detail="入力が不正です")
    repo = RunHistoryRepository()
    repo.upsert_editorial_stance_review(
        article_id=article_id,
        original_stance=original,
        corrected_stance=corrected,
        comment=comment,
    )
    # 訂正を articles 本体へ還流 (2026-07-31): レビュー表に留めるとクロス集計・
    # フィルタが訂正前の値のままになる write-only 状態だった。
    stance_updated = repo.update_article_editorial_stance(article_id, corrected)
    return {"saved": True, "article_id": article_id, "stance_updated": stance_updated > 0}
