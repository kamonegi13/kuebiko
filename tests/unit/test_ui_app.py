"""FastAPI ルータ群のレンダリングテスト (Phase 1.5)。

ネットワーク/サブプロセスは触らず、HTML レンダリングと基本ルーティングのみ確認する。
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _init_git(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(["git", "-C", str(root), "config", "user.name", "test"], check=True)


def _bootstrap_project(tmp_path: Path) -> Path:
    (tmp_path / "prompts" / "briefing").mkdir(parents=True)
    (tmp_path / "prompts" / "briefing/summarizer.j2").write_text("test prompt", encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "pipelines.yaml").write_text(
        "pipelines:\n  - name: direct-rss-fetch\n"
        "    source:\n      type: rss\n      max_articles: 10\n"
        "    processor: {}\n",
        encoding="utf-8",
    )
    (tmp_path / "data").mkdir()
    (tmp_path / ".env").write_text(
        "DISCORD_WEBHOOK_ALERT=https://discord.com/api/webhooks/1/a\n"
        "DISCORD_WEBHOOK_BRIEF=https://discord.com/api/webhooks/2/b\n"
        "DISCORD_WEBHOOK_WATCH=https://discord.com/api/webhooks/3/w\n"
        "DISCORD_WEBHOOK_OPS=https://discord.com/api/webhooks/4/o\n",
        encoding="utf-8",
    )
    _init_git(tmp_path)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "init"],
        check=True,
    )
    return tmp_path


@pytest.fixture
def client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    project = _bootstrap_project(tmp_path)
    monkeypatch.setenv("CTI_PROJECT_ROOT", str(project))
    monkeypatch.chdir(project)
    # ``.env`` をプロジェクト内のものに切り替え
    monkeypatch.setenv("DISCORD_WEBHOOK_ALERT", "https://discord.com/api/webhooks/1/a")
    monkeypatch.setenv("DISCORD_WEBHOOK_BRIEF", "https://discord.com/api/webhooks/2/b")
    monkeypatch.setenv("DISCORD_WEBHOOK_WATCH", "https://discord.com/api/webhooks/3/w")
    monkeypatch.setenv("DISCORD_WEBHOOK_OPS", "https://discord.com/api/webhooks/4/o")

    # FastAPI を毎回新規にインスタンス化 (lifespan を発火させる)
    from src.ui import app as app_module

    fresh_app = app_module.create_app()
    with TestClient(fresh_app) as c:
        yield c


def test_dashboard_redirects_to_react(client: TestClient) -> None:
    """/ は React SPA (/app/dashboard) に redirect。"""
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/app/dashboard"


def test_history_legacy_redirects_to_react(client: TestClient) -> None:
    """/history は React SPA に redirect。"""
    resp = client.get("/history", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/app/history"


def test_api_v1_history_returns_json(client: TestClient) -> None:
    """新 JSON API は articles / recent_runs / filters を返す。"""
    resp = client.get("/api/v1/history")
    assert resp.status_code == 200
    data = resp.json()
    assert "articles" in data
    assert "recent_runs" in data
    assert "filters" in data
    assert isinstance(data["articles"], list)


def test_runs_redirects_to_react(client: TestClient) -> None:
    """/runs は React SPA (/app/runs) に redirect。"""
    resp = client.get("/runs", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/app/runs"


def test_run_detail_redirects_to_react(client: TestClient) -> None:
    """/runs/{id} は React SPA に redirect。"""
    resp = client.get("/runs/123", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/app/run/123"


def test_api_v1_config_does_not_leak_secrets(client: TestClient) -> None:
    """C-1: /api/v1/config の env_values で secret (webhook 等) を平文露出しない。"""
    resp = client.get("/api/v1/config")
    assert resp.status_code == 200
    data = resp.json()
    ev = data["env_values"]
    secret_keys = set(data["secret_keys"])
    for k, v in ev.items():
        if k in secret_keys and v:
            assert "discord.com/api/webhooks/" not in v, f"{k} leaked raw secret"
            assert "***" in v or v == "", f"{k} not masked in env_values"


def test_api_v1_runs_pipelines(client: TestClient) -> None:
    resp = client.get("/api/v1/runs/pipelines")
    assert resp.status_code == 200
    data = resp.json()
    assert "pipelines" in data
    assert isinstance(data["pipelines"], list)


def test_api_v1_dashboard_summary(client: TestClient) -> None:
    resp = client.get("/api/v1/dashboard/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert "summary" in data
    assert "recent_runs" in data
    assert "db_stats" in data


def test_api_v1_prompts_lists_summarizer(client: TestClient) -> None:
    resp = client.get("/api/v1/prompts")
    assert resp.status_code == 200
    data = resp.json()
    assert "files" in data
    assert any("briefing/summarizer.j2" in f for f in data["files"])


def test_api_v1_prompts_file_returns_content(client: TestClient) -> None:
    resp = client.get("/api/v1/prompts/file", params={"path": "prompts/briefing/summarizer.j2"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["path"] == "prompts/briefing/summarizer.j2"
    assert "test prompt" in data["content"]


def test_api_v1_config_lists_env_keys(client: TestClient) -> None:
    resp = client.get("/api/v1/config")
    assert resp.status_code == 200
    data = resp.json()
    assert "DISCORD_WEBHOOK_ALERT" in data["env_keys"]
    # シークレットの実値は env_masked 経由でマスクされる (4 文字 + ***)
    masked = data["env_masked"].get("DISCORD_WEBHOOK_ALERT", "")
    assert "https://discord.com/api/webhooks/1/a" not in masked


def test_config_history_endpoints(client: TestClient) -> None:
    """運用 config の変更履歴 + revert (config_store 版履歴の可視化)。

    write (revert) は READ_ONLY middleware が全 POST を一律 403 で弾く前提のため
    ここでは full instance での read + revert の振る舞いのみ検証する。
    """
    from src.storage import config_store

    # 2 版を保存して履歴を作る (cwd は fixture が project に chdir 済 = API と同 DB)
    config_store.save_config("channels", [{"id": "alert"}], note="hist v1")
    config_store.save_config("channels", [{"id": "alert"}, {"id": "brief"}], note="hist v2")

    # key 一覧 (whitelist + 現 version + 版数)
    keys = client.get("/api/v1/config-history").json()["keys"]
    chan = next(k for k in keys if k["key"] == "channels")
    assert chan["label"] == "チャンネル"
    assert chan["version_count"] >= 2
    current = chan["current_version"]
    assert current is not None

    # 履歴 (新しい順、現行マーク)
    hist = client.get("/api/v1/config-history/channels").json()
    assert hist["current_version"] == current
    assert hist["versions"][0]["is_current"] is True
    assert hist["versions"][0]["version"] == current

    # 特定 version の値
    v = client.get(f"/api/v1/config-history/channels/{current}").json()
    assert isinstance(v["value"], list)

    # 未知 key / 存在しない version は 404
    assert client.get("/api/v1/config-history/secret_key").status_code == 404
    assert client.get("/api/v1/config-history/channels/999999").status_code == 404

    # revert: 1 つ前の version を新 version として復元 (履歴は失われない)
    prev = current - 1
    r = client.post("/api/v1/config-history/channels/revert", json={"version": prev})
    assert r.status_code == 200
    body = r.json()
    assert body["reverted"] is True
    assert body["from_version"] == prev
    assert body["new_version"] == current + 1
    # 復元後の現在値 = prev の値
    assert config_store.get_config("channels") == config_store.get_config_version("channels", prev)


def test_match_lists_crud_and_routing_vocab(client: TestClient) -> None:
    """語彙拡張②: match-lists の保存/取得 + routing vocab への反映 + 検証 + 履歴対象。"""
    # 保存
    r = client.post(
        "/api/v1/match-lists",
        json={
            "lists": [
                {"name": "vendors", "description": "監視ベンダ", "terms": ["Fortinet", "FortiOS"]}
            ]
        },
    )
    assert r.status_code == 200
    # 取得
    data = client.get("/api/v1/match-lists").json()
    assert data["lists"][0]["name"] == "vendors"
    assert data["lists"][0]["terms"] == ["Fortinet", "FortiOS"]
    # routing vocab (型付きカタログ) の keyword_list プロパティに list 名が値域として載る
    vocab = client.get("/api/v1/routing-rules").json()["vocabulary"]
    props = {p["id"]: p for p in vocab["properties"]}
    assert props["keyword_list"]["kind"] == "set"
    assert "vendors" in props["keyword_list"]["values"]
    # 不正 (空 name) は 400
    bad = client.post("/api/v1/match-lists", json={"lists": [{"name": "", "terms": ["x"]}]})
    assert bad.status_code == 400
    # config-history の対象 key に match_lists が含まれる
    keys = [k["key"] for k in client.get("/api/v1/config-history").json()["keys"]]
    assert "match_lists" in keys


def test_api_v1_schedule_lists_pipelines(client: TestClient) -> None:
    resp = client.get("/api/v1/schedule")
    assert resp.status_code == 200
    data = resp.json()
    # Phase 2.7: pipeline 単位の schedule 情報が出る
    names = [p["name"] for p in data["pipelines"]]
    assert "direct-rss-fetch" in names
    # 詳細フィールド (think / dedup / triage / cluster_members) すべて返す
    daily = next(p for p in data["pipelines"] if p["name"] == "direct-rss-fetch")
    for key in [
        "max_articles",
        "schedule_enabled",
        "triage_enabled",
        "triage_keep_importance",
        "triage_max_keep",
        "think_enabled",
        "similarity_threshold_hard",
        "similarity_threshold_cluster",
        "dedup_window_hours_hard",
        "dedup_window_hours_cluster",
        "cluster_members",
    ]:
        assert key in daily, f"missing field: {key}"
    assert isinstance(daily["cluster_members"], list)


def test_api_v1_health_status_returns_json(client: TestClient) -> None:
    """health-status はネットワーク失敗時でも JSON で返る。"""
    resp = client.get("/api/v1/health-status")
    assert resp.status_code == 200
    data = resp.json()
    assert "checks" in data
    assert isinstance(data["checks"], list)


def test_api_health_liveness(client: TestClient) -> None:
    """liveness probe (/api/health) は外部 I/O なしで即時 200 を返す。

    docker-compose healthcheck がこのパスを raise_for_status 付きで叩くため、
    ルートが存在し 2xx を返すことを保証する (以前は未実装で 404 だった)。
    """
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_404_returns_html(client: TestClient) -> None:
    resp = client.get("/nonexistent")
    assert resp.status_code == 404


def test_prompt_save_creates_backup_and_commit(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/prompts/save",
        data={"path": "prompts/briefing/summarizer.j2", "content": "edited!"},
    )
    assert resp.status_code == 200
    assert resp.json()["saved"] is True

    project_root = Path.cwd()
    assert (project_root / "prompts" / "briefing/summarizer.j2.bak").exists()
    assert "edited!" in (project_root / "prompts" / "briefing/summarizer.j2").read_text(
        encoding="utf-8",
    )


def test_env_save_updates_only_provided_fields(client: TestClient) -> None:
    resp = client.post("/api/v1/config/env", data={"LOG_LEVEL": "DEBUG"})
    assert resp.status_code == 200
    env_text = (Path.cwd() / ".env").read_text(encoding="utf-8")
    assert "LOG_LEVEL=DEBUG" in env_text
    # 既存のシークレットは残る
    assert "DISCORD_WEBHOOK_ALERT=https://discord.com/api/webhooks/1/a" in env_text


def test_history_delete_removes_finished_run(client: TestClient) -> None:
    """個別 run 削除エンドポイントが finished な run を消すこと。"""
    from datetime import UTC, datetime

    from src.storage.run_history import RunHistoryRepository, RunRecord

    project_root = Path.cwd()
    repo = RunHistoryRepository(db_path=project_root / "data" / "run_history.db")
    run_id = repo.start_run(
        RunRecord(
            started_at=datetime.now(UTC),
            pipeline="daily-briefing",
            dry_run=False,
            triggered_by="manual",
        ),
    )
    repo.finish_run(
        run_id,
        status="succeeded",
        finished_at=datetime.now(UTC),
        total_fetched=0,
        summarized=0,
        posted=0,
        marked_read=0,
        error_count=0,
    )

    resp = client.post(f"/api/v1/history/{run_id}/delete")
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] is True
    assert body["run_id"] == run_id
    assert repo.get_run(run_id) is None


def test_history_delete_running_run_returns_409(client: TestClient) -> None:
    """running な run は削除拒否 (409)。"""
    from datetime import UTC, datetime

    from src.storage.run_history import RunHistoryRepository, RunRecord

    project_root = Path.cwd()
    repo = RunHistoryRepository(db_path=project_root / "data" / "run_history.db")
    run_id = repo.start_run(
        RunRecord(
            started_at=datetime.now(UTC),
            pipeline="daily-briefing",
            dry_run=False,
            triggered_by="manual",
        ),
    )
    # finish_run を呼ばない → running のまま
    resp = client.post(f"/api/v1/history/{run_id}/delete")
    assert resp.status_code == 409
    assert repo.get_run(run_id) is not None


def test_api_v1_dashboard_includes_db_stats(client: TestClient) -> None:
    """ダッシュボード API は db_stats を含む。"""
    resp = client.get("/api/v1/dashboard/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert "db_stats" in data
    assert isinstance(data["db_stats"], dict)


def test_lifespan_runs_vacuum_when_sentinel_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sentinel が無い状態で起動すると VACUUM が走り、sentinel が作られること。"""
    project = _bootstrap_project(tmp_path)
    monkeypatch.setenv("CTI_PROJECT_ROOT", str(project))
    monkeypatch.chdir(project)
    monkeypatch.setenv("DISCORD_WEBHOOK_ALERT", "https://discord.com/api/webhooks/1/a")
    monkeypatch.setenv("DISCORD_WEBHOOK_BRIEF", "https://discord.com/api/webhooks/2/b")
    monkeypatch.setenv("DISCORD_WEBHOOK_WATCH", "https://discord.com/api/webhooks/3/w")
    monkeypatch.setenv("DISCORD_WEBHOOK_OPS", "https://discord.com/api/webhooks/4/o")

    sentinel = project / "data" / ".last_vacuum"
    assert not sentinel.exists()

    from src.ui import app as app_module

    with TestClient(app_module.create_app()) as _:
        pass

    assert sentinel.exists()


