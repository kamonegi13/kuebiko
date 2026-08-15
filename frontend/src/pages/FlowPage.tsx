// 情報フロー (/app/flow) — 関心から配信まで (PIR → 重要度 → 配信ルール → チャンネル) を
// 実流量つきで図示し、軽い調整 (PIR の評価影響 / ルール順序・投稿先 / チャンネル有効) を
// その場で行うハブページ。深い編集は各専用ページ (PIR 編集 / ルーティング / チャンネル) へ。
//
// エッジは DOM 実測 (getBoundingClientRect + ResizeObserver) の SVG ベジェ。lg 未満では
// SVG を描かず縦積み (ステージ間は ↓ 見出しで接続、390px 横スクロールなし)。

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, BellOff, Pencil, Plus, Trash2 } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { pageContainer } from "../components/Page";
import { useRuntimeFlags } from "../hooks/useRuntimeFlags";
import { flowApi, type FlowPir } from "../api/flow";
import { routingRulesApi, type PreviewResponse, type RoutingRule } from "../api/routingRules";
import { channelsApi, type ChannelDef } from "../api/channels";
import { ChannelChip, channelColor, useChannelMeta } from "../components/channel";
import { Drawer } from "../components/Drawer";
import { ProductRoutingCard } from "./channels/ProductRoutingCard";
import { MatchListsView } from "./config/MatchListsView";
import { BriefDeliveryPanel } from "../components/BriefDeliveryPanel";
import {
  type EditRule,
  emitWhen,
  parseWhen,
  summarizeWhen,
} from "./routing/clauseModel";
import { RuleEditorFields } from "./routing/RuleEditorFields";
import { ChannelEditForm } from "./channels/ChannelEditForm";
import { ChannelWebhookField, WebhookStatusDot } from "./channels/ChannelWebhookField";
import { vocabLabel } from "../hooks/useVocab";

// ---------- 色 (tailwind.config.ts のパレットと同期) ----------

const IMP_COLOR: Record<string, string> = {
  high: "#ff6b6b",
  medium: "#6b88ff",
  low: "#6b7280",
  unknown: "#4a5260",
};
// チャンネル色は共有 channelColor() を使う (CH_COLOR の重複定義を廃止)。
const FALLBACK_COLOR = "#4a5260";
const IMP_ORDER = ["high", "medium", "low", "unknown"];

const PIR_COLLAPSED_COUNT = 8;
// SVG エッジを描く最小コンテナ幅 (これ未満 = 縦積みモード)
const MIN_GRAPH_WIDTH = 900;

function edgeWidth(count: number): number {
  return Math.max(1.5, Math.min(9, 1.5 + Math.sqrt(count)));
}

interface Edge {
  key: string;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  color: string;
  count: number;
  dashed: boolean;
  faint: boolean;
}

function edgePath(e: Edge): string {
  const bend = Math.max(24, (e.x2 - e.x1) * 0.45);
  return `M ${e.x1} ${e.y1} C ${e.x1 + bend} ${e.y1}, ${e.x2 - bend} ${e.y2}, ${e.x2} ${e.y2}`;
}

// setEdges の無駄な更新 (= 再レンダーループ) を防ぐ等価判定
function edgesEqual(a: Edge[], b: Edge[]): boolean {
  if (a.length !== b.length) return false;
  return a.every((e, i) => {
    const o = b[i];
    return (
      e.key === o.key &&
      e.x1 === o.x1 &&
      e.y1 === o.y1 &&
      e.x2 === o.x2 &&
      e.y2 === o.y2 &&
      e.count === o.count &&
      e.faint === o.faint
    );
  });
}

// ---------- メインページ ----------

