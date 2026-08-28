"""公開面の遡及を Sonnet で実行する (v6: caveats + 3 関門 + プロンプト対の保存)。

対象: 公開一覧 (high・直近 1 か月) のうち、最新版が v6 形式 (caveats キー) を
持たないもの全部 — 未生成 (元記事の要約を表示中) と旧フォーマットの両方。
目的は 2 つ: 読者に見える面を最高品質で揃える / SFT の教師対を一括で貯める。
"""

import asyncio, json, sys, time

sys.path.insert(0, "/app")
from datetime import UTC, datetime, timedelta
from src.config_loader import load_app_config
from src.eventnews import runner
from src.storage.run_history import RunHistoryRepository
from src.tools.model_tiers import Step, build_llm_for, build_llm_for_ref
from src.ui.services.eventnews_hourly_job import _entity_counts, _load_members


async def main():
    # 対象 id の一覧 (引数で差し替え可能 — 一部だけ作り直したいときに使う)
    ids = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "/tmp/backfill_ids.json"))
    # モデルを固定したいとき (Opus 教師層など) は第 2 引数で ref を渡す。
    # **ティア設定は変えない** — 変えると毎時の生成まで巻き込む。
    model_ref = sys.argv[2] if len(sys.argv) > 2 else ""
    if model_ref:
        print(f"モデル固定: {model_ref}", flush=True)
    repo = RunHistoryRepository()
    recs = {
        r.state.item_id: r
        for r in repo.list_event_items(origin="live", limit=20000)
        if not r.merged_into
    }
    counts = _entity_counts(repo, datetime.now(UTC) - timedelta(days=14))
    pending = []
    already = 0
    for item_id in ids:
        rec = recs.get(item_id)
        if rec is None:
            continue
        v = repo.list_event_versions(item_id)
        # fallback (→gemma4:31b) 版は教師データとして数えない — Sonnet で作り直す
        done = bool(v) and bool(v[0].body_json) and '"caveats"' in v[0].body_json
        if model_ref:
            # 固定モデル指定時は「そのモデルで作られたか」で判定する
            done = done and v[0].model == model_ref
        else:
            done = done and "→" not in v[0].model
        if done:
            already += 1
            continue
        mm = _load_members(repo, list(rec.state.member_ids), counts)
        members = [mm[x] for x in rec.state.member_ids if x in mm]
        if members:
            pending.append((rec.state, members))
    print(f"公開 {len(ids)} 件 / v6 済み {already} / 対象 {len(pending)} 件", flush=True)
    t0 = time.monotonic()

    def progress(i, total, item_id):
        el = time.monotonic() - t0
        eta = (el / max(1, i - 1)) * (total - i + 1) if i > 1 else 0
        if i % 10 == 0 or i == 1:
            print(f"[{i}/{total}] 経過{el / 60:.0f}分 残り約{eta / 60:.0f}分", flush=True)

    # 移動枠 (4-5 時間の転がる窓) に**自己調整**する:
    #   探針 1 件 → fallback なら 30 分退いて再試行 (枠が回復したら自然に通る)
    #   通れば 25 件の塊で進み、塊の fallback 率 > 20% でまた退く
    # (実測 2026-08-27: 連続 ~75 呼出/時 + 開発セッションの併用で fallback 率 24%)
    # Opus は 1 件が重く枠を食うので、塊を小さく休みを長くする。
    # 4 時間の移動枠に当たったら退く自己調整はそのまま効く。
    heavy = "opus" in model_ref
    CHUNK, BACKOFF, REST = (10, 1800, 240) if heavy else (25, 1800, 120)
    #: 別々の項目で何件連続して落ちたら「経路が使えない」とみなすか
    PROBE_STREAK = 3
    #: 塊の末尾が何件連続で落ちたら「経路が使えない」とみなすか
    ROUTE_DOWN_STREAK = 5

    def _now_iso():
        return datetime.now(UTC).isoformat()

    def outcome(chunk, since):
        """(fallback 件数, 新しく版が書かれた件数) を返す。

        ⚠ **``since`` 以降に書かれた版だけを見る**。生成が skip された項目
        (単独報・本文なし) は版が増えないので、以前の版の model をそのまま
        読んでしまい、古い fallback 表記を「今回の失敗」と誤読して同じ項目で
        永久に足踏みする (2026-08-28 に実際に 2 回退いたまま停止した)。
        """
        fb = wrote = 0
        for state, _ in chunk:
            v = repo.list_event_versions(state.item_id)
            if not v or v[0].generated_at.isoformat() < since:
                continue
            wrote += 1
            if "→" in v[0].model:
                fb += 1
        return fb, wrote

    def trailing_fallbacks(chunk, since):
        """塊の末尾で連続して fallback した件数 (生成された版だけを数える)。

        経路が使えないときは以降ずっと落ちるので末尾が伸びる。散発的な拒否では
        伸びない — この 2 つを率で区別しようとすると、拒否が集中する残り物で
        必ず誤判定する。
        """
        n = 0
        for state, _ in reversed(chunk):
            v = repo.list_event_versions(state.item_id)
            if not v or v[0].generated_at.isoformat() < since:
                continue  # 生成対象外は判定材料にしない
            if "→" not in v[0].model:
                break
            n += 1
        return n

    if model_ref:
        factory = lambda: build_llm_for_ref(model_ref, Step.EVENT_NEWS, load_app_config())
    else:
        factory = lambda: build_llm_for(Step.EVENT_NEWS, load_app_config())
    total_done = 0
    i = 0
    probe_failures = 0
    while i < len(pending):
        # 探針: 1 件だけ生成して fallback を見る
        probe = pending[i : i + 1]
        since = _now_iso()
        await runner.generate_pending(repo, probe, factory)
        fb, wrote = outcome(probe, since)
        if fb:
            # ⚠ **同じ項目で足踏みしない**。拒否は入力ごとの事象で、後で試しても
            #    同じ結果になる (2026-08-28 実測: 1 件の拒否で 30 分退き続けた)。
            #    別の項目で連続して落ちたときだけ「経路が使えない」と判断する。
            probe_failures += 1
            i += 1
            total_done += 1
            if probe_failures >= PROBE_STREAK:
                print(
                    f"探針が {probe_failures} 件連続で fallback — "
                    f"{BACKOFF // 60} 分待つ (累計 {total_done}/{len(pending)})",
                    flush=True,
                )
                await asyncio.sleep(BACKOFF)
                probe_failures = 0
            continue
        probe_failures = 0
        if wrote == 0:
            # 生成対象外 (本文なし等)。判定材料にならないので次へ進める
            i += 1
            total_done += 1
            continue
        i += 1
        total_done += 1
        chunk = pending[i : i + CHUNK]
        if chunk:
            since = _now_iso()
            await runner.generate_pending(
                repo,
                chunk,
                factory,
                on_progress=lambda a, b, iid: progress(total_done + a, len(pending), iid),
            )
            fb, wrote = outcome(chunk, since)
            tail = trailing_fallbacks(chunk, since)
            i += len(chunk)
            total_done += len(chunk)
            print(
                f"塊: {len(chunk)} 件 (生成 {wrote} / fallback {fb}) "
                f"累計 {total_done}/{len(pending)}",
                flush=True,
            )
            # ⚠ **fallback 率では退かない**。拒否は記事の内容ごとに起きるので、
            #    残り物ほど拒否が集中して率が上がる — 経路は正常なのに退き続ける。
            #    経路が使えないときは「以降ずっと落ちる」ので、**末尾が連続で
            #    落ちているか**だけを見る (2026-08-28)。
            if tail >= ROUTE_DOWN_STREAK:
                print(f"末尾 {tail} 件が連続 fallback — {BACKOFF // 60} 分退く", flush=True)
                await asyncio.sleep(BACKOFF)
            else:
                await asyncio.sleep(REST)
    print(f"完了: 所要 {(time.monotonic() - t0) / 60:.0f} 分", flush=True)


asyncio.run(main())
