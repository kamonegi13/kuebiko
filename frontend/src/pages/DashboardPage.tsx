// Intelligence Overview ダッシュボード (Phase 3: 明示座標グリッド)。
// 設計: docs/dashboard_redesign_intelligence_overview.md
// 原則: Intel Graph タブ=各次元の深掘り。Dashboard=各タブ要点を compact widget に凝縮した
// 「統合された一望」。各 widget は deep-dive を持たず Intel Graph へドリルダウン。
// widget 実装は dashboard/registry.tsx + dashboard/widgets/*.tsx (このファイルは page shell)。
//
// レイアウト機構 (v2, 2026-07-10): react-grid-layout による明示座標 (x,y,w,h) の 2D グリッド。
// 旧 v1 (順序+span+実測 masonry) は編集 chrome を含んで中身を測る → 保存で chrome が消えて
// 再計算 → ズレが連鎖する構造的欠陥があったため廃止。編集/表示が同一座標を使うので
// 「保存するとズレる」が構造的に起きない。中身は tile に追従 (超過は内部スクロール)。
// モバイルは従来どおり 1 列 content 駆動 (座標は並び順 y にのみ使用)。

import { ChevronsDownUp, Save, Settings, X } from "lucide-react";
import { pageContainer } from "../components/Page";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { MouseEvent as ReactMouseEvent } from "react";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import GridLayout, { verticalCompactor } from "react-grid-layout";
import type { Layout, LayoutItem } from "react-grid-layout";
import "react-grid-layout/css/styles.css";
import "react-resizable/css/styles.css";
import {
  dashboardLayoutApi, loadMobileLayout, saveMobileLayout, loadPcLayout, savePcLayout, clearPcLayout,
  type DashboardLayout, type StoredDashboardLayout, type StoredWidgetPlacement,
  type WidgetPlacement, type LegacyWidgetPlacement,
} from "../api/dashboard";
import { useIsMobile } from "../hooks/useIsMobile";
import { useWebSocket } from "../hooks/useWebSocket";
import { WIDGET_REGISTRY } from "./dashboard/registry";
import { WidgetThumbnail } from "./dashboard/WidgetThumbnail";
import { GlobalWindowSelector } from "./dashboard/GlobalWindow";
import { ConfigOptionEditor, ViewSettingsPill } from "./dashboard/ViewSettings";
import { pruneWidgetView, useWidgetViewAll } from "./dashboard/widgetView";
import type { WidgetDef } from "./dashboard/shared";

// 他 page (History/Run/Schedule) が dashboard 由来の StatusBadge を使うため再 export。
export { StatusBadge } from "./dashboard/shared";

// グリッド定数 (backend dashboard_layout.py の GRID_COLS / 検証範囲と一致させる)。
// 12 カラム × 8px 行、margin 12px (旧 gap-3 と同じ余白) → 1 行増加あたり実効 20px。
const COLS = 12;
const ROW_HEIGHT_PX = 8;
const MARGIN_PX = 12;
const UNIT_PX = ROW_HEIGHT_PX + MARGIN_PX;
const MIN_W_UNITS = 2;
const MIN_H_UNITS = 3;
// v1 で auto 高さだった widget / defaultHeight 未指定 widget の初期 tile 高さ (px)。
const DEFAULT_TILE_PX = 300;

// px 高さ → 行数 (tile 実高 = h*8 + (h-1)*12 なので逆算)。
function pxToUnits(px: number): number {
  return Math.max(MIN_H_UNITS, Math.round((px + MARGIN_PX) / UNIT_PX));
}
// registry の初期寸法 → グリッド単位。defaultSpan は 4 カラム基準なので ×3。
function defaultW(def: WidgetDef): number {
  return Math.max(MIN_W_UNITS, Math.min(COLS, def.defaultSpan * 3));
}
function defaultH(def: WidgetDef): number {
  return pxToUnits(def.defaultHeight ?? DEFAULT_TILE_PX);
}