export function FlowPage() {
  const flags = useRuntimeFlags();
  const readOnly = flags.read_only;
  const [days, setDays] = useState(7);
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["flow", days],
    queryFn: () => flowApi.get(days),
  });

  const qc = useQueryClient();
  const [selectedPir, setSelectedPir] = useState<string | null>(null);
  const [showAllPirs, setShowAllPirs] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  // Drawer 編集 (条件/チャンネル全項目)。vocab と full channel registry は専用 API から取得。
  const { data: routingMeta } = useQuery({ queryKey: ["routing-rules"], queryFn: routingRulesApi.get });
  const { data: channelReg } = useQuery({ queryKey: ["channels"], queryFn: channelsApi.get });
  // webhook 疎通 (GET 検証・実投稿なし)。チャンネルカードの疎通ドットに使う。
  const { data: chHealth } = useQuery({
    queryKey: ["channels-health"],
    queryFn: channelsApi.health,
    refetchInterval: 120_000,
  });
  const vocab = routingMeta?.vocabulary;
  // index=null = 新規追加
  const [ruleDraft, setRuleDraft] = useState<{ index: number | null; rule: EditRule } | null>(null);
  const [chDraft, setChDraft] = useState<{ index: number | null; channel: ChannelDef } | null>(null);
  const [rulePreview, setRulePreview] = useState<PreviewResponse | null>(null);
  const channelMeta = useChannelMeta();

  // --- ルールのローカル編集 state (順序 / 投稿先のみ。条件編集はルーティングへ) ---
  const [rules, setRules] = useState<RoutingRule[] | null>(null);
  const [rulesDirty, setRulesDirty] = useState(false);
  const serverRules = data?.rules;
  useEffect(() => {
    if (serverRules) {
      setRules(serverRules.map((r) => ({ ...r })));
      setRulesDirty(false);
    }
  }, [serverRules]);

  const sortedPirs = useMemo(() => {
    const list = [...(data?.pirs ?? [])];
    list.sort((a, b) => Number(b.enabled) - Number(a.enabled) || b.matched_total - a.matched_total);
    return list;
  }, [data]);
  // measure の useCallback deps に入るため参照安定が必須 (毎レンダー新配列だと
  // effect → setEdges → 再レンダーの無限ループになる)
  const visiblePirs = useMemo(
    () => (showAllPirs ? sortedPirs : sortedPirs.slice(0, PIR_COLLAPSED_COUNT)),
    [sortedPirs, showAllPirs],
  );

  const importanceKeys = useMemo(() => {
    const present = new Set(Object.keys(data?.importance_totals ?? {}));
    for (const e of data?.importance_channel ?? []) present.add(e.importance);
    return IMP_ORDER.filter((k) => k === "high" || k === "medium" || k === "low" || present.has(k));
  }, [data]);

  // ---------- エッジ実測 ----------

  const containerRef = useRef<HTMLDivElement | null>(null);
  const pirRefs = useRef<Map<string, HTMLDivElement | null>>(new Map());
  const impRefs = useRef<Map<string, HTMLElement | null>>(new Map());
  const ladderRef = useRef<HTMLDivElement | null>(null);
  const channelRefs = useRef<Map<string, HTMLDivElement | null>>(new Map());
  const [edges, setEdges] = useState<Edge[]>([]);

  const applyEdges = useCallback((next: Edge[]) => {
    setEdges((prev) => (edgesEqual(prev, next) ? prev : next));
  }, []);

  const measure = useCallback(() => {
    const container = containerRef.current;
    const ladder = ladderRef.current;
    if (!container || !ladder || !data) return applyEdges([]);
    const base = container.getBoundingClientRect();
    if (base.width < MIN_GRAPH_WIDTH) return applyEdges([]);
    const next: Edge[] = [];

    const center = (el: HTMLElement, side: "left" | "right") => {
      const r = el.getBoundingClientRect();
      return {
        x: (side === "left" ? r.left : r.right) - base.left,
        y: r.top + r.height / 2 - base.top,
      };
    };

    // PIR → importance (点線 = 「評価基準の注入 + 実マッチ数」、決定論ではない)
    for (const pir of visiblePirs) {
      const el = pirRefs.current.get(pir.id);
      if (!el) continue;
      const from = center(el, "right");
      for (const [imp, count] of Object.entries(pir.by_importance)) {
        const impEl = impRefs.current.get(imp);
        if (!impEl || count <= 0) continue;
        const to = center(impEl, "left");
        next.push({
          key: `pir:${pir.id}:${imp}`,
          x1: from.x,
          y1: from.y,
          x2: to.x,
          y2: to.y,
          color: IMP_COLOR[imp] ?? FALLBACK_COLOR,
          count,
          dashed: true,
          faint: selectedPir !== null && selectedPir !== pir.id,
        });
      }
    }

    // importance → 配信ルール (実流量)
    const ladderLeft = center(ladder, "left");
    for (const imp of importanceKeys) {
      const impEl = impRefs.current.get(imp);
      const count = data.importance_totals[imp] ?? 0;
      if (!impEl || count <= 0) continue;
      const from = center(impEl, "right");
      next.push({
        key: `imp:${imp}`,
        x1: from.x,
        y1: from.y,
        x2: ladderLeft.x,
        y2: from.y * 0.35 + ladderLeft.y * 0.65,
        color: IMP_COLOR[imp] ?? FALLBACK_COLOR,
        count,
        dashed: false,
        faint: false,
      });
    }

    // 配信ルール → channel (実流量 = posted_count)
    const ladderRight = center(ladder, "right");
    for (const ch of data.channels) {
      const chEl = channelRefs.current.get(ch.id);
      if (!chEl || ch.posted_count <= 0) continue;
      const to = center(chEl, "left");
      next.push({
        key: `ch:${ch.id}`,
        x1: ladderRight.x,
        y1: to.y * 0.35 + ladderRight.y * 0.65,
        x2: to.x,
        y2: to.y,
        color: channelColor(ch.id),
        count: ch.posted_count,
        dashed: false,
        faint: false,
      });
    }
    applyEdges(next);
  }, [data, visiblePirs, importanceKeys, selectedPir, applyEdges]);

  useLayoutEffect(() => {
    measure();
    // 初回ロード時、AppShell の幅確定や web フォント適用が paint 後に来ることがあり、
    // 最初の measure が width 不足 (MIN_GRAPH_WIDTH 未満) で空になることがある。
    // 2 フレーム後に再計測してレイアウト確定後の座標を拾う。
    const rafs: number[] = [];
    rafs.push(
      requestAnimationFrame(() => {
        measure();
        rafs.push(requestAnimationFrame(measure));
      }),
    );
    const onResize = () => measure();
    window.addEventListener("resize", onResize);

    const container = containerRef.current;
    const ladder = ladderRef.current;
    let observer: ResizeObserver | undefined;
    if (typeof ResizeObserver !== "undefined") {
      observer = new ResizeObserver(() => measure());
      // container 幅 + ladder 高さ (PIR 全表示などで変わる) の両方を監視
      if (container) observer.observe(container);
      if (ladder) observer.observe(ladder);
    }
    return () => {
      rafs.forEach(cancelAnimationFrame);
      window.removeEventListener("resize", onResize);
      observer?.disconnect();
    };
  }, [measure]);

  // ---------- 編集 mutation ----------
  // PIR はインテリジェンス (関心の定義) なのでフローでは編集しない (PIR ページで著作)。
  // ここでは配信 (ルール / チャンネル) のみ編集する。

  // ルールセット全体を保存 (順序変更・投稿先・Drawer 編集すべて同じ経路)。
  const saveRulesMut = useMutation({
    mutationFn: (rs: RoutingRule[]) => routingRulesApi.save(rs),
    onSuccess: (_d, rs) => {
      setRules(rs);
      setRulesDirty(false);
      setRuleDraft(null);
      setMsg("配信ルールを保存しました");
      qc.invalidateQueries({ queryKey: ["routing-rules"] });
      refetch();
    },
    onError: (e: unknown) => setMsg(`ルール保存失敗: ${e instanceof Error ? e.message : String(e)}`),
  });

  // チャンネルセット全体を保存 (Drawer 編集・追加・削除)。
  const saveChannelsMut = useMutation({
    mutationFn: (chs: ChannelDef[]) => channelsApi.save(chs),
    onSuccess: () => {
      setChDraft(null);
      setMsg("チャンネルを更新しました");
      qc.invalidateQueries({ queryKey: ["channels"] });
      refetch();
    },
    onError: (e: unknown) =>
      setMsg(`チャンネル更新失敗: ${e instanceof Error ? e.message : String(e)}`),
  });

  // チャンネルの有効/無効トグル (full registry を patch して保存)。
  const toggleChannelMut = useMutation({
    mutationFn: async (args: { id: string; enabled: boolean }) => {
      const current = channelReg ?? (await channelsApi.get());
      return channelsApi.save(
        current.channels.map((c) => (c.id === args.id ? { ...c, enabled: args.enabled } : c)),
      );
    },
    onSuccess: () => {
      setMsg("チャンネルを更新しました");
      qc.invalidateQueries({ queryKey: ["channels"] });
      refetch();
    },
    onError: (e: unknown) =>
      setMsg(`チャンネル更新失敗: ${e instanceof Error ? e.message : String(e)}`),
  });

  // ---- Drawer 保存ハンドラ ----
  // draft を適用した候補ルールセットを組み立てる (preview / save で共用)。
  const buildCandidateRules = (mode: "save" | "delete"): RoutingRule[] | null => {
    if (!ruleDraft || !rules) return null;
    const built: RoutingRule = {
      id: ruleDraft.rule.id,
      label: ruleDraft.rule.label.trim() || undefined,
      channel: ruleDraft.rule.channel,
      when: emitWhen(ruleDraft.rule.root),
    };
    if (mode === "delete" && ruleDraft.index !== null) {
      return rules.filter((_, i) => i !== ruleDraft.index);
    }
    if (ruleDraft.index === null) return [...rules, built];
    return rules.map((r, i) => (i === ruleDraft.index ? built : r));
  };

  const previewRuleMut = useMutation({
    mutationFn: (rs: RoutingRule[]) => routingRulesApi.preview(rs),
    onSuccess: (resp) => setRulePreview(resp),
    onError: (e: unknown) => setMsg(`プレビュー失敗: ${e instanceof Error ? e.message : String(e)}`),
  });

  const persistRule = (mode: "save" | "delete") => {
    const next = buildCandidateRules(mode);
    if (next) saveRulesMut.mutate(next);
  };

  const persistChannel = (mode: "save" | "delete") => {
    if (!chDraft || !channelReg) return;
    const base = channelReg.channels;
    let next: ChannelDef[];
    if (mode === "delete" && chDraft.index !== null) {
      next = base.filter((_, i) => i !== chDraft.index);
    } else if (chDraft.index === null) {
      next = [...base, chDraft.channel];
    } else {
      next = base.map((c, i) => (i === chDraft.index ? chDraft.channel : c));
    }
    saveChannelsMut.mutate(next);
  };

  const openRuleDrawer = (idx: number) => {
    const r = rules?.[idx];
    if (!r) return;
    setRulePreview(null);
    setRuleDraft({
      index: idx,
      rule: { id: r.id, label: r.label ?? "", channel: r.channel, root: parseWhen(r.when) },
    });
  };
  const openNewRuleDrawer = () => {
    const ch = vocab?.channels[0] ?? "watch";
    setRulePreview(null);
    setRuleDraft({
      index: null,
      rule: {
        id: "new_rule",
        label: "",
        channel: ch,
        root: { kind: "group", mode: "all", children: [], negated: false },
      },
    });
  };
  const openChannelDrawer = (id: string) => {
    const idx = channelReg?.channels.findIndex((c) => c.id === id) ?? -1;
    const c = idx >= 0 ? channelReg!.channels[idx] : null;
    if (!c) return;
    setChDraft({ index: idx, channel: { ...c } });
  };
  const openNewChannelDrawer = () => {
    setChDraft({
      index: null,
      channel: {
        id: "",
        label: "",
        webhook_env_key: "",
        enabled: true,
        routable: true,
        push: true,
        fallback: null,
        order: 99,
      },
    });
  };

  const moveRule = (idx: number, dir: -1 | 1) => {
    setRules((rs) => {
      if (!rs) return rs;
      const j = idx + dir;
      if (j < 0 || j >= rs.length) return rs;
      const next = [...rs];
      [next[idx], next[j]] = [next[j], next[idx]];
      return next;
    });
    setRulesDirty(true);
  };
  const setRuleChannel = (idx: number, channel: string) => {
    setRules((rs) => (rs ? rs.map((r, i) => (i === idx ? { ...r, channel } : r)) : rs));
    setRulesDirty(true);
  };

  if (isLoading) return <div className={pageContainer("wide")}>読み込み中…</div>;
  if (error || !data || !rules)
    return <div className={pageContainer("wide")}>読み込みに失敗しました</div>;

  const routableChannels = data.channels.filter((c) => c.routable).map((c) => c.id);

  return (
    <div className={`${pageContainer("wide")} space-y-4 pb-10`}>
      {/* ヘッダ: 簡潔なタイトル + 1 行説明 + 期間セレクタ */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="m-0 text-xl font-bold text-fg tracking-tight">情報フロー</h2>
          <p className="m-0 mt-1 text-sm text-fg-muted">
            関心 (PIR) から配信チャンネルまでの流れと実際の記事数。配信ルールとチャンネルはここで編集します。
          </p>
        </div>
        <select
          value={days}
          onChange={(e) => setDays(parseInt(e.target.value, 10))}
          className="shrink-0 rounded border border-border-subtle bg-surface-2 px-2 py-1 text-sm text-fg"
        >
          {[7, 14, 30].map((d) => (
            <option key={d} value={d}>
              直近 {d} 日
            </option>
          ))}
        </select>
      </div>

      {msg && (
        <div className="rounded border border-border-subtle bg-surface-2 px-3 py-1.5 text-sm text-fg-muted">
          {msg}
        </div>
      )}

      {/* フロー本体 */}
      <div ref={containerRef} className="relative">
        <svg className="pointer-events-none absolute inset-0 hidden h-full w-full overflow-visible lg:block">
          {edges.map((e) => (
            <g key={e.key}>
              <path
                d={edgePath(e)}
                fill="none"
                stroke={e.color}
                strokeWidth={e.dashed ? 1.5 : edgeWidth(e.count)}
                strokeDasharray={e.dashed ? "4 4" : undefined}
                opacity={e.faint ? 0.06 : e.dashed ? 0.5 : 0.3}
              />
              {!e.dashed && !e.faint && (
                <text
                  x={(e.x1 + e.x2) / 2}
                  y={(e.y1 + e.y2) / 2 - 6}
                  fill={e.color}
                  fontSize="10"
                  textAnchor="middle"
                  opacity={0.9}
                >
                  {e.count}
                </text>
              )}
            </g>
          ))}
        </svg>

        <div className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(220px,1.1fr)_110px_minmax(260px,1.3fr)_150px] lg:gap-10">
          {/* ---- 列 1: 関心 (PIR) ---- */}
          <section>
            <StageHeader
              title="関心 (PIR)"
              detail="インテリジェンス — クリックで編集"
              href="/app/pir"
            />
            <div className="space-y-1.5">
              {visiblePirs.map((pir) => (
                <PirCard
                  key={pir.id}
                  pir={pir}
                  selected={selectedPir === pir.id}
                  refFn={(el) => {
                    pirRefs.current.set(pir.id, el);
                  }}
                  onSelect={() =>
                    setSelectedPir((cur) => (cur === pir.id ? null : pir.id))
                  }
                />
              ))}
            </div>
            {sortedPirs.length > PIR_COLLAPSED_COUNT && (
              <button
                onClick={() => setShowAllPirs((v) => !v)}
                className="mt-2 text-xs text-accent hover:underline"
              >
                {showAllPirs
                  ? "上位のみ表示"
                  : `すべて表示 (残り ${sortedPirs.length - PIR_COLLAPSED_COUNT} 件)`}
              </button>
            )}
          </section>

          <MobileArrow label="記事の重要度が判定される（記事の選別）" />

          {/* ---- 列 2: 評価 (重要度) ---- */}
          <section className="flex flex-row gap-2 lg:flex-col lg:justify-center lg:gap-8">
            <div className="hidden lg:block">
              <StageHeader title="評価" detail="重要度" />
            </div>
            {importanceKeys.map((imp) => (
              <a
                key={imp}
                ref={(el) => {
                  impRefs.current.set(imp, el);
                }}
                href={`/app/news?importance=${imp}&since=${days * 24}`}
                className="flex-1 rounded-lg border border-border-subtle bg-surface-2 px-2 py-2 text-center no-underline hover:border-border-emphasized lg:flex-none"
                style={{ borderLeftColor: IMP_COLOR[imp] ?? FALLBACK_COLOR, borderLeftWidth: 3 }}
                title={`重要度 ${vocabLabel("importance", imp)} の記事一覧を見る`}
              >
                <div className="text-sm font-semibold text-fg">{vocabLabel("importance", imp)}</div>
                <div className="tnum text-xs text-fg-subtle">
                  {data.importance_totals[imp] ?? 0} 件
                </div>
              </a>
            ))}
          </section>

          <MobileArrow label="重要度と検出シグナルで配信先が決まる（上から順に最初に一致したルール）" />

          {/* ---- 列 3: 配信ルール (ladder) ---- */}
          {/* 評価・チャンネル列と同様に縦中央寄せ。PIR を全表示して左列が伸びても
              ladder が上端に張り付かず、importance/channel と高さが揃って線が読みやすい。 */}
          <section className="lg:flex lg:flex-col lg:justify-center">
            <StageHeader
              title="配信 (ルール)"
              detail="上から順に最初に一致したルールを適用。鉛筆アイコンで条件を編集"
            />
            <div
              ref={ladderRef}
              className={`rounded-lg border p-2 ${data.engine_enabled ? "border-border-subtle bg-surface-2" : "border-warning bg-warning-soft"}`}
            >
              {!data.engine_enabled && (
                <div className="mb-1.5 inline-flex items-center gap-1 text-[11px] text-warning">
                  <AlertTriangle className="h-3.5 w-3.5" /> ルールエンジン無効 — 表示中のルールは適用されていません
                </div>
              )}
              <ol className="m-0 list-none space-y-1 p-0">
                {rules.map((r, i) => (
                  <li
                    key={i}
                    className="flex items-center gap-1.5 rounded border border-border-subtle/60 bg-surface-1 px-1.5 py-1"
                  >
                    <span className="tnum w-4 shrink-0 text-right text-[11px] text-fg-subtle">
                      {i + 1}
                    </span>
                    {/* 主表示はルール名。条件式は 2 行目に添える (名前が無いと 14 本の
                        ルールが条件式の羅列になり、どれが何か読めなくなる)。 */}
                    <span className="min-w-0 flex-1" title={`${r.id}: ${summarizeWhen(r.when)}`}>
                      <span className="block truncate text-xs text-fg">{r.label || r.id}</span>
                      <span className="block truncate text-[11px] text-fg-subtle">
                        {summarizeWhen(r.when)}
                      </span>
                    </span>
                    <select
                      value={r.channel}
                      disabled={readOnly}
                      onChange={(e) => setRuleChannel(i, e.target.value)}
                      className="shrink-0 rounded border border-border-subtle bg-surface-2 px-1 py-0.5 text-[11px] text-fg"
                    >
                      {routableChannels.map((c) => (
                        <option key={c} value={c}>
                          {channelMeta(c).label}
                        </option>
                      ))}
                    </select>
                    {!readOnly && (
                      <span className="flex shrink-0 items-center">
                        <button
                          onClick={() => moveRule(i, -1)}
                          disabled={i === 0}
                          className="px-0.5 text-[11px] text-fg-subtle hover:text-fg disabled:opacity-30"
                          title="評価順を上へ"
                        >
                          ↑
                        </button>
                        <button
                          onClick={() => moveRule(i, 1)}
                          disabled={i === rules.length - 1}
                          className="px-0.5 text-[11px] text-fg-subtle hover:text-fg disabled:opacity-30"
                          title="評価順を下へ"
                        >
                          ↓
                        </button>
                        <button
                          onClick={() => openRuleDrawer(i)}
                          className="ml-0.5 text-fg-subtle hover:text-accent"
                          title="条件を編集"
                        >
                          <Pencil className="h-3 w-3" />
                        </button>
                      </span>
                    )}
                  </li>
                ))}
              </ol>
              {!readOnly && (
                <button
                  onClick={openNewRuleDrawer}
                  className="mt-1.5 inline-flex items-center gap-1 text-[11px] text-accent hover:underline"
                >
                  <Plus className="h-3 w-3" /> ルールを追加
                </button>
              )}
              {!readOnly && rulesDirty && (
                <div className="mt-2 flex items-center gap-2">
                  <button
                    onClick={() => {
                      if (rules && confirm("配信ルール (順序・投稿先) を保存します。よろしいですか?"))
                        saveRulesMut.mutate(rules);
                    }}
                    disabled={saveRulesMut.isPending}
                    className="rounded bg-accent px-2.5 py-1 text-xs font-semibold text-white hover:bg-accent-hover disabled:opacity-50"
                  >
                    {saveRulesMut.isPending ? "保存中…" : "ルールを保存"}
                  </button>
                  <button
                    onClick={() => {
                      setRules(serverRules ? serverRules.map((r) => ({ ...r })) : rules);
                      setRulesDirty(false);
                    }}
                    className="text-xs text-fg-subtle hover:text-fg"
                  >
                    破棄
                  </button>
                </div>
              )}
            </div>
            <div className="mt-1.5 text-[11px] text-fg-subtle">
              判定材料 (シグナル) は KEV・0day・日本関連・既知 APT 等の固定検出。
            </div>
          </section>

          <MobileArrow label="チャンネルへ投稿" />

          {/* ---- 列 4: チャンネル ---- */}
          <section className="lg:flex lg:flex-col lg:justify-center">
            <div className="hidden lg:block">
              <StageHeader title="チャンネル" detail="配信する/しないの切替" />
            </div>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-1 lg:gap-4">
              {data.channels.map((ch) => (
                <div
                  key={ch.id}
                  ref={(el) => {
                    channelRefs.current.set(ch.id, el);
                  }}
                  className={`rounded-lg border border-border-subtle bg-surface-2 px-2 py-2 ${ch.enabled ? "" : "opacity-50"}`}
                  style={{ borderLeftColor: channelColor(ch.id), borderLeftWidth: 3 }}
                >
                  <div className="flex items-start gap-1">
                    <a
                      href={`/app/news?channel=${ch.id}&since=${days * 24}`}
                      className="block min-w-0 flex-1 truncate text-sm font-semibold no-underline hover:opacity-80"
                      style={{ color: channelColor(ch.id) }}
                      title={`${ch.label || ch.id} の記事一覧を見る`}
                    >
                      {ch.label || ch.id}
                    </a>
                    {!readOnly && (
                      <button
                        onClick={() => openChannelDrawer(ch.id)}
                        className="shrink-0 text-fg-subtle hover:text-accent"
                        title="チャンネルを編集"
                      >
                        <Pencil className="h-3 w-3" />
                      </button>
                    )}
                  </div>
                  <div className="flex items-center gap-1.5">
                    <div className="tnum text-xs text-fg-subtle">{ch.posted_count} 件</div>
                    {chHealth?.checks?.[ch.id] && (
                      <WebhookStatusDot
                        status={chHealth.checks[ch.id].status}
                        detail={chHealth.checks[ch.id].detail}
                      />
                    )}
                  </div>
                  {channelReg && channelReg.webhook_set[ch.id] === false && (
                    <div className="mt-0.5 inline-flex items-center gap-1 text-[10px] text-warning">
                      <AlertTriangle className="h-3 w-3" /> 投稿先URL未設定
                    </div>
                  )}
                  {channelReg?.channels.find((c) => c.id === ch.id)?.push === false && (
                    <div
                      className="mt-0.5 inline-flex items-center gap-1 text-[10px] text-fg-subtle"
                      title="このチャンネルは Discord に配信せず Web に保存のみ"
                    >
                      <BellOff className="h-3 w-3" /> 保存のみ
                    </div>
                  )}
                  <label className="mt-1 flex items-center gap-1 text-[11px] text-fg-subtle">
                    <input
                      type="checkbox"
                      checked={ch.enabled}
                      disabled={readOnly || toggleChannelMut.isPending}
                      onChange={(e) =>
                        toggleChannelMut.mutate({ id: ch.id, enabled: e.target.checked })
                      }
                    />
                    有効
                  </label>
                </div>
              ))}
            </div>
            {!readOnly && (
              <button
                onClick={openNewChannelDrawer}
                className="mt-2 inline-flex items-center gap-1 text-[11px] text-accent hover:underline"
              >
                <Plus className="h-3 w-3" /> チャンネルを追加
              </button>
            )}
          </section>
        </div>
      </div>

      {/* 凡例 */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-fg-subtle">
        <span>
          <svg width="26" height="8" className="mr-1 inline-block align-middle">
            <line x1="0" y1="4" x2="26" y2="4" stroke="#6b88ff" strokeWidth="3" opacity="0.5" />
          </svg>
          実線 = 実際に流れた記事数 (太さ比例)
        </span>
        <span>
          <svg width="26" height="8" className="mr-1 inline-block align-middle">
            <line x1="0" y1="4" x2="26" y2="4" stroke="#6b88ff" strokeWidth="1.5" strokeDasharray="4 4" opacity="0.7" />
          </svg>
          点線 = PIR が重要度の判定基準を提供（最終判定はAIが実施）
        </span>
        <span>期間: 直近 {data.period_days} 日 / 対象記事 {data.total_articles} 件</span>
      </div>

      {/* プロダクト (ダイジェスト) の配信先 — 記事はルール、ダイジェストはここで制御 */}
      <ProductRoutingCard readOnly={readOnly} />

      {/* brief チャンネルの配信調整 (旧: 設定 → ブリーフ設定。配信領域へ移設) */}
      <BriefDeliveryPanel readOnly={readOnly} />

      {/* マッチリスト = 配信ルールの「キーワードリスト」条件で使う語彙 (2026-08-02 設定から移設)。
          使う場所と設定する場所を一致させる — 設定ページは対象画面を持たないものだけを残す。 */}
      {!readOnly && <MatchListsView />}

      {/* ルール編集 Drawer */}
      <Drawer
        isOpen={ruleDraft !== null}
        onClose={() => setRuleDraft(null)}
        title={ruleDraft?.index === null ? "ルールを追加" : "ルールを編集"}
        widthClass="md:w-[40rem]"
      >
        {ruleDraft && vocab && (
          <div className="space-y-4">
            <RuleEditorFields
              rule={ruleDraft.rule}
              vocab={vocab}
              readOnly={readOnly}
              onChange={(patch) =>
                setRuleDraft((d) => (d ? { ...d, rule: { ...d.rule, ...patch } } : d))
              }
            />
            {/* 保存前プレビュー (代表シナリオで 現行 vs 提案 の差分) */}
            <div className="rounded-lg border border-border-subtle bg-surface-1 p-2">
              <div className="flex items-center gap-2">
                <button
                  onClick={() => {
                    const cand = buildCandidateRules("save");
                    if (cand) previewRuleMut.mutate(cand);
                  }}
                  disabled={previewRuleMut.isPending}
                  className="rounded-md border border-accent bg-accent-soft px-2.5 py-1 text-xs font-medium text-accent-hover hover:bg-accent/20 disabled:opacity-50"
                >
                  {previewRuleMut.isPending ? "判定中…" : "プレビュー (代表シナリオで差分確認)"}
                </button>
                {rulePreview && (
                  <span className="text-[11px] text-fg-muted">
                    現行から <strong className="text-fg">{rulePreview.changed_count}</strong> /{" "}
                    {rulePreview.total} 件が変化
                  </span>
                )}
              </div>
              {rulePreview && (
                <div className="mt-2 overflow-x-auto">
                  {rulePreview.errors.length > 0 && (
                    <div className="mb-1.5 rounded border border-critical bg-critical-soft p-1.5 text-[11px] text-critical">
                      {rulePreview.errors.join("; ")}
                    </div>
                  )}
                  <table className="w-full text-[11px]">
                    <thead>
                      <tr className="border-b border-border-subtle text-fg-subtle">
                        <th className="py-1 pr-2 text-left font-medium">シナリオ</th>
                        <th className="px-2 py-1 text-left font-medium">現行</th>
                        <th className="px-2 py-1 text-left font-medium">提案</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rulePreview.scenarios.map((s) => (
                        <tr
                          key={s.scenario}
                          className={`border-b border-border-subtle/40 ${s.changed ? "bg-warning-soft/40" : ""}`}
                        >
                          <td className="py-1 pr-2 text-fg">{s.scenario}</td>
                          <td className="px-2 py-1">
                            <ChannelChip meta={channelMeta(s.current_channel)} />
                          </td>
                          <td className="px-2 py-1">
                            <ChannelChip meta={channelMeta(s.proposed_channel)} />
                            {s.changed && <span className="ml-1 text-warning">⟵ 変化</span>}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>

            <p className="m-0 text-[11px] text-fg-subtle">
              保存すると配信ルール全体が版として記録されます (順序の未保存変更も併せて確定)。
            </p>
            {!readOnly && (
              <div className="flex items-center gap-2">
                <button
                  onClick={() => persistRule("save")}
                  disabled={saveRulesMut.isPending}
                  className="rounded-md bg-accent px-4 py-1.5 text-sm font-semibold text-white hover:bg-accent-hover disabled:opacity-50"
                >
                  {saveRulesMut.isPending ? "保存中…" : "保存"}
                </button>
                {ruleDraft.index !== null && (
                  <button
                    onClick={() => {
                      if (confirm("このルールを削除します。よろしいですか?")) persistRule("delete");
                    }}
                    disabled={saveRulesMut.isPending}
                    className="inline-flex items-center gap-1 rounded-md border border-critical/40 px-3 py-1.5 text-sm text-critical hover:bg-critical-soft disabled:opacity-50"
                  >
                    <Trash2 className="h-3.5 w-3.5" /> 削除
                  </button>
                )}
                <button onClick={() => setRuleDraft(null)} className="text-sm text-fg-subtle hover:text-fg">
                  キャンセル
                </button>
              </div>
            )}
          </div>
        )}
      </Drawer>

      {/* チャンネル編集 Drawer */}
      <Drawer
        isOpen={chDraft !== null}
        onClose={() => setChDraft(null)}
        title={chDraft?.index === null ? "チャンネルを追加" : "チャンネルを編集"}
        widthClass="md:w-[36rem]"
      >
        {chDraft && (
          <div className="space-y-4">
            {chDraft.index === null && (
              <label className="block text-xs text-fg-subtle">
                ID (英小文字 / 数字 / _)
                <input
                  value={chDraft.channel.id}
                  onChange={(e) => {
                    const id = e.target.value.replace(/[^a-z0-9_]/g, "");
                    setChDraft((d) =>
                      d
                        ? {
                            ...d,
                            channel: {
                              ...d.channel,
                              id,
                              webhook_env_key: id ? `DISCORD_WEBHOOK_${id.toUpperCase()}` : "",
                            },
                          }
                        : d,
                    );
                  }}
                  placeholder="vendor_intel"
                  className="mt-0.5 block w-full rounded border border-border-subtle bg-surface-1 px-2 py-1 font-mono text-sm text-fg"
                />
              </label>
            )}
            <ChannelEditForm
              channel={chDraft.channel}
              allIds={(channelReg?.channels ?? []).map((c) => c.id)}
              isBuiltin={(channelReg?.builtin_ids ?? []).includes(chDraft.channel.id)}
              referenced={channelReg?.rule_refs[chDraft.channel.id] ?? false}
              readOnly={readOnly}
              onChange={(patch) =>
                setChDraft((d) => (d ? { ...d, channel: { ...d.channel, ...patch } } : d))
              }
            />
            {chDraft.index !== null ? (
              <ChannelWebhookField
                channelId={chDraft.channel.id}
                masked={channelReg?.webhook_masked?.[chDraft.channel.id] ?? ""}
                health={chHealth?.checks?.[chDraft.channel.id]}
                readOnly={readOnly}
              />
            ) : (
              <p className="m-0 text-[11px] text-fg-subtle">
                投稿先 URL (Discord webhook) はチャンネルを保存した後、この画面で設定できます。
              </p>
            )}
            {!readOnly && (
              <div className="flex items-center gap-2">
                <button
                  onClick={() => persistChannel("save")}
                  disabled={saveChannelsMut.isPending || (chDraft.index === null && !chDraft.channel.id)}
                  className="rounded-md bg-accent px-4 py-1.5 text-sm font-semibold text-white hover:bg-accent-hover disabled:opacity-50"
                >
                  {saveChannelsMut.isPending ? "保存中…" : "保存"}
                </button>
                {chDraft.index !== null &&
                  !(channelReg?.builtin_ids ?? []).includes(chDraft.channel.id) && (
                    <button
                      onClick={() => {
                        if (confirm("このチャンネルを削除します。よろしいですか?")) persistChannel("delete");
                      }}
                      disabled={saveChannelsMut.isPending}
                      className="inline-flex items-center gap-1 rounded-md border border-critical/40 px-3 py-1.5 text-sm text-critical hover:bg-critical-soft disabled:opacity-50"
                    >
                      <Trash2 className="h-3.5 w-3.5" /> 削除
                    </button>
                  )}
                <button onClick={() => setChDraft(null)} className="text-sm text-fg-subtle hover:text-fg">
                  キャンセル
                </button>
              </div>
            )}
          </div>
        )}
      </Drawer>
    </div>
  );
}

// ---------- 部品 ----------

function StageHeader({ title, detail, href }: { title: string; detail: string; href?: string }) {
  return (
    <div className="mb-2">
      <div className="flex items-baseline gap-2">
        <h3 className="m-0 text-sm font-bold text-fg">{title}</h3>
        {href && (
          <a href={href} className="text-[11px] text-accent no-underline hover:underline">
            詳細 →
          </a>
        )}
      </div>
      <div className="text-[11px] text-fg-subtle">{detail}</div>
    </div>
  );
}

// lg 未満 (縦積み) でのみ表示するステージ間の接続見出し
function MobileArrow({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-2 text-[11px] text-fg-subtle lg:hidden">
      <span className="text-fg-muted">↓</span>
      {label}
    </div>
  );
}

// PIR は「設定」ではなく「インテリジェンス (関心の定義)」。フロー上では上流コンテキストの
// 読み取りに徹し、編集 (description→compile / 重要度 / 有効) は PIR ページで行う。
function PirCard({
  pir,
  selected,
  refFn,
  onSelect,
}: {
  pir: FlowPir;
  selected: boolean;
  refFn: (el: HTMLDivElement | null) => void;
  onSelect: () => void;
}) {
  return (
    <div
      ref={refFn}
      onClick={onSelect}
      className={`cursor-pointer rounded-lg border bg-surface-2 px-2.5 py-1.5 ${
        selected
          ? "border-accent ring-1 ring-accent-ring"
          : "border-border-subtle hover:border-border-emphasized"
      } ${pir.enabled ? "" : "opacity-50"}`}
    >
      <div className="flex items-center gap-2">
        <a
          href={`/app/pir/${encodeURIComponent(pir.id)}`}
          onClick={(ev) => ev.stopPropagation()}
          className="min-w-0 flex-1 truncate text-xs font-medium text-fg no-underline hover:text-accent"
          title={`${pir.title} (${pir.id}) — クリックで PIR を編集`}
        >
          {pir.title}
        </a>
        <span className="tnum shrink-0 text-[11px] text-fg-subtle" title="期間内マッチ記事数">
          {pir.matched_total}
        </span>
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-fg-subtle">
        {pir.target_importance !== "auto" && (
          <span title="この PIR が選別 (重要度判定) の基準として与える水準">
            評価: {vocabLabel("importance", pir.target_importance)}
          </span>
        )}
        {!pir.enabled && <span className="text-warning">無効</span>}
        {pir.matched_total > 0 && (
          <span className="ml-auto flex gap-1">
            {IMP_ORDER.filter((k) => (pir.by_importance[k] ?? 0) > 0).map((k) => (
              <span key={k} style={{ color: IMP_COLOR[k] }} className="tnum">
                {vocabLabel("importance", k)}
                {pir.by_importance[k]}
              </span>
            ))}
          </span>
        )}
      </div>
    </div>
  );
}