def test_history_purge_deletes_old_runs(client: TestClient) -> None:
    """Phase 0 F3: purge は run_logs のみ削除し runs / articles を保持すること。

    旧実装は古い run を一括削除し、紐づく articles を FK CASCADE で全損させていた。
    """
    from datetime import UTC, datetime, timedelta

    from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord

    project_root = Path.cwd()
    repo = RunHistoryRepository(db_path=project_root / "data" / "run_history.db")
    # 古い run と新しい run を作る
    old_id = repo.start_run(
        RunRecord(
            started_at=datetime.now(UTC) - timedelta(days=40),
            pipeline="daily-briefing",
            dry_run=False,
            triggered_by="manual",
        ),
    )
    repo.finish_run(
        old_id,
        status="succeeded",
        finished_at=datetime.now(UTC) - timedelta(days=40),
        total_fetched=0,
        summarized=0,
        posted=0,
        marked_read=0,
        error_count=0,
    )
    # 古い run に article を紐付け (旧実装ならこれが CASCADE で全損していた)
    repo.add_article(
        ArticleRecord(run_id=old_id, article_id="old-art", title="t", url="u", status="posted"),
    )
    new_id = repo.start_run(
        RunRecord(
            started_at=datetime.now(UTC),
            pipeline="daily-briefing",
            dry_run=False,
            triggered_by="manual",
        ),
    )
    repo.finish_run(
        new_id,
        status="succeeded",
        finished_at=datetime.now(UTC),
        total_fetched=0,
        summarized=0,
        posted=0,
        marked_read=0,
        error_count=0,
    )

    resp = client.post("/api/v1/history/purge", data={"days": "30"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["days"] == 30
    assert body["target"] == "run_logs"
    # F3: run も新旧ともに保持される (削除されない)
    assert repo.get_run(old_id) is not None
    assert repo.get_run(new_id) is not None
    # F3: 古い run の article も保持される (旧実装なら CASCADE で消えていた)
    old_arts = repo.list_articles(run_id=old_id, limit=10)
    assert any(a.article_id == "old-art" for a in old_arts)


def test_entity_pivot_endpoint(client: TestClient) -> None:
    """Phase 2 K2: 逆引き pivot — 参照記事 + 共起 actor、query 自身は related から除外。"""
    from datetime import UTC, datetime

    from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord

    repo = RunHistoryRepository(db_path=Path.cwd() / "data" / "run_history.db")
    rid = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False))
    for aid in ("p1", "p2"):
        repo.add_article(
            ArticleRecord(
                run_id=rid,
                article_id=aid,
                title=f"t-{aid}",
                url=f"u-{aid}",
                status="posted",
            ),
        )
    repo.add_article_entities(
        "p1",
        [("ioc_ip", "9.9.9.9"), ("actor", "lazarus"), ("cve", "CVE-2026-9")],
    )
    repo.add_article_entities(
        "p2", [("ioc_ip", "9.9.9.9"), ("actor", "lazarus"), ("actor", "apt41")]
    )

    resp = client.get("/api/v1/pivot", params={"entity_type": "ioc_ip", "value": "9.9.9.9"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["article_count"] == 2
    rel = {r["type"]: {i["value"]: i["count"] for i in r["items"]} for r in data["related"]}
    assert rel["actor"]["lazarus"] == 2
    assert rel["actor"]["apt41"] == 1
    assert rel["cve"]["CVE-2026-9"] == 1
    # query entity 自身 (ioc_ip 9.9.9.9) は related に出ない
    assert "ioc_ip" not in rel

    # 不正な entity_type は 400
    assert (
        client.get("/api/v1/pivot", params={"entity_type": "evil", "value": "x"}).status_code == 400
    )


def test_semantic_search_endpoint(client: TestClient) -> None:
    """Phase 2 K1: 意味的検索 — fake embedder で top-K cosine + url→article 解決。"""
    from datetime import UTC, datetime

    from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord
    from src.tools.embedding_client import EmbeddingClient, EmbeddingResponse

    repo = RunHistoryRepository(db_path=Path.cwd() / "data" / "run_history.db")
    rid = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False))
    seed = [
        ("a1", "https://e/a", [1.0, 0.0], "a" * 32),
        ("a2", "https://e/b", [0.0, 1.0], "b" * 32),
    ]
    for aid, url, vec, h in seed:
        repo.add_article(
            ArticleRecord(
                run_id=rid,
                article_id=aid,
                title=f"t-{aid}",
                url=url,
                status="posted",
                summary=f"summary-{aid}",
            ),
        )
        repo.mark_url_seen(url_hash=h, url=url, title="t")
        repo.add_article_embedding(url_hash=h, url=url, vector=vec, model="fake-embed")

    class _FakeEmbedder(EmbeddingClient):
        @property
        def model(self) -> str:
            return "fake-embed"

        @property
        def dim(self) -> int:
            return 2

        async def embed(self, text: str, *, kind: str = "document") -> EmbeddingResponse:
            return EmbeddingResponse(vector=(1.0, 0.0), model="fake-embed", dim=2)

    client.app.state.embedder = _FakeEmbedder()  # type: ignore[attr-defined]

    resp = client.get(
        "/api/v1/semantic-search",
        params={"query": "china apt telecom", "min_similarity": "0.5"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["model"] == "fake-embed"
    # クエリ [1,0] に近いのは a のみ (b は直交)
    assert data["count"] == 1
    top = data["results"][0]
    assert top["url"] == "https://e/a"
    assert top["article_id"] == "a1"
    assert top["summary"] == "summary-a1"
    assert top["similarity"] >= 0.99


def test_semantic_search_disabled_returns_503(client: TestClient) -> None:
    """OLLAMA_EMBED_MODEL 未設定 (embedder None) のとき 503 を返す。"""
    client.app.state.embedder = None  # type: ignore[attr-defined]
    resp = client.get("/api/v1/semantic-search", params={"query": "anything"})
    assert resp.status_code == 503


def test_article_detail_endpoint(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 2 K4: 記事 deep-view — enrichment + entities + Discord deep-link。"""
    from datetime import UTC, datetime

    from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord

    monkeypatch.setenv("DISCORD_GUILD_ID", "999000")

    repo = RunHistoryRepository(db_path=Path.cwd() / "data" / "run_history.db")
    rid = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=rid,
            article_id="tag:detail",
            title="北朝鮮APTによる暗号資産窃取",
            url="https://e/detail",
            feed_title="Feed X",
            importance="high",
            category="apt",
            status="posted",
            summary="要約テキスト",
            socio_political_intent="financial",
            socio_political_rationale="暗号資産窃取が動機",
            routing_rule_id="R2.alert_japan_critical_apt",
            routing_reason="日本の重要インフラを標的にした APT 活動",
            discord_message_id="msg123",
            discord_channel_id="ch456",
        ),
    )
    # body は別経路 (update_article_body) で永続化する設計。
    repo.update_article_body("tag:detail", body="本文テキスト" * 5)
    repo.add_article_entities(
        "tag:detail",
        [("actor", "lazarus"), ("cve", "CVE-2026-1"), ("ioc_domain", "evil.example")],
    )

    resp = client.get("/api/v1/articles/tag:detail")
    assert resp.status_code == 200
    data = resp.json()
    art = data["article"]
    assert art["title"].startswith("北朝鮮APT")
    assert art["body"].startswith("本文テキスト")
    assert art["socio_political_intent"] == "financial"
    # flow Phase 3: 「なぜこのチャンネルか」(投稿先決定の監査情報) が detail に出る
    assert art["routing_rule_id"] == "R2.alert_japan_critical_apt"
    assert art["routing_reason"] == "日本の重要インフラを標的にした APT 活動"
    # Discord deep-link が guild + channel + message から組まれる
    assert data["discord_url"] == "https://discord.com/channels/999000/ch456/msg123"
    # entities が type 別に束ねられ、actor が上位
    types = [g["type"] for g in data["entities"]]
    assert types[0] == "actor"
    actor_group = next(g for g in data["entities"] if g["type"] == "actor")
    assert actor_group["values"] == ["lazarus"]

    # 存在しない article は 404
    assert client.get("/api/v1/articles/tag:nope").status_code == 404


def test_forecast_endpoint(client: TestClient) -> None:
    """Phase 4: /forecast が spike (FC3) / トレンド (FC4) を返す。"""
    from datetime import UTC, datetime

    from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord

    repo = RunHistoryRepository(db_path=Path.cwd() / "data" / "run_history.db")
    now = datetime.now(UTC)
    # lazarus が直近週に集中 (baseline 0) → spike になる
    for i in range(4):
        rid = repo.start_run(RunRecord(started_at=now, pipeline="x", dry_run=False))
        repo.add_article(
            ArticleRecord(
                run_id=rid,
                article_id=f"fa{i}",
                title="t",
                url=f"https://e/fa{i}",
                status="posted",
                created_at=now,
            )
        )
        repo.add_article_entities(f"fa{i}", [("actor", "lazarus")], when=now)

    resp = client.get("/api/v1/intel-graph/forecast", params={"weeks": "8"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["has_data"] is True
    assert any(t["value"] == "lazarus" for t in data["actor_trends"])
    assert any(s["value"] == "lazarus" for s in data["spike_alerts"])
    assert "indicator_stats" in data


def test_export_articles_csv(client: TestClient) -> None:
    """Phase 2.5 A1: 記事 CSV export (header + enrichment 列 + filter)。"""
    from datetime import UTC, datetime

    from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord

    repo = RunHistoryRepository(db_path=Path.cwd() / "data" / "run_history.db")
    rid = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=rid,
            article_id="csv1",
            title="CSV テスト記事",
            url="https://e/csv1",
            importance="high",
            category="apt",
            status="posted",
            summary="要約",
            socio_political_intent="espionage",
        )
    )
    resp = client.get(
        "/api/v1/export/articles.csv", params={"since_days": "30", "importance": "high"}
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers.get("content-disposition", "")
    body = resp.text
    lines = body.strip().splitlines()
    assert lines[0].startswith("article_id,created_at")
    assert any("csv1" in ln and "espionage" in ln for ln in lines[1:])


def test_notes_crud_endpoint(client: TestClient) -> None:
    """Phase 5: note の PUT → GET → list → 全空で delete。"""
    # PUT (upsert)
    resp = client.put(
        "/api/v1/notes/note-art-1",
        json={
            "bookmarked": True,
            "note": "メモ本文",
            "tags": ["apt", "要追跡"],
            "judgment": "重要先例",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["exists"] is True

    # GET single
    g = client.get("/api/v1/notes/note-art-1")
    assert g.status_code == 200
    assert g.json()["bookmarked"] is True
    assert g.json()["tags"] == ["apt", "要追跡"]

    # list (bookmarked only)
    lst = client.get("/api/v1/notes", params={"bookmarked_only": "true"})
    assert lst.status_code == 200
    ids = [n["article_id"] for n in lst.json()["notes"]]
    assert "note-art-1" in ids

    # 全フィールド空で PUT → delete 扱い
    empty = client.put(
        "/api/v1/notes/note-art-1",
        json={"bookmarked": False, "note": "", "tags": [], "judgment": ""},
    )
    assert empty.json()["exists"] is False
    assert client.get("/api/v1/notes/note-art-1").json()["exists"] is False


def test_unified_search_quick_endpoint(client: TestClient) -> None:
    """検索改善: /api/v1/search?mode=quick が hybrid 結果を返す (LLM 不要)。"""
    from datetime import UTC, datetime

    from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord

    # embedder 無効化 (OLLAMA_EMBED_MODEL 未設定相当) — keyword leg で動く
    client.app.state.embedder = None  # type: ignore[attr-defined]
    repo = RunHistoryRepository(db_path=Path.cwd() / "data" / "run_history.db")
    rid = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=rid,
            article_id="srch1",
            title="中国APTが通信網に侵入した事案",
            url="https://e/srch1",
            category="apt",
            status="posted",
        )
    )
    resp = client.get("/api/v1/search", params={"query": "通信", "mode": "quick"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "quick"
    assert data["reranked"] is False
    assert any(h["article_id"] == "srch1" for h in data["results"])


def test_yaml_save_with_invalid_schema_returns_400(client: TestClient) -> None:
    """invalid YAML を保存しようとすると 400 (EditError → HTTPException)。"""
    resp = client.post(
        "/api/v1/config/yaml",
        data={
            "path": "config/pipelines.yaml",
            "content": "this: is_not_valid: yaml::: bad",
        },
    )
    assert resp.status_code == 400


# React SPA migration: legacy URLs redirect to /app/
def test_intel_graph_legacy_root_redirects_to_app(client: TestClient) -> None:
    resp = client.get("/intel-graph", follow_redirects=False)
    assert resp.status_code in (302, 303)
    assert resp.headers["location"] == "/app/"


def test_intel_graph_pmesii_api_uses_limit_10(client: TestClient) -> None:
    """JSON API /api/v1/intel-graph/pmesii が 10 件 limit で fetch_axis_dashboard を呼ぶ。"""
    from unittest.mock import patch

    with patch(
        "src.ui.routers.intel_graph.fetch_axis_dashboard",
        wraps=__import__(
            "src.ui.services.intel_graph_analytics",
            fromlist=["fetch_axis_dashboard"],
        ).fetch_axis_dashboard,
    ) as mock_fad:
        resp = client.get("/api/v1/intel-graph/pmesii")
        assert resp.status_code == 200
        assert mock_fad.called
        last_call = mock_fad.call_args
        assert last_call.kwargs.get("recent_incident_limit") == 10


def test_editorial_quality_legacy_redirects_to_app(client: TestClient) -> None:
    """/intel-graph/editorial-quality は Operations タブに統合 (redirect)。"""
    resp = client.get("/intel-graph/editorial-quality", follow_redirects=False)
    assert resp.status_code in (302, 303)
    assert resp.headers["location"] == "/app/intel/operations"


def test_api_v1_editorial_quality_returns_json(client: TestClient) -> None:
    """Phase B-R5b 観察: editorial-quality JSON が空 DB でも crash せず schema を返す。"""
    resp = client.get("/api/v1/editorial-quality")
    assert resp.status_code == 200
    data = resp.json()
    assert "feed_rows" in data
    assert "stance_totals" in data
    assert "stances" in data
    assert "propaganda" in data["stances"]
    assert "factual_report" in data["stances"]


def test_editorial_quality_review_submit_creates_record(client: TestClient) -> None:
    """analyst が訂正を submit → DB に review record 作成。"""
    from datetime import UTC, datetime, timedelta

    from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord

    repo = RunHistoryRepository()
    run_id = repo.start_run(
        RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False),
    )
    repo.add_article(
        ArticleRecord(
            run_id=run_id,
            article_id="review-test-1",
            title="x",
            url="https://example.com/x",
            feed_title="Sputnik",
            status="posted",
            editorial_stance="propaganda",
            created_at=datetime.now(UTC) - timedelta(hours=1),
        ),
    )
    resp = client.post(
        "/api/v1/editorial-quality/review",
        data={
            "article_id": "review-test-1",
            "original_stance": "propaganda",
            "corrected_stance": "factual_report",
            "comment": "actually factual deployment report",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["saved"] is True
    review = repo.get_editorial_stance_review("review-test-1")
    assert review is not None
    assert review["corrected_stance"] == "factual_report"


# JSON API v1 tests (React SPA endpoints)
def test_api_v1_snapshot_returns_json(client: TestClient) -> None:
    resp = client.get("/api/v1/intel-graph/snapshot")
    assert resp.status_code == 200
    data = resp.json()
    assert "families" in data
    assert "nations" in data
    assert "discovery" in data
    assert "filters" in data


def test_api_v1_pmesii_returns_json(client: TestClient) -> None:
    resp = client.get("/api/v1/intel-graph/pmesii")
    assert resp.status_code == 200
    data = resp.json()
    assert "cards" in data
    assert isinstance(data["cards"], list)
    # 7 axes (T 軸は 2026-07-16 に廃止 — 監査対処 P1)
    assert len(data["cards"]) == 7
    assert all(c["axis_id"] != "T" for c in data["cards"])


def test_api_v1_synthesis_returns_json(client: TestClient) -> None:
    resp = client.get("/api/v1/intel-graph/synthesis?period_type=daily")
    assert resp.status_code == 200
    data = resp.json()
    assert "period_type" in data
    assert data["period_type"] == "daily"
    assert "has_data" in data


def test_api_v1_threats_returns_json(client: TestClient) -> None:
    resp = client.get("/api/v1/intel-graph/threats")
    assert resp.status_code == 200
    data = resp.json()
    assert "actors" in data
    assert "discovery" in data


def test_api_v1_threats_filter_toggle_off_treats_empty_and_zero_as_false(
    client: TestClient,
) -> None:
    """chip 解除時に渡される '' / '0' / 'false' を Python 側で False と扱う。"""
    r1 = client.get("/api/v1/intel-graph/threats?japan_only=")
    assert r1.status_code == 200
    r2 = client.get("/api/v1/intel-graph/threats?japan_only=0&high_only=false")
    assert r2.status_code == 200
    r3 = client.get("/api/v1/intel-graph/threats?japan_only=1&high_only=1")
    assert r3.status_code == 200


def test_api_v1_actor_detail_unknown_returns_found_false(client: TestClient) -> None:
    resp = client.get("/api/v1/intel-graph/threats/actor/does-not-exist")
    assert resp.status_code == 200
    data = resp.json()
    assert data["found"] is False


# Legacy redirects to React SPA (Discord 過去 link 互換)


def test_intel_graph_legacy_summary_redirects(client: TestClient) -> None:
    resp = client.get("/intel-graph/summary", follow_redirects=False)
    assert resp.status_code in (302, 303)
    assert resp.headers["location"].startswith("/app/")


def test_intel_graph_legacy_actors_redirects(client: TestClient) -> None:
    resp = client.get("/intel-graph/actors", follow_redirects=False)
    assert resp.status_code in (302, 303)
    assert resp.headers["location"].startswith("/app/")


def test_intel_graph_legacy_actor_id_redirects(client: TestClient) -> None:
    resp = client.get("/intel-graph/actor/apt41", follow_redirects=False)
    assert resp.status_code in (302, 303)
    assert "/app/" in resp.headers["location"]
    assert "actor=apt41" in resp.headers["location"]


# Phase H Batch 8: Taxonomy Review UI (React SPA + JSON API)
def test_taxonomy_review_legacy_redirects_to_app(client: TestClient) -> None:
    """/intel-graph/taxonomy-review は Operations タブに統合 (redirect)。"""
    resp = client.get("/intel-graph/taxonomy-review", follow_redirects=False)
    assert resp.status_code in (302, 303)
    assert resp.headers["location"] == "/app/intel/operations"


def test_api_v1_taxonomy_review_returns_empty(client: TestClient) -> None:
    """提案 0 件でも crash せず tier buckets を返す。"""
    resp = client.get("/api/v1/taxonomy-review")
    assert resp.status_code == 200
    data = resp.json()
    assert "tier_1" in data
    assert "tier_2" in data
    assert "tier_3" in data
    assert "recent_reviewed" in data


def test_taxonomy_review_invalid_action_returns_400(client: TestClient) -> None:
    """不正な action は 400 を返す。"""
    resp = client.post("/api/v1/taxonomy-review/1/invalid_action")
    assert resp.status_code == 400


# ---------- 統一ジョブ制御 API (2026-07-06) ----------


def test_api_jobs_lists_all_kinds(client: TestClient) -> None:
    """GET /api/v1/jobs は全ジョブ (K1/K2/K3) をライブ状態つきで返す。"""
    resp = client.get("/api/v1/jobs")
    assert resp.status_code == 200
    data = resp.json()
    jobs = {j["id"]: j for j in data["jobs"]}
    assert "direct-rss-fetch" in jobs
    assert "auto-trigger-synthesis" in jobs
    assert "pir-entity-rebuild" in jobs
    # 各 job に制御に要る field が揃う
    j = jobs["daily-maintenance"]
    assert j["protection"] == "critical"
    assert "schedule_label" in j and "next_run_at" in j
    assert isinstance(data["disabled_important"], list)


def test_api_jobs_toggle_optional(client: TestClient) -> None:
    """optional ジョブは confirm 無しで停止できる。"""
    resp = client.post("/api/v1/jobs/ransomware-live-ingest/toggle", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    # 永続化されており再取得で反映
    jobs = {j["id"]: j for j in client.get("/api/v1/jobs").json()["jobs"]}
    assert jobs["ransomware-live-ingest"]["enabled"] is False


def test_api_jobs_toggle_critical_requires_confirm(client: TestClient) -> None:
    """critical ジョブの停止は confirm 必須 (ミス防止)。"""
    resp = client.post("/api/v1/jobs/daily-maintenance/toggle", json={"enabled": False})
    assert resp.status_code == 409
    ok = client.post(
        "/api/v1/jobs/daily-maintenance/toggle", json={"enabled": False, "confirm": True}
    )
    assert ok.status_code == 200


def test_api_jobs_schedule_validation(client: TestClient) -> None:
    """不正な interval は 400、正常は 200 で schedule_label を返す。"""
    bad = client.post(
        "/api/v1/jobs/direct-rss-fetch/schedule",
        json={"schedule_type": "interval", "interval_minutes": 1},
    )
    assert bad.status_code == 400
    ok = client.post(
        "/api/v1/jobs/direct-rss-fetch/schedule",
        json={"schedule_type": "interval", "interval_minutes": 120},
    )
    assert ok.status_code == 200
    assert "120" in ok.json()["schedule_label"]


def test_api_jobs_unknown_404(client: TestClient) -> None:
    resp = client.post("/api/v1/jobs/nonexistent-job/toggle", json={"enabled": True})
    assert resp.status_code == 404


def test_api_jobs_collection_flag_present(client: TestClient) -> None:
    """GET /api/v1/jobs: 収集ジョブは respects_analysis_window フラグを持つ (動的抑止対象)。"""
    data = client.get("/api/v1/jobs").json()
    jobs = {j["id"]: j for j in data["jobs"]}
    assert jobs["direct-rss-fetch"]["respects_analysis_window"] is True
    # 固定の解析帯 (analysis_window) はレスポンスから撤去済
    assert "analysis_window" not in data


class TestAnthropicKeyEndpoint:
    """外部 LLM API キーの UI 完結編集 (.env 保存・即時反映・平文非返却)。"""

    def test_save_key_enables_external_immediately(self, client: TestClient) -> None:
        before = client.get("/api/v1/model-tiers").json()
        assert before["external_enabled"] is False
        assert before["external"] == []

        resp = client.post(
            "/api/v1/model-tiers/anthropic-key", json={"api_key": "sk-ant-test-1234"}
        )
        assert resp.status_code == 200
        assert resp.json()["external_enabled"] is True

        # 再起動なしで GET に即反映され、外部選択肢が現れる
        after_resp = client.get("/api/v1/model-tiers")
        after = after_resp.json()
        assert after["external_enabled"] is True
        assert any(m.startswith("anthropic:") for m in after["external"])
        # 平文キーはどのレスポンスにも出ない (マスクのみ)
        assert "sk-ant-test-1234" not in after_resp.text
        assert after["anthropic_key_masked"].endswith("***")

    def test_clear_key_blocked_while_tier_assigned(self, client: TestClient) -> None:
        client.post("/api/v1/model-tiers/anthropic-key", json={"api_key": "sk-ant-x-9999"})
        saved = client.post(
            "/api/v1/model-tiers",
            json={
                "tiers": {
                    "reasoning": "gemma4:31b",
                    "fast": "anthropic:claude-haiku-4-5",
                    "dialog": "gemma4:26b",
                    "embedding": "",
                }
            },
        )
        assert saved.status_code == 200

        # 外部割当が残ったままのキー削除は拒否 (run 時に初めて壊れる事故を防ぐ)
        blocked = client.post("/api/v1/model-tiers/anthropic-key", json={"api_key": ""})
        assert blocked.status_code == 400
        assert "ローカル" in blocked.json()["detail"]

        # ローカルへ戻せば削除できる
        client.post(
            "/api/v1/model-tiers",
            json={
                "tiers": {
                    "reasoning": "gemma4:31b",
                    "fast": "gemma4:26b",
                    "dialog": "gemma4:26b",
                    "embedding": "",
                }
            },
        )
        cleared = client.post("/api/v1/model-tiers/anthropic-key", json={"api_key": ""})
        assert cleared.status_code == 200
        assert cleared.json()["external_enabled"] is False


def test_assistant_chat_rejects_forbidden_model_override(client: TestClient) -> None:
    """会話単位 model override も denylist を通る (LLM 呼出前に 400)。"""
    resp = client.post(
        "/api/v1/assistant/chat",
        json={"message": "test", "model": "qwen3:32b"},
    )
    assert resp.status_code == 400
    assert "禁止" in resp.json()["detail"]


class TestReadOnlyAllowlist:
    """readonly instance の write 遮断と分析チャット例外 (2026-07-19)。"""

    @pytest.fixture
    def ro_client(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
        project = _bootstrap_project(tmp_path)
        monkeypatch.setenv("CTI_PROJECT_ROOT", str(project))
        monkeypatch.chdir(project)
        monkeypatch.setenv("READ_ONLY", "1")
        from src.ui import app as app_module

        monkeypatch.setattr(app_module, "READ_ONLY_FLAG", True)
        fresh_app = app_module.create_app()
        with TestClient(fresh_app) as c:
            yield c

    def test_generic_post_still_blocked(self, ro_client: TestClient) -> None:
        resp = ro_client.post("/api/v1/model-tiers", json={"tiers": {}})
        assert resp.status_code == 403

    def test_assistant_chat_not_blocked(self, ro_client: TestClient) -> None:
        # allowlist で middleware は通過する (LLM 不在の test 環境では 200 は返らないが
        # 403 でないこと = write 遮断の例外が効いていることを検証)
        resp = ro_client.post("/api/v1/assistant/chat", json={"message": "hi"})
        assert resp.status_code != 403

    def test_article_translate_not_blocked(self, ro_client: TestClient) -> None:
        # 本文オンデマンド翻訳 (2026-07-25): モバイル閲覧が主用途のため readonly でも
        # middleware を通過する (存在しない記事なので 404 = endpoint まで到達した証拠)
        resp = ro_client.post("/api/v1/articles/nonexistent/translate")
        assert resp.status_code == 404

    def test_other_article_posts_still_blocked(self, ro_client: TestClient) -> None:
        # /translate 以外の articles 配下 POST は従来どおり 403
        resp = ro_client.post("/api/v1/articles/nonexistent/other")
        assert resp.status_code == 403

    def test_mobile_tunnel_named_config_blocked(self, ro_client: TestClient) -> None:
        # named tunnel の token/hostname 保存は full instance 限定。公開 readonly では 403
        # (write middleware が POST を遮断 = 公開面から token を設定/上書きできない)。
        resp = ro_client.post(
            "/api/v1/mobile-tunnel/named-config", json={"hostname": "x.example.com"}
        )
        assert resp.status_code == 403

    def test_mobile_tunnel_named_config_clear_blocked(self, ro_client: TestClient) -> None:
        resp = ro_client.post("/api/v1/mobile-tunnel/named-config/clear")
        assert resp.status_code == 403