export function DashboardPage() {
  const qc = useQueryClient();
  const isMobile = useIsMobile();
  const { data: serverLayout } = useQuery({
    queryKey: ["dashboard-layout"],
    queryFn: () => dashboardLayoutApi.get(),
  });
  // モバイル専用レイアウト (localStorage)。PC (server) とは独立に保存・編集できる。
  // 初回 (localStorage 未設定) は server レイアウトを起点にする。
  const [mobileLayout, setMobileLayout] = useState(() => loadMobileLayout());
  const [pcLayout, setPcLayout] = useState(() => loadPcLayout());
  // 実効レイアウト: localStorage(この端末) > server(共有 default)。モバイル/PC 別キーで
  // 端末ごとに保持できる。旧 widget rename + v1(span)→v2(座標) の移行も load 時に適用。
  const rawLayout = isMobile ? (mobileLayout ?? serverLayout) : (pcLayout ?? serverLayout);
  const layout = useMemo(() => migrateLayout(rawLayout), [rawLayout]);
  useWebSocket((ev) => {
    if (ev.type === "article_posted" || ev.type === "pipeline_complete" || ev.type === "pipeline_running") {
      qc.invalidateQueries({ queryKey: ["dashboard"] });
      qc.invalidateQueries({ queryKey: ["article-feed"] });
      qc.invalidateQueries({ queryKey: ["dash-history"] });
    }
    if (ev.type === "synthesis_ready") {
      qc.invalidateQueries({ queryKey: ["dash-synthesis"] });
      qc.invalidateQueries({ queryKey: ["dash-pmesii"] });
      qc.invalidateQueries({ queryKey: ["dash-spotlight"] });
    }
    if (ev.type === "actor_new" || ev.type === "spike_alert") {
      qc.invalidateQueries({ queryKey: ["dash-snapshot"] });
      qc.invalidateQueries({ queryKey: ["dash-threats"] });
      qc.invalidateQueries({ queryKey: ["dash-pmesii"] });
      qc.invalidateQueries({ queryKey: ["pir-dashboard"] });
    }
  });

  // 閲覧時の widget 個別設定 (端末ローカル)。編集中は base (layout.config) を見せる。
  const viewAll = useWidgetViewAll();
  // ⚙ ピルの発見性: 表示直後の数秒だけ全ピルを見せてから hover 表示に落とす。
  const [pillIntro, setPillIntro] = useState(true);
  useEffect(() => {
    const t = window.setTimeout(() => setPillIntro(false), 3500);
    return () => window.clearTimeout(t);
  }, []);
  useEffect(() => {
    if (layout) pruneWidgetView(layout.widgets.map((w) => w.uid ?? w.id));
  }, [layout]);

  const [editing, setEditing] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false); // mobile: 追加ピッカー (bottom sheet) の開閉
  const [draft, setDraft] = useState<WidgetPlacement[]>([]);
  // ツールボックスから grid へドラッグ中の widget id (droppingItem の寸法決定に使う)。
  const [dragNewId, setDragNewId] = useState<string | null>(null);
  useEffect(() => {
    // mobile は並び順が編集対象なので (y,x) で整列してから draft にする。
    if (layout) setDraft(isMobile ? sortByPos(layout.widgets) : layout.widgets);
  }, [layout, isMobile]);

  // grid コンテナ幅 (react-grid-layout は明示 width が必須)。RGL 同梱の useContainerWidth は
  // 実測前の initialWidth (1280px) に張り付く事象があったため、自前の ResizeObserver で確実に測る。
  // callback ref (state): コンテナは layout ロード後にしか mount しないため、ref の出現に追従する。
  const [gridEl, setGridEl] = useState<HTMLDivElement | null>(null);
  const [gridWidth, setGridWidth] = useState(0);
  useLayoutEffect(() => {
    if (!gridEl) return;
    const update = () => setGridWidth(gridEl.getBoundingClientRect().width);
    update();
    const ro = new ResizeObserver(update);
    ro.observe(gridEl);
    return () => ro.disconnect();
  }, [gridEl]);

  // ツールボックスのライブプレビュー: hover した widget を実データで縮小描画 (~220ms 遅延・1個)。
  const hoverTimer = useRef<number | null>(null);
  const [preview, setPreview] = useState<{ id: string; top: number; left: number } | null>(null);
  function onItemEnter(id: string, e: ReactMouseEvent<HTMLElement>) {
    const r = e.currentTarget.getBoundingClientRect();
    if (hoverTimer.current) window.clearTimeout(hoverTimer.current);
    hoverTimer.current = window.setTimeout(() => setPreview({ id, top: r.top, left: r.right + 8 }), 220);
  }
  function onItemLeave() {
    if (hoverTimer.current) {
      window.clearTimeout(hoverTimer.current);
      hoverTimer.current = null;
    }
    setPreview(null);
  }

  const saveMut = useMutation({
    mutationFn: (widgets: WidgetPlacement[]) => dashboardLayoutApi.put({ widgets }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["dashboard-layout"] }),
    // エラー (外部 readonly の 403 等) は handleSave 側で localStorage fallback として扱う。
  });

  // 保存先: モバイル=この端末の localStorage。PC=まず server を試し、失敗 (外部 readonly の
  // 403 等) なら**この端末の localStorage に fallback**保存 (= 端末ごとにレイアウトを持てる)。
  // 書込可能 PC (ローカル) は server 保存成功時に端末 override を消し共有 default に追従。
  async function handleSave(widgets: WidgetPlacement[]) {
    if (isMobile) {
      // モバイルは並び順のみが意味を持つ → y を連番へ正規化 (x/w/h は PC 用の値を温存しない
      // — mobile レイアウトは独立キーなので PC 座標を壊さない)。
      const normalized = widgets.map((w, i) => ({ ...w, x: 0, y: i }));
      saveMobileLayout({ widgets: normalized });
      setMobileLayout({ widgets: normalized });
      setEditing(false);
      return;
    }
    try {
      await saveMut.mutateAsync(widgets);
      clearPcLayout(); // server 同期成功 → 端末 override は消す
      setPcLayout(null);
    } catch {
      savePcLayout({ widgets }); // server write 不可 → この端末に保存 (per-device)
      setPcLayout({ widgets });
    }
    setEditing(false);
  }

  // ── draft 操作 (uid ベース: 並び順に依存しない) ──
  function removeW(uid: string) {
    setDraft((prev) => prev.filter((w) => w.uid !== uid));
  }
  function moveW(uid: string, delta: number) {
    setDraft((prev) => {
      const idx = prev.findIndex((w) => w.uid === uid);
      const j = idx + delta;
      if (idx < 0 || j < 0 || j >= prev.length) return prev;
      const next = [...prev];
      [next[idx], next[j]] = [next[j], next[idx]];
      return next;
    });
  }
  function setCfg(uid: string, key: string, value: unknown) {
    setDraft((prev) => prev.map((w) => (w.uid === uid ? { ...w, config: { ...(w.config ?? {}), [key]: value } } : w)));
  }
  function appendWidget(id: string) {
    const def = WIDGET_REGISTRY[id];
    setDraft((prev) => [...prev, {
      id, uid: newUid(), x: 0, y: bottomY(prev), w: defaultW(def), h: defaultH(def),
      config: { ...(def.defaultConfig ?? {}) },
    }]);
  }

  // ── 高さを中身にぴったり合わせる (fit) ──
  // ユーザー操作起点の一回きりの実測 → h 設定。v1 の「測定がレイアウトを駆動し続ける」構造
  // とは異なり feedback loop が無い (測るのは widget の自然高 scrollHeight、chrome は含めない
  // ため保存後にちょうど収まる)。fill 型 (地図) は自然高が無いので対象外。
  const contentRefs = useRef<Record<string, HTMLDivElement | null>>({});
  function fitHeight(uid: string) {
    const el = contentRefs.current[uid];
    if (!el || el.scrollHeight <= 0) return;
    // 切り上げ: 丸め下げで中身が 1-2px 欠けるのを防ぐ
    const h = Math.max(MIN_H_UNITS, Math.ceil((el.scrollHeight + MARGIN_PX) / UNIT_PX));
    setDraft((prev) => prev.map((w) => (w.uid === uid ? { ...w, h } : w)));
  }
  function fitAllHeights() {
    for (const w of draft) {
      if (w.uid && WIDGET_REGISTRY[w.id] && !WIDGET_REGISTRY[w.id].fill) fitHeight(w.uid);
    }
  }

  // RGL からの座標更新を draft へ反映 (uid=LayoutItem.i で対応付け)。
  // 表示モードでも compaction 正規化で発火しうるため編集中のみ反映する。
  function onLayoutChange(next: Layout) {
    if (!editing) return;
    setDraft((prev) => prev.map((w) => {
      const item = next.find((l) => l.i === (w.uid ?? w.id));
      return item && (item.x !== w.x || item.y !== w.y || item.w !== w.w || item.h !== w.h)
        ? { ...w, x: item.x, y: item.y, w: item.w, h: item.h }
        : w;
    }));
  }

  // ツールボックスからのドロップ: RGL が算出した座標で新規配置。
  function onDrop(_layout: Layout, item: LayoutItem | undefined, e: Event) {
    e.preventDefault();
    if (!dragNewId || !item) return;
    const def = WIDGET_REGISTRY[dragNewId];
    setDraft((prev) => [...prev, {
      id: dragNewId, uid: newUid(), x: item.x, y: item.y, w: item.w, h: item.h,
      config: { ...(def.defaultConfig ?? {}) },
    }]);
    setDragNewId(null);
  }

  if (!layout) return <div className="p-8 text-fg-muted">読み込み中...</div>;

  // 不明 id (registry に無い) は描画しない (draft には温存され保存で失われない)。
  const visible = (editing ? draft : (isMobile ? sortByPos(layout.widgets) : layout.widgets))
    .filter((w) => WIDGET_REGISTRY[w.id]);
  // multi-instance: 同一 widget を設定違いで複数配置できるよう、toolbox は全 widget を常に提示。
  const available = Object.keys(WIDGET_REGISTRY);

  const rglLayout: Layout = visible.map((w) => ({
    i: w.uid ?? w.id, x: w.x, y: w.y, w: w.w, h: w.h, minW: MIN_W_UNITS, minH: MIN_H_UNITS,
  }));
  // 表示モードは保存座標を vertical compaction で正規化して描画 (編集モードと同一規則なので
  // 通常は no-op)。旧移行や手修正で生じた隙間/重なりを決定論的に解消する。
  const displayLayout: Layout = editing ? rglLayout : verticalCompactor.compact(rglLayout, COLS);
  const droppingDef = dragNewId ? WIDGET_REGISTRY[dragNewId] : null;

  // ツールボックスの widget リスト (desktop=左 overlay / mobile=下部 sheet の双方で共有)。
  const widgetItems = editing ? (
    <div className="space-y-1">
      {available.map((id) => {
        const def = WIDGET_REGISTRY[id];
        // single (非 multi) は配置済みなら追加不可。multi は設定違いで何個でも置ける。
        const placedCount = visible.filter((w) => w.id === id).length;
        const addable = !!def.multi || placedCount === 0;
        return (
          <button key={id}
            draggable={addable && !isMobile}
            onDragStart={addable && !isMobile ? (e) => {
              e.dataTransfer.setData("text/plain", id); // Firefox は data 必須
              setDragNewId(id);
            } : undefined}
            onDragEnd={() => setDragNewId(null)}
            onClick={addable ? () => appendWidget(id) : undefined}
            onMouseEnter={!isMobile && addable ? (e) => onItemEnter(id, e) : undefined}
            onMouseLeave={isMobile ? undefined : onItemLeave}
            className={`w-full text-left flex items-center gap-2 border rounded p-1.5 transition-colors ${addable ? "border-border-subtle hover:border-accent/60 hover:bg-accent-subtle md:cursor-grab md:active:cursor-grabbing" : "border-border-subtle opacity-50 cursor-not-allowed"}`}>
            <div className="w-14 shrink-0"><WidgetThumbnail kind={def.thumb} /></div>
            <span className="min-w-0 flex-1">
              <span className="block text-xs font-medium text-fg leading-snug truncate">{def.title}</span>
              {def.multi ? (
                <span className="block text-[9px] text-accent">複数配置可{placedCount > 0 ? ` ・${placedCount}個配置中` : ""}</span>
              ) : placedCount > 0 ? (
                <span className="block text-[9px] text-fg-subtle">配置済み (1つのみ)</span>
              ) : null}
            </span>
          </button>
        );
      })}
    </div>
  ) : null;

  return (
    <div className={`${pageContainer("wide")} space-y-4`}>
      <div className="flex items-baseline justify-between gap-2 flex-wrap">
        <div className="flex items-baseline gap-2 flex-wrap">
          <h2 className="m-0 text-xl font-bold text-fg tracking-tight">Intelligence Overview</h2>
          {isMobile && (
            <span className="text-[10px] uppercase tracking-wider bg-accent-subtle text-accent-hover px-1.5 py-0.5 rounded font-mono" title="モバイル専用レイアウト (この端末に保存・PC とは独立)">モバイル</span>
          )}
        </div>
        <div className="flex items-center gap-3 text-sm">
          {/* 全体の対象期間 (ページ chrome に常設 — widget を外しても消えない) */}
          <GlobalWindowSelector />
          {!isMobile && pcLayout && !editing && (
            <span
              className="inline-flex items-center gap-1.5 text-[10px] bg-warning-soft text-warning px-1.5 py-0.5 rounded"
              title="サーバへ保存できなかったため、この端末内に保存されたレイアウトを表示しています。共有版 (サーバ) の更新は反映されません"
            >
              この端末専用レイアウト
              <button
                onClick={() => { clearPcLayout(); setPcLayout(null); }}
                className="underline hover:no-underline"
              >
                共有版に戻す
              </button>
            </span>
          )}
          {editing ? (
            /* mobile は下部バーが 保存/取消 を担うので header には出さない (重複回避) */
            !isMobile && (
              <>
                <button onClick={() => handleSave(draft)} disabled={saveMut.isPending}
                  className="inline-flex items-center gap-1 bg-accent hover:bg-accent-hover text-white px-3 py-1 rounded text-xs font-semibold disabled:opacity-50">
                  {saveMut.isPending ? "保存中…" : <><Save className="h-3.5 w-3.5" /> 保存</>}
                </button>
                <button onClick={() => { setDraft(layout.widgets); setEditing(false); }}
                  className="text-fg-subtle hover:text-fg text-xs">取消</button>
              </>
            )
          ) : (
            <button onClick={() => setEditing(true)}
              className="inline-flex items-center gap-1 border border-border-subtle text-fg hover:bg-surface-2 px-3 py-1 rounded text-xs font-semibold">
              <Settings className="h-3.5 w-3.5" /> カスタマイズ
            </button>
          )}
        </div>
      </div>

      {/* 編集ツールボックス (desktop=左オーバーレイ)。grid は全幅のまま。 */}
      {editing && !isMobile && (
        <aside className="fixed left-0 top-0 bottom-0 w-60 z-[80] !mt-0 overflow-y-auto bg-surface-2 border-r border-border-emphasized shadow-2xl p-2">
          <div className="flex items-center justify-between gap-1 px-1 mt-1 mb-1">
            <span className="text-[11px] uppercase tracking-wider text-fg-muted">ツールボックス</span>
            <div className="flex items-center gap-1.5">
              <button onClick={() => handleSave(draft)} disabled={saveMut.isPending}
                className="inline-flex items-center gap-1 bg-accent hover:bg-accent-hover text-white px-2 py-1 rounded text-[11px] font-semibold disabled:opacity-50"
                title="レイアウトを保存して終了">
                {saveMut.isPending ? "保存中…" : <><Save className="h-3 w-3" /> 保存</>}
              </button>
              <button onClick={() => { setDraft(layout.widgets); setEditing(false); }}
                className="text-fg-subtle hover:text-fg" title="破棄して終了">
                <X className="h-4 w-4" />
              </button>
            </div>
          </div>
          <div className="text-[10px] text-fg-subtle px-1 mb-2 leading-snug">
            クリックで最後に追加 / ドラッグで好きな位置へ / マウスを乗せると実物プレビュー
          </div>
          <button onClick={fitAllHeights}
            className="w-full mb-2 inline-flex items-center justify-center gap-1 border border-border-subtle rounded px-2 py-1 text-[11px] text-fg hover:bg-surface-1 hover:border-accent/60"
            title="全タイルの高さを中身の自然な高さにぴったり合わせる (地図など全面表示のものは除く)">
            <ChevronsDownUp className="h-3 w-3" /> 全タイルの高さを中身に合わせる
          </button>
          {widgetItems}
        </aside>
      )}

      {/* 編集ツールバー (mobile=下部固定)。grid を覆わないので各 widget の編集バー
          (削除/上下移動) が常に操作可能。「+ 追加」で展開式ピッカー (bottom sheet)。 */}
      {editing && isMobile && (
        <div className="fixed inset-x-0 bottom-0 z-[95] bg-surface-1 border-t border-border-emphasized shadow-2xl pb-[env(safe-area-inset-bottom)]">
          {pickerOpen && (
            <div className="max-h-[55vh] overflow-y-auto border-b border-border-subtle p-2">
              <div className="flex items-center justify-between px-1 mb-1.5">
                <span className="text-[11px] uppercase tracking-wider text-fg-muted">ウィジェットを追加</span>
                <button onClick={() => setPickerOpen(false)} className="text-fg-subtle hover:text-fg"><X className="h-4 w-4" /></button>
              </div>
              <div className="text-[10px] text-fg-subtle px-1 mb-2">タップで最後に追加 (配置済みは無効)</div>
              {widgetItems}
            </div>
          )}
          <div className="flex items-center gap-2 px-3 py-2">
            <button onClick={() => setPickerOpen((o) => !o)}
              className={`inline-flex items-center gap-1 rounded px-3 py-1.5 text-xs font-semibold ${pickerOpen ? "bg-accent text-white" : "border border-accent/50 text-accent"}`}>
              {pickerOpen ? "閉じる" : "+ ウィジェット追加"}
            </button>
            <button onClick={() => { setPickerOpen(false); handleSave(draft); }}
              className="ml-auto inline-flex items-center gap-1 rounded bg-accent px-3 py-1.5 text-xs font-semibold text-white">
              <Save className="h-3.5 w-3.5" /> 保存
            </button>
            <button onClick={() => { setDraft(sortByPos(layout.widgets)); setEditing(false); setPickerOpen(false); }}
              className="text-xs text-fg-subtle hover:text-fg">取消</button>
          </div>
        </div>
      )}

      {/* ── widget grid ── */}
      {isMobile ? (
        /* mobile: 1 列 content 駆動 (座標は並び順にのみ使用)。 */
        <div className="space-y-3">
          {visible.map((w, idx) => {
            const def = WIDGET_REGISTRY[w.id];
            const key = w.uid ?? w.id;
            const base = { ...(def.defaultConfig ?? {}), ...(w.config ?? {}) };
            // 閲覧時は view overrides を重ねる。編集中は base (共有既定) を編集・表示。
            const cfg = editing ? base : { ...base, ...(viewAll[key] ?? {}) };
            return (
              <div key={key} className="min-w-0 relative">
                {editing && (
                  <TileChrome
                    def={def} cfg={cfg} isMobile
                    isFirst={idx === 0} isLast={idx === visible.length - 1}
                    onUp={() => moveW(key, -1)} onDown={() => moveW(key, +1)}
                    onRemove={() => removeW(key)}
                    onConfigChange={(k, v) => setCfg(key, k, v)}
                  />
                )}
                {!editing && (
                  <ViewSettingsPill def={def} cfg={cfg} uid={key}
                    overridden={Object.keys(viewAll[key] ?? {}).length > 0} mobile intro={pillIntro} />
                )}
                <def.Component config={cfg} mobile />
              </div>
            );
          })}
        </div>
      ) : (
        /* desktop: 明示座標グリッド。編集/表示とも同一の座標・同一の compaction で描画する。 */
        <div ref={setGridEl}>
          {gridWidth > 0 && (
            <GridLayout
              width={gridWidth}
              layout={displayLayout}
              gridConfig={{ cols: COLS, rowHeight: ROW_HEIGHT_PX, margin: [MARGIN_PX, MARGIN_PX], containerPadding: [0, 0] }}
              dragConfig={{ enabled: editing, handle: ".dash-drag" }}
              resizeConfig={{ enabled: editing, handles: ["e", "s", "se"] }}
              dropConfig={{ enabled: editing && !!droppingDef }}
              droppingItem={droppingDef ? {
                i: "__dropping__", x: 0, y: 0, w: defaultW(droppingDef), h: defaultH(droppingDef),
              } : undefined}
              onDrop={onDrop}
              onLayoutChange={onLayoutChange}
            >
              {visible.map((w) => {
                const def = WIDGET_REGISTRY[w.id];
                const key = w.uid ?? w.id;
                const base = { ...(def.defaultConfig ?? {}), ...(w.config ?? {}) };
                // 閲覧時は view overrides を重ねる。編集中は base (共有既定) を編集・表示。
                const cfg = editing ? base : { ...base, ...(viewAll[key] ?? {}) };
                return (
                  <div key={key} className="flex flex-col min-w-0 relative group">
                    {editing && (
                      <TileChrome
                        def={def} cfg={cfg} isMobile={false}
                        isFirst={false} isLast={false}
                        onUp={() => {}} onDown={() => {}}
                        onRemove={() => removeW(key)}
                        onConfigChange={(k, v) => setCfg(key, k, v)}
                        onFit={def.fill ? undefined : () => fitHeight(key)}
                      />
                    )}
                    {!editing && (
                      <ViewSettingsPill def={def} cfg={cfg} uid={key}
                        overridden={Object.keys(viewAll[key] ?? {}).length > 0} intro={pillIntro} />
                    )}
                    {/* 中身は tile に追従。スクロールは WidgetCard/Shell の内側で行う
                        (ここでスクロールさせるとカード背景ごと動いて破綻する) */}
                    <div className="flex-1 min-h-0 overflow-hidden"
                      ref={(el) => { contentRefs.current[key] = el; }}>
                      <def.Component config={cfg} />
                    </div>
                  </div>
                );
              })}
            </GridLayout>
          )}
        </div>
      )}

      {/* ツールボックス項目の hover ライブプレビュー (実 widget・実データ・縮小描画・非操作) */}
      {editing && preview && (
        <div className="fixed z-[90] w-[380px] !mt-0 pointer-events-none"
          style={{
            top: Math.max(8, Math.min(preview.top, window.innerHeight - 300)),
            left: Math.max(8, Math.min(preview.left, window.innerWidth - 396)),
          }}>
          <div className="bg-surface-2 border border-border-emphasized rounded-lg shadow-2xl p-2">
            <div className="text-[10px] uppercase tracking-wider text-fg-subtle mb-1 px-1">
              プレビュー — {WIDGET_REGISTRY[preview.id].title}
            </div>
            <div className="max-h-[260px] overflow-hidden rounded">
              {(() => {
                const C = WIDGET_REGISTRY[preview.id].Component;
                return <C config={WIDGET_REGISTRY[preview.id].defaultConfig ?? {}} />;
              })()}
            </div>
          </div>
        </div>
      )}
      {visible.length === 0 && !editing && (
        <div className="text-fg-subtle text-sm italic p-8 text-center">
          ウィジェットがありません。「カスタマイズ」で追加してください。
        </div>
      )}
    </div>
  );
}

// ── tile の編集 chrome (タイトルバー + config select 行) ──
function TileChrome({ def, cfg, isMobile, isFirst, isLast, onUp, onDown, onRemove, onConfigChange, onFit }: {
  def: WidgetDef; cfg: Record<string, unknown>; isMobile: boolean;
  isFirst: boolean; isLast: boolean;
  onUp: () => void; onDown: () => void; onRemove: () => void;
  onConfigChange: (key: string, value: string) => void;
  onFit?: () => void; // 高さを中身に合わせる (desktop・非 fill 型のみ)
}) {
  return (
    <div className="shrink-0">
      <div className="flex items-center gap-1.5 mb-1 px-1 text-[11px] text-fg-muted">
        {isMobile ? (
          <span className="font-semibold truncate flex-1">{def.title}</span>
        ) : (
          <span className="dash-drag flex-1 min-w-0 flex items-center gap-1 cursor-grab active:cursor-grabbing select-none" title="ドラッグで移動">
            <span className="text-fg-subtle">⠿</span>
            <span className="font-semibold truncate">{def.title}</span>
          </span>
        )}
        {isMobile && (
          <>
            <button onClick={onUp} disabled={isFirst} className="px-1 disabled:opacity-30 hover:text-fg" title="上へ">↑</button>
            <button onClick={onDown} disabled={isLast} className="px-1 disabled:opacity-30 hover:text-fg" title="下へ">↓</button>
          </>
        )}
        {onFit && (
          <button onClick={onFit} className="px-1 hover:text-fg" title="高さを中身に合わせる">
            <ChevronsDownUp className="h-3.5 w-3.5" />
          </button>
        )}
        <button onClick={onRemove} className="px-1 text-critical hover:opacity-70" title="削除"><X className="h-3.5 w-3.5" /></button>
      </div>
      {def.configOptions && def.configOptions.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 mb-1 px-1">
          {def.configOptions.map((opt) => (
            <ConfigOptionEditor key={opt.key} opt={opt}
              value={String(cfg[opt.key] ?? "")} onChange={(v) => onConfigChange(opt.key, v)} />
          ))}
        </div>
      )}
    </div>
  );
}

// ── レイアウト移行 ──
// (1) v1 (順序+span+高さpx) → v2 (x,y,w,h 座標)。1 つでも legacy がいれば全体を旧フロー
//     グリッドの並び順で座標化する (以後は vertical compaction が隙間を詰める)。
//     端末 localStorage に古い保存が残りうるため退役させない (退役条件 = v1 形式の
//     localStorage が世に残っていないと確認できたとき)。
// (2) uid 採番 (multi-instance の安定 key)。
// (退役 2026-07-14: pmesii_spike→situation の一度きり rename。未移行の端末が万一
//  あっても registry に無い id は描画されず draft には温存される = 破壊しない)
function migrateLayout(stored: StoredDashboardLayout | undefined | null): DashboardLayout | undefined {
  if (!stored) return undefined;
  const widgets: StoredWidgetPlacement[] = stored.widgets;
  const v2 = widgets.some(isLegacyPlacement) ? legacyToCoords(widgets) : (widgets as WidgetPlacement[]);
  return { widgets: v2.map((w) => (w.uid ? w : { ...w, uid: newUid() })) };
}

// legacy 判定: v2 は必ず数値の x/w を持つ (backend が extra=forbid で保証)。
function isLegacyPlacement(w: StoredWidgetPlacement): w is LegacyWidgetPlacement {
  return typeof (w as WidgetPlacement).x !== "number" || typeof (w as WidgetPlacement).w !== "number";
}

// v1 の並び (左→右に詰め、収まらなければ次の行) を素朴に座標へ写す。
// 高さは 明示px > registry defaultHeight > 推定値 の順。移行後はユーザが自由に調整する。
function legacyToCoords(widgets: StoredWidgetPlacement[]): WidgetPlacement[] {
  const out: WidgetPlacement[] = [];
  let x = 0;
  let y = 0;
  let rowMaxH = 0;
  for (const src of widgets) {
    const legacy = isLegacyPlacement(src);
    const span = legacy ? (src.span ?? 2) : Math.max(1, Math.min(4, Math.round(src.w / 3)));
    const wUnits = Math.max(MIN_W_UNITS, Math.min(COLS, span * 3));
    const def = WIDGET_REGISTRY[src.id] as WidgetDef | undefined;
    const hUnits = legacy
      ? pxToUnits(src.h ?? def?.defaultHeight ?? DEFAULT_TILE_PX)
      : src.h;
    if (x + wUnits > COLS) {
      x = 0;
      y += rowMaxH;
      rowMaxH = 0;
    }
    out.push({ id: src.id, uid: src.uid, x, y, w: wUnits, h: hUnits, config: src.config });
    x += wUnits;
    rowMaxH = Math.max(rowMaxH, hUnits);
  }
  return out;
}

// (y, x) の読み順に整列 (mobile の 1 列 stack 用)。
function sortByPos(widgets: WidgetPlacement[]): WidgetPlacement[] {
  return [...widgets].sort((a, b) => a.y - b.y || a.x - b.x);
}

// 末尾追加時の y (最下端の次)。
function bottomY(widgets: WidgetPlacement[]): number {
  return widgets.reduce((m, w) => Math.max(m, w.y + w.h), 0);
}

// widget インスタンスの一意 ID (同一 widget を複数配置する際の安定 React key)。
function newUid(): string {
  const c = globalThis.crypto;
  return c && "randomUUID" in c ? c.randomUUID() : `w-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}
