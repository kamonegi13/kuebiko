import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { api } from "../../api/client";
import { pagesApi } from "../../api/pages";
import { useFilters } from "../../state/filters";
import { useRuntimeFlags } from "../../hooks/useRuntimeFlags";
import type { ActorBrief, ActorThreat } from "../../api/types";
import { Drawer } from "../Drawer";
import { ActorDetail as DictActorCard } from "../../pages/actors/ActorDetail";
import { EditorialView } from "./operations/EditorialView";
import { intentLabel, intentMeta, isHypothesisIntent } from "../../utils/diamond";
import { DiamondDiagram } from "../DiamondDiagram";
import { sectorLabel } from "../geo/sectorColors";
import { actorHref } from "../../utils/intelNav";
import { useChannelMeta } from "../channel";
import { vocabLabel } from "../../hooks/useVocab";
import { Sparkline } from "../charts";

// 辞書の参照カードをその場で開くローダ (脅威アクターページの context を保つ)。
// getActors は react-query で辞書ページと共有キャッシュ。actor 未収載なら案内を出す。
function DictionaryCardLoader({ actorId, onClose }: { actorId: string; onClose: () => void }) {
  const qc = useQueryClient();
  const { read_only } = useRuntimeFlags();
  const { data, isLoading } = useQuery({ queryKey: ["actors"], queryFn: () => pagesApi.getActors() });
  if (isLoading) return <div className="text-fg-muted text-sm">読み込み中…</div>;
  const record = data?.actors.find((a) => a.id === actorId);
  if (!record) {
    return (
      <div className="text-fg-muted text-sm space-y-2">
        <p className="m-0">このアクターは辞書に未収載です。</p>
        <a href="/app/actors" className="text-accent-hover">アクター辞書を開く ↗</a>
      </div>
    );
  }
  return (
    <DictActorCard
      actor={record}
      families={data?.families ?? []}
      readOnly={read_only}
      onSaved={() => {
        qc.invalidateQueries({ queryKey: ["actors"] });
        onClose();
      }}
    />
  );
}

// ティア順ソート用ランク (未評価/評価なしは最下位)
const TIER_RANK: Record<string, number> = { critical: 4, high: 3, moderate: 2, watch: 1 };

function tierRank(t: ActorThreat | null | undefined): number {
  return t?.tier ? TIER_RANK[t.tier] ?? 0 : 0;
}

export function ThreatsTab({ actorLookup }: { actorLookup: Record<string, string> }) {
  const f = useFilters();
  // list 並び: activity=観測件数順 (従来) / threat=ミッション脅威度順
  const [sortBy, setSortBy] = useState<"activity" | "threat">("threat");

  const { data: threatsData } = useQuery({
    queryKey: ["threats", f.time, f.nation, f.family, f.japan_only, f.high_only, f.search],
    queryFn: () => api.threats({
      time: f.time, nation: f.nation, family: f.family,
      japan_only: f.japan_only, high_only: f.high_only, search: f.search,
    }),
  });

  // rail 再設計 (2026-07-25): 名前/別名/系統/国のインクリメンタル絞込 + 変化のみ表示チップ。
  // snapshot は一括取得済のためすべてクライアント側 (backend 追加呼出なし)。
  const [railQuery, setRailQuery] = useState("");
  const [changedOnly, setChangedOnly] = useState(false);
  // ティア節の開閉 (既定: Critical/High 展開、Moderate/Watch/評価対象外 折畳)。
  const [openTiers, setOpenTiers] = useState<Record<string, boolean>>({});

  const sortedActors = useMemo(() => {
    const actors = threatsData?.actors ?? [];
    if (sortBy === "activity") {
      // server 順 (観測件数 desc) — ただし休眠 (0件) を含むのでそのまま
      return actors;
    }
    return [...actors].sort(
      (a, b) => tierRank(b.threat) - tierRank(a.threat) || b.total_articles - a.total_articles,
    );
  }, [threatsData, sortBy]);

  const railActors = useMemo(() => {
    const q = railQuery.trim().toLowerCase();
    let list = sortedActors;
    if (changedOnly) list = list.filter((a) => a.is_new || a.is_spike || a.is_quiet_waking);
    if (q) {
      list = list.filter(
        (a) =>
          a.canonical.toLowerCase().includes(q) ||
          a.family.toLowerCase().includes(q) ||
          a.nation.toLowerCase().includes(q) ||
          a.aliases.some((al) => al.toLowerCase().includes(q)),
      );
    }
    return list;
  }, [sortedActors, railQuery, changedOnly]);

  // ティア節グルーピング (脅威度順のみ)。検索/チップ有効時・活動順はフラット表示
  // (折畳の中に結果を隠さない)。休眠もティア内に残す (暗域=不明≠安全 — ティア非影響)。
  const isFlatRail = sortBy === "activity" || railQuery.trim() !== "" || changedOnly;
  const tierSections = useMemo(() => {
    if (isFlatRail) return [];
    const order: { key: string; label: string; defaultOpen: boolean }[] = [
      { key: "critical", label: vocabLabel("threat_tier", "critical"), defaultOpen: true },
      { key: "high", label: vocabLabel("threat_tier", "high"), defaultOpen: true },
      { key: "moderate", label: vocabLabel("threat_tier", "moderate"), defaultOpen: false },
      { key: "watch", label: vocabLabel("threat_tier", "watch"), defaultOpen: false },
      { key: "none", label: "評価対象外", defaultOpen: false },
    ];
    return order
      .map((o) => ({
        ...o,
        actors: railActors.filter((a) => (a.threat?.tier ?? "none") === o.key),
      }))
      .filter((s) => s.actors.length > 0);
  }, [railActors, isFlatRail]);

  const { data: detail } = useQuery({
    queryKey: ["actorDetail", f.actor, f.time],
    queryFn: () => api.actorDetail(f.actor, f.time),
    enabled: !!f.actor,
  });

  // Mobile (< md): actor 未選択時は list 表示、選択時は detail に切替
  const mobileShowList = !f.actor;

  // P3 (2026-07-25): 関係が空 (未選択 / 系統・共起なし) のときは右列ごと畳んで
  // 中央詳細に幅を返す (「系統メンバーなし」だけの 260px 列を常設しない)。
  const hasRelations = !!(
    detail?.found &&
    detail.relations &&
    ((detail.relations.family_members?.length ?? 0) > 0 ||
      (detail.relations.cooccur_actors?.length ?? 0) > 0)
  );

  return (
    <div className={`grid grid-cols-1 md:grid-cols-[300px_1fr] ${hasRelations ? "xl:grid-cols-[300px_1fr_260px]" : ""} gap-3.5 min-h-[640px]`}>

      {/* LEFT: actor list — mobile では actor 選択時に非表示。
          md+ では sticky 化 (top-24 = AppHeader+toolbar の下) し、中央詳細が長くても追従させる。 */}
      <div className={`${mobileShowList ? "block" : "hidden md:flex"} bg-surface-1 border border-border-subtle rounded-lg overflow-hidden flex-col max-h-[calc(100vh-7rem)] md:self-start md:sticky md:top-24 flex`}>
        <div className="px-3.5 py-3 border-b border-border-subtle flex items-center justify-between gap-2">
          <span className="text-xs uppercase tracking-wider text-fg-muted font-semibold">actors</span>
          <span className="inline-flex items-center gap-2">
            <span className="inline-flex rounded border border-border-subtle overflow-hidden text-[10px]">
              <button
                onClick={() => setSortBy("threat")}
                title="ミッション脅威度 (関連度×能力) の降順 — 休眠アクターも評価順に並ぶ"
                className={`px-1.5 py-0.5 transition-colors ${sortBy === "threat" ? "bg-accent-soft text-accent-hover" : "text-fg-subtle hover:text-fg"}`}
              >
                脅威度順
              </button>
              <button
                onClick={() => setSortBy("activity")}
                title="期間内の観測 (報道) 件数の降順"
                className={`px-1.5 py-0.5 transition-colors ${sortBy === "activity" ? "bg-accent-soft text-accent-hover" : "text-fg-subtle hover:text-fg"}`}
              >
                活動順
              </button>
            </span>
            <span className="bg-surface-3 text-fg text-xs px-2 py-0.5 rounded-full tnum">{threatsData?.actors.length || 0}</span>
          </span>
        </div>
        {/* rail 内絞込 (2026-07-25 再設計): 検索 + triage チップ。151 件の縦読みを不要にする */}
        <div className="px-2.5 py-2 border-b border-border-subtle space-y-1.5">
          <input
            type="search"
            value={railQuery}
            onChange={(e) => setRailQuery(e.target.value)}
            placeholder="名前・別名・系統・国で絞込…"
            className="w-full bg-surface-2 border border-border-subtle rounded px-2 py-1 text-xs text-fg focus:outline-none focus:border-accent"
          />
          <label className="flex items-center gap-1.5 text-[11px] text-fg-muted cursor-pointer select-none">
            <input
              type="checkbox"
              checked={changedOnly}
              onChange={(e) => setChangedOnly(e.target.checked)}
            />
            急増・新規・復帰のみ
          </label>
        </div>
        <div className="overflow-y-auto flex-1">
          {!threatsData && [1, 2, 3, 4, 5].map((i) => (
            <div key={i} className="h-9 mx-2 my-1 rounded bg-gradient-to-r from-surface-1 via-surface-3 to-surface-1 bg-[length:200%_100%] animate-shimmer" />
          ))}
          {threatsData && railActors.length === 0 && (
            <div className="p-10 text-center text-fg-subtle text-sm">
              条件に一致する actor がいません<br />
              <span className="text-[11px]">絞り込み条件を変更してください</span>
            </div>
          )}
          {isFlatRail
            ? railActors.map((a) => (
                <ActorRowCompact key={a.actor_id} actor={a} selected={f.actor === a.actor_id} onClick={() => f.selectActor(a.actor_id)} />
              ))
            : tierSections.map((s) => {
                const open = openTiers[s.key] ?? s.defaultOpen;
                return (
                  <details key={s.key} open={open}>
                    <summary
                      onClick={(e) => {
                        e.preventDefault();
                        setOpenTiers((m) => ({ ...m, [s.key]: !open }));
                      }}
                      className="sticky top-0 z-10 cursor-pointer list-none px-3 py-1.5 bg-surface-2 border-b border-border-subtle text-[10.5px] uppercase tracking-wider text-fg-muted font-semibold flex items-center justify-between select-none [&::-webkit-details-marker]:hidden"
                    >
                      <span>{open ? "▾" : "▸"} {s.label}</span>
                      <span className="tnum">{s.actors.length}</span>
                    </summary>
                    {s.actors.map((a) => (
                      <ActorRowCompact key={a.actor_id} actor={a} selected={f.actor === a.actor_id} onClick={() => f.selectActor(a.actor_id)} />
                    ))}
                  </details>
                );
              })}
        </div>
      </div>

      {/* CENTER: actor detail — mobile では actor 選択時のみ */}
      <div className={`${mobileShowList ? "hidden md:block" : "block"} bg-surface-1 border border-border-subtle rounded-lg overflow-hidden`}>
        {/* Mobile back button: actor 選択中で md 未満 のとき */}
        {f.actor && (
          <button
            onClick={() => f.selectActor("")}
            className="md:hidden flex items-center gap-2 px-4 py-2.5 border-b border-border-subtle text-accent text-sm w-full hover:bg-surface-2"
          >
            ← アクター一覧に戻る
          </button>
        )}
        {!detail || !detail.found ? (
          <div className="py-20 px-10 text-center text-fg-muted">
            <h3 className="m-0 mb-2 text-fg text-lg font-semibold">Actor を選択してください</h3>
            <p className="text-fg-subtle text-sm">左の一覧からアクターを選ぶか、下の探索パネルから急増・新規のアクターを選択</p>
          </div>
        ) : detail.activity ? (
          <>
            <ActorDetail detail={{
              activity: detail.activity,
              recent_articles: detail.recent_articles,
              timeline_daily: detail.timeline_daily,
              timeline_event: detail.timeline_event,
              timeline_coverage: detail.timeline_coverage,
              relations: detail.relations,
              known_ttps: detail.known_ttps,
              child_groups: detail.child_groups,
            }} />
            {/* < xl では右 rail が無いので relations を detail 末尾に折りたたみ表示 */}
            {detail.relations && (
              <details className="xl:hidden border-t border-border-subtle">
                <summary className="cursor-pointer px-6 py-3 text-[10.5px] uppercase tracking-wider text-fg-muted font-semibold select-none">
                  関係 (系統 / 共起) を表示
                </summary>
                <Relations
                  family={detail.activity.family}
                  familyMembers={detail.relations.family_members}
                  cooccurActors={detail.relations.cooccur_actors}
                  actorLookup={actorLookup}
                  onSelect={f.selectActor}
                />
              </details>
            )}
          </>
        ) : null}
      </div>

      {/* RIGHT: relations (xl かつ関係が非空のときのみ) — sticky で中央の長さに追従。 */}
      {hasRelations && detail?.relations && detail.activity && (
        <div className="hidden xl:block bg-surface-1 border border-border-subtle rounded-lg overflow-hidden max-h-[calc(100vh-7rem)] overflow-y-auto self-start sticky top-24">
          <Relations
            family={detail.activity.family}
            familyMembers={detail.relations.family_members}
            cooccurActors={detail.relations.cooccur_actors}
            actorLookup={actorLookup}
            onSelect={f.selectActor}
          />
        </div>
      )}
    </div>
  );
}

// ティアの表示スタイル (色は既存 severity palette に整合)。
// label は vocab "threat_tier" (vocabLabel、英語 Critical/High/Moderate/Watch) を SSoT に。
const TIER_STYLE: Record<string, string> = {
  critical: "bg-critical-soft text-critical",
  high: "bg-warning-soft text-warning",
  moderate: "bg-accent-soft text-accent-hover",
  watch: "bg-surface-3 text-fg-muted",
};

// ティアの色ドット (compact 行用)。ティア名の文脈は節見出し (グルーピング時) と
// tooltip が担う。休眠の高ティアも薄めず表示する (暗域=不明≠安全 — ティア非影響)。
const TIER_DOT: Record<string, string> = {
  critical: "bg-critical",
  high: "bg-warning",
  moderate: "bg-accent",
  watch: "bg-surface-3 border border-border-default",
  none: "bg-surface-3",
};

type RowBadge = { color: "success" | "critical" | "accent" | "warning"; label: string };

function rowBadges(a: ActorBrief): RowBadge[] {
  const out: RowBadge[] = [];
  if (a.is_new) out.push({ color: "success", label: "新規" });
  else if (a.is_spike) out.push({ color: "critical", label: "急増" });
  if (a.is_quiet_waking) out.push({ color: "accent", label: "復帰" });
  if (a.japan_targeted_count > 0) out.push({ color: "warning", label: "JP" });
  if ((a.matched_pir_ids?.length ?? 0) > 0) out.push({ color: "accent", label: "PIR" });
  return out;
}

// compact 1 行 (2026-07-25 rail 再設計): rail は「選ぶための一覧」— sparkline は
// 詳細の活動タイムラインと重複するため撤去し、151 件の縦密度を約 1/2 行高にする。
// 系統・別名・休眠は tooltip で補完 (別名検索は rail 検索が担う)。
function ActorRowCompact({ actor: a, selected, onClick }: { actor: ActorBrief; selected: boolean; onClick: () => void }) {
  const tier = a.threat?.tier ?? "none";
  const dormant = a.threat?.activity_state === "dormant";
  const badges = rowBadges(a);
  const shown = badges.slice(0, 2);
  const rest = badges.slice(2);
  const tooltip = [
    a.canonical,
    a.family ? `系統: ${a.family}` : "",
    a.aliases.length > 0 ? `別名: ${a.aliases.slice(0, 8).join(" / ")}` : "",
    a.threat?.tier ? `ティア: ${vocabLabel("threat_tier", a.threat.tier)}${dormant ? "・休眠 (ティア非影響)" : ""}` : "",
  ]
    .filter(Boolean)
    .join("\n");
  return (
    <div
      onClick={onClick}
      title={tooltip}
      className={`px-3 py-1.5 flex items-center gap-2 cursor-pointer border-b border-border-subtle transition-colors ${
        selected ? "bg-accent-subtle shadow-[inset_3px_0_0_#6b88ff]" : "hover:bg-surface-2"
      }`}
    >
      <span className={`h-2 w-2 rounded-full shrink-0 ${TIER_DOT[tier] ?? TIER_DOT.none}`} />
      <span className={`text-sm font-medium truncate min-w-0 flex-1 ${dormant ? "text-fg-muted" : "text-fg"}`}>
        {a.canonical}
      </span>
      {a.kind === "organization" && <Badge color="warning">機関</Badge>}
      {a.kind === "contractor" && <Badge color="warning">請負</Badge>}
      {dormant && <span className="text-[9px] text-fg-subtle shrink-0">休眠</span>}
      {a.nation && (
        <span className="text-[9.5px] font-semibold text-fg-muted bg-surface-3 px-1 rounded-sm shrink-0">{a.nation.toUpperCase()}</span>
      )}
      <span className={`tnum text-xs font-semibold w-6 text-right shrink-0 ${a.total_articles > 0 ? "text-fg" : "text-fg-subtle"}`}>
        {a.total_articles}
      </span>
      <span className="flex gap-1 shrink-0">
        {shown.map((b) => (
          <Badge key={b.label} color={b.color}>{b.label}</Badge>
        ))}
        {rest.length > 0 && (
          <span className="text-[9px] text-fg-subtle self-center" title={rest.map((b) => b.label).join(" / ")}>
            +{rest.length}
          </span>
        )}
      </span>
    </div>
  );
}

function Badge({ color, children }: { color: "success" | "critical" | "accent" | "warning"; children: React.ReactNode }) {
  const styles: Record<string, string> = {
    success: "bg-success-soft text-success",
    critical: "bg-critical-soft text-critical",
    accent: "bg-accent-soft text-accent-hover",
    warning: "bg-warning-soft text-warning",
  };
  return (
    <span className={`inline-flex items-center px-1.5 rounded-sm text-[9.5px] font-bold tracking-wide h-4 ${styles[color]}`}>{children}</span>
  );
}

// 恒久行動史ストリップ (アクター辞書 Phase1): 月次×永年 sparkline の埋込。観測ゼロなら非表示。
function MonthlyHistoryStrip({ actorId, onOpenDict }: { actorId: string; onOpenDict: () => void }) {
  const { data } = useQuery({
    queryKey: ["actorHistory", actorId],
    queryFn: () => pagesApi.actorHistory(actorId),
  });
  const spark = (data?.series ?? []).map((p) => p.subject_articles);
  const total = spark.reduce((sum, v) => sum + v, 0);
  if (total === 0) return null;
  return (
    <div className="flex items-center gap-2.5 px-6 py-2 border-b border-border-subtle text-xs">
      <span
        className="text-[10px] uppercase tracking-wider text-fg-muted font-semibold"
        title="当該アクターを主題として報じた記事のみを数える永久記録 (このページの期間内件数は言及ベース)"
      >
        主題記事の行動史 (月次)
      </span>
      <span className="text-accent">
        <Sparkline data={spark} width={110} height={18} highlightLast />
      </span>
      <span className="text-fg font-semibold tnum">{total} 件</span>
      <span className="text-[10px] text-fg-subtle">{data?.epoch_month ?? ""} 以降の主題記事</span>
      <button type="button" onClick={onOpenDict} className="ml-auto text-accent-hover whitespace-nowrap">
        辞書で恒久史を見る ↗
      </button>
    </div>
  );
}

function ActorDetail({ detail }: { detail: { activity: NonNullable<import("../../api/types").ActorDetail["activity"]>; recent_articles?: import("../../api/types").ActorArticle[]; timeline_daily?: [string, number][]; timeline_event?: [string, number][]; timeline_coverage?: { dated: number; total: number; event_in_window: number }; relations?: import("../../api/types").ActorRelations; known_ttps?: string[]; child_groups?: [string, string, number, string][] } }) {
  const a = detail.activity;
  const chMeta = useChannelMeta();
  const [reviewOpen, setReviewOpen] = useState(false);
  const [dictOpen, setDictOpen] = useState(false);
  // 時間軸トグル: 報道時刻 (collection) ↔ 発生時刻 (実際に活動した年代、dated のみ)。
  const [tlBasis, setTlBasis] = useState<"report" | "event">("report");
  const tlIsEvent = tlBasis === "event";
  const tlCov = detail.timeline_coverage;
  // 既知 TTP (Actor 辞書 knowledge) の T 番号集合。観測 TTP との gap 判定に使う。
  // 既知サブ technique (T1059.001) の親 (T1059) も既知扱い — 観測側の regex 抽出は
  // 親 ID で出ることが多く、そのままだと false になるため。
  const knownTtps = detail.known_ttps ?? [];
  const knownTtpIds = new Set(
    knownTtps.flatMap((t) => {
      const id = t.split(" ")[0].toUpperCase();
      return [id, id.split(".")[0]];
    }),
  );

  return (
    <>
      {/* Hero */}
      <div className="px-6 pt-5 pb-4 bg-gradient-to-b from-surface-2 to-surface-1 border-b border-border-subtle">
        <div className="flex items-start justify-between gap-3 mb-1.5 flex-wrap">
          <h1 className="m-0 text-2xl font-bold tracking-tight text-fg flex items-baseline gap-2.5 flex-wrap">
            <span>{a.canonical}</span>
            <span className="inline-flex gap-1.5 ml-1">
              {a.kind === "organization" && <HeroBadge variant="warning">国家機関</HeroBadge>}
              {a.kind === "contractor" && <HeroBadge variant="warning">請負業者</HeroBadge>}
              {a.nation && <HeroBadge>{a.nation.toUpperCase()}</HeroBadge>}
              {a.family && (
                <HeroBadge variant="accent">系統: {vocabLabel("actor_family", a.family)}</HeroBadge>
              )}
              {a.mitre_group && <HeroBadge variant="mitre">MITRE {a.mitre_group}</HeroBadge>}
            </span>
          </h1>
          <span className="shrink-0 inline-flex gap-1.5">
            <button
              onClick={() => setDictOpen(true)}
              title="アクター辞書の参照カード (帰属・背景・既知 TTP・出典) をその場で開く"
              className="bg-surface-2 border border-border-default text-fg-muted hover:text-fg hover:border-accent-soft text-xs px-2.5 py-1 rounded-md inline-flex items-center gap-1 transition-colors"
            >
              辞書カード
            </button>
            <button
              onClick={() => setReviewOpen(true)}
              title="この actor の関連記事の論調をその場で確認"
              className="bg-surface-2 border border-border-default text-fg-muted hover:text-fg hover:border-accent-soft text-xs px-2.5 py-1 rounded-md inline-flex items-center gap-1 transition-colors"
            >
              この actor の論調を確認
            </button>
          </span>
        </div>
        {a.aliases.length > 0 && (
          <div className="text-fg-subtle text-sm mt-1">{a.aliases.join(" · ")}</div>
        )}
        {a.description && (
          <div className="text-fg-muted text-sm leading-relaxed mt-2.5 max-w-3xl">{a.description}</div>
        )}
        {(a.kind === "organization" || a.kind === "contractor") && (
          <div className="mt-2.5 text-xs text-warning bg-warning-soft rounded px-2.5 py-1.5 max-w-3xl">
            これは{a.kind === "organization" ? "国家機関" : "請負業者"}です。件数は「活動」でなく
            <strong>グループ名なしで機関が単独言及された記事</strong>の数です
            (配下グループが特定できた記事はそのグループに計上)。
          </div>
        )}
      </div>

      {/* organization: 配下グループの活動 rollup */}
      {a.kind === "organization" && (detail.child_groups?.length ?? 0) > 0 && (
        <div className="px-6 py-3 border-b border-border-subtle">
          <div className="text-[10.5px] uppercase tracking-wider text-fg-muted font-semibold mb-2">
            配下グループの活動 (期間内)
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-1.5">
            {detail.child_groups!.map(([cid, canonical, count, spark]) => (
              <a
                key={cid}
                href={actorHref(cid)}
                className="flex items-center gap-2.5 text-sm text-fg-muted hover:text-accent-hover bg-surface-1 rounded px-2.5 py-1.5"
              >
                <span className="flex-1 truncate">{canonical}</span>
                <span className="sparkline-svg" dangerouslySetInnerHTML={{ __html: spark }} />
                <span className="tnum text-fg font-semibold">{count}</span>
              </a>
            ))}
          </div>
        </div>
      )}

      {/* Drawer: actor-scoped editorial review */}
      <Drawer
        isOpen={reviewOpen}
        onClose={() => setReviewOpen(false)}
        title={`論調レビュー — ${a.canonical}`}
      >
        <EditorialView actorFilter={a.canonical} />
      </Drawer>

      {/* Drawer: 辞書の参照カードをその場で表示 (脅威アクターから離れない) */}
      <Drawer
        isOpen={dictOpen}
        onClose={() => setDictOpen(false)}
        title={`${a.canonical}`}
      >
        {dictOpen && <DictionaryCardLoader actorId={a.actor_id} onClose={() => setDictOpen(false)} />}
      </Drawer>

      {/* Stats strip */}
      <div className="grid grid-cols-4 gap-px bg-border-subtle border-b border-border-subtle">
        <Stat label="期間内の記事数" value={String(a.total_articles)} suffix="件" />
        <Stat label="急増倍率" value={a.is_new ? "新規" : (a.spike_ratio?.toFixed(1) ?? "0.0")} suffix={a.is_new ? "" : "×"} trend={a.is_spike ? "up" : ""} trendText={a.is_new ? "初観測" : a.is_spike ? "↑ 平常より多い" : "平常どおり"} />
        <Stat label="日本標的" value={String(a.japan_targeted_count)} suffix="件" />
        <Stat label="最終観測" value={a.last_seen_iso.slice(0, 10) || "—"} small />
      </div>

      {/* 恒久行動史 (月次) — 直近 90 日 (このページ) と棲み分けた永年レンズの埋込カード。
          詳細 (月次内訳・関連情勢) は辞書カード = 恒久史のホームで見る (2026-07-26 確定)。 */}
      <MonthlyHistoryStrip actorId={a.actor_id} onOpenDict={() => setDictOpen(true)} />

      {/* ミッション脅威評価 (関連度×能力の透明ティア + 活動状態併記) */}
      {a.threat && <MissionThreatSection threat={a.threat} />}

      {/* Activity timeline: 報道時刻↔発生時刻トグル + カバレッジ正直化 */}
      <Section title="活動タイムライン">
        <div className="flex items-center justify-end mb-1">
          <div className="inline-flex rounded border border-border-subtle overflow-hidden text-[10.5px]">
            <button
              onClick={() => setTlBasis("report")}
              className={`px-2 py-0.5 transition-colors ${!tlIsEvent ? "bg-accent-soft text-accent-hover" : "text-fg-subtle hover:text-fg"}`}
              title="記事が報道/取得された時刻 (全件)"
            >
              報道時刻
            </button>
            <button
              onClick={() => setTlBasis("event")}
              className={`px-2 py-0.5 transition-colors ${tlIsEvent ? "bg-accent-soft text-accent-hover" : "text-fg-subtle hover:text-fg"}`}
              title="事象が実際に発生した時刻 = このアクターの活動年代 (発生日が判明した記事のみ・過去に偏る)"
            >
              発生時刻
            </button>
          </div>
        </div>
        <TimelineBars data={(tlIsEvent ? detail.timeline_event : detail.timeline_daily) || []} />
        {tlIsEvent && tlCov && (
          <div className="mt-1 text-[10px] leading-snug text-fg-subtle">
            発生日付き {tlCov.dated}/{tlCov.total} 件（期間内 {tlCov.event_in_window}）。未抽出（速報の
            大半）は非表示＝<span className="text-warning">不明（≠無し）</span>。期間を長く（1年など）とるほど活動年代が出る。
          </div>
        )}
      </Section>

      {/* Diamond Model: 4 頂点 + 2 軸 を菱形図で可視化 (md+)。mobile は下の各軸グループに fallback。 */}
      <Section title="Diamond Model — この actor を中心とした侵入分析">
        <div className="hidden md:block">
          <DiamondDiagram activity={a} />
          {/* 配色凡例 (ベンダー慣行準拠): Adversary 頂点の nation 色 */}
          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-fg-subtle">
            <span className="text-fg-muted">Adversary 帰属色:</span>
            <LegendDot hex="#e5484d" label="中国" />
            <LegendDot hex="#6b88ff" label="ロシア" />
            <LegendDot hex="#a371f7" label="北朝鮮" />
            <LegendDot hex="#45b878" label="イラン" />
            <span className="text-fg-faint">·</span>
            <span>頂点をクリック → 各詳細へ ／ 凡例の意図をクリック → ニュース連動</span>
          </div>
        </div>
        <p className="md:hidden m-0 text-fg-muted text-xs leading-relaxed">
          <b className="text-fg">4 頂点</b>: Adversary (= 本 actor) ／ Capability ／ Infrastructure ／
          Victim。<b className="text-fg ml-1">2 軸</b>: socio-political (意図) ／ technical。
          下の各セクションで詳細を表示。
        </p>
      </Section>

      {/* Capability 軸 */}
      <DiamondGroup
        anchorId="dm-capability"
        title="Capability — TTPs, malware, tools, CVEs"
        empty={a.top_cves.length === 0 && a.top_ttps.length === 0 && a.top_malware_families.length === 0 && a.top_tools.length === 0}
      >
        {a.top_malware_families.length > 0 && (
          <Subsection label={`マルウェア系統 · ${a.top_malware_families.length}`}>
            <Pills items={a.top_malware_families.map(([m, n]) => `${m} (${n})`)} />
          </Subsection>
        )}
        {a.top_tools.length > 0 && (
          <Subsection label={`ツール · ${a.top_tools.length}`}>
            <Pills items={a.top_tools.map(([t, n]) => `${t} (${n})`)} />
          </Subsection>
        )}
        {a.top_ttps.length > 0 && (
          <Subsection label={`観測 TTPs · ${a.top_ttps.length}`}>
            {/* 既知 (辞書の MITRE TTP) との突き合わせ: 既知外 = 行動変化の可能性をハイライト */}
            <div className="flex flex-wrap gap-1.5">
              {a.top_ttps.map((t, i) => {
                const id = t.split(" ")[0].toUpperCase();
                const parentId = id.split(".")[0];
                const isKnown =
                  knownTtpIds.size === 0 || knownTtpIds.has(id) || knownTtpIds.has(parentId);
                return (
                  <span
                    key={i}
                    title={isKnown ? "MITRE 既知の TTP" : "MITRE 既知 TTP に含まれない観測 — 行動変化 / 誤抽出の可能性"}
                    className={`text-[11px] px-1.5 py-0.5 rounded border font-mono ${
                      isKnown
                        ? "bg-surface-2 border-border-subtle text-fg"
                        : "bg-warning-soft border-warning text-warning font-semibold"
                    }`}
                  >
                    {isKnown ? t : t}
                  </span>
                );
              })}
            </div>
            {knownTtps.length > 0 && (
              <p className="m-0 mt-1.5 text-[11px] text-fg-subtle">
                既知 TTP (MITRE / 辞書): {knownTtps.length} 件 —{" "}
                <button onClick={() => setDictOpen(true)} className="text-accent-hover underline">
                  辞書カードで一覧
                </button>
                {" "}／ 既知リスト外の観測 (行動変化シグナル)
              </p>
            )}
          </Subsection>
        )}
        {a.top_cves.length > 0 && (
          <Subsection label={`CVEs · ${a.top_cves.length}`}>
            <Pills items={a.top_cves} />
          </Subsection>
        )}
      </DiamondGroup>

      {/* Infrastructure 軸 */}
      <DiamondGroup
        anchorId="dm-infrastructure"
        title="Infrastructure — IoCs (IP, domain, hash, URL)"
        empty={a.top_iocs_ip.length === 0 && a.top_iocs_domain.length === 0 && a.top_iocs_hash.length === 0 && a.top_iocs_url.length === 0}
        emptyHint="まだ IOC が蓄積されていません (翌朝以降の記事取り込みで順次たまります)"
      >
        {a.top_iocs_ip.length > 0 && (
          <Subsection label={`IP · ${a.top_iocs_ip.length}`}>
            <Pills items={a.top_iocs_ip.map(([v, n]) => `${v} (${n})`)} mono />
          </Subsection>
        )}
        {a.top_iocs_domain.length > 0 && (
          <Subsection label={`ドメイン · ${a.top_iocs_domain.length}`}>
            <Pills items={a.top_iocs_domain.map(([v, n]) => `${v} (${n})`)} mono />
          </Subsection>
        )}
        {a.top_iocs_hash.length > 0 && (
          <Subsection label={`ハッシュ · ${a.top_iocs_hash.length}`}>
            <Pills items={a.top_iocs_hash.map(([v, n]) => `${v.slice(0, 12)}... (${n})`)} mono />
          </Subsection>
        )}
        {a.top_iocs_url.length > 0 && (
          <Subsection label={`URL · ${a.top_iocs_url.length}`}>
            <Pills items={a.top_iocs_url.map(([v, n]) => `${v.slice(0, 50)}... (${n})`)} mono />
          </Subsection>
        )}
      </DiamondGroup>

      {/* Victim 軸 */}
      <DiamondGroup
        anchorId="dm-victim"
        title="Victim — 分野・対象国・日本標的"
        empty={a.top_sectors.length === 0 && a.top_countries.length === 0 && a.japan_targeted_count === 0}
      >
        {a.top_sectors.length > 0 && (
          <Subsection label="分野">
            <Pills items={a.top_sectors.map(([s, n]) => `${sectorLabel(s)} (${n})`)} />
          </Subsection>
        )}
        {a.top_countries.length > 0 && (
          <Subsection label="対象国">
            <Pills items={a.top_countries.map(([c, n]) => `${c} (${n})`)} />
          </Subsection>
        )}
        {a.japan_targeted_count > 0 && (
          <Subsection label="日本標的">
            <span className="inline-flex items-center gap-1.5 bg-warning-soft text-warning px-2.5 py-1 rounded-md text-xs font-semibold">
              {a.japan_targeted_count} 件
            </span>
          </Subsection>
        )}
      </DiamondGroup>

      {/* Socio-political 軸 (Adversary → Victim の意図)。Diamond 2 meta-feature 軸の 1 つ。
          intent chip → News を「その intent × この actor」で絞り込み (cross-navigation)。 */}
      <DiamondGroup
        anchorId="dm-socio"
        title="Socio-political 軸 — Adversary → Victim の意図"
        empty={a.top_intents.length === 0}
        emptyHint="まだ意図が判定された記事がありません (要約処理の更新後にたまります)"
      >
        <Subsection label={`意図の分布 · ${a.top_intents.length}`}>
          <div className="flex flex-wrap gap-1.5">
            {a.top_intents.map(([it, n]) => (
              <a
                key={it}
                href={`/app/news?intent=${encodeURIComponent(it)}&search=${encodeURIComponent(a.canonical)}`}
                title={`ニュースを「${intentLabel(it)} × ${a.canonical}」で絞り込み`}
                className={`inline-flex items-center gap-1 bg-surface-2 border border-accent/30 px-2.5 py-0.5 rounded-full text-xs hover:bg-accent/15 hover:border-accent-soft transition-colors ${intentMeta(it)?.tone ?? "text-fg"}`}
              >
                {intentLabel(it)} <span className="text-fg-subtle tnum">({n})</span>
              </a>
            ))}
          </div>
        </Subsection>
      </DiamondGroup>

      <Section title={`最近の記事 · ${detail.recent_articles?.length ?? 0}`}>
        <ul className="list-none m-0 p-0 text-sm">
          {detail.recent_articles?.map((art) => (
            <li key={art.article_id} className="py-2 border-b border-border-subtle last:border-b-0 flex items-start gap-2.5">
              <span className={`w-1.5 h-1.5 rounded-full mt-1.5 shrink-0 ${
                art.importance === "high" ? "bg-critical shadow-[0_0_0_3px_rgba(255,107,107,0.18)]" :
                art.importance === "medium" ? "bg-warning" : "bg-fg-subtle"
              }`} />
              <div className="flex-1 min-w-0">
                <a href={art.url} target="_blank" rel="noopener noreferrer" className="text-accent hover:underline text-sm font-medium block truncate">{art.title}</a>
                <div className="text-fg-subtle text-[11px] mt-0.5 flex gap-2 flex-wrap items-center">
                  <span>{art.feed_title.slice(0, 20)}</span>
                  <span>·</span>
                  <span>{art.created_at}</span>
                  {art.posted_channel && <span className="bg-surface-3 text-fg-muted px-1.5 rounded-sm text-[10px] font-mono">{chMeta(art.posted_channel).label}</span>}
                  {art.socio_political_intent && intentMeta(art.socio_political_intent) && (
                    <span className={`px-1.5 rounded-sm text-[10px] bg-surface-2 ${intentMeta(art.socio_political_intent)?.tone}`}
                      title="socio-political 軸 (この事案の意図)">
                      {intentLabel(art.socio_political_intent)}
                      {isHypothesisIntent(art.intent_confidence) && <span className="opacity-70"> (仮説)</span>}
                    </span>
                  )}
                </div>
                {art.technical_axis_summary && (
                  <p className="text-fg-subtle text-[11px] mt-1 flex items-start gap-1 leading-relaxed"
                    title="technical 軸 (Capability⇄Infrastructure の技術的なつながり)">
                    <span className="line-clamp-2">{art.technical_axis_summary}</span>
                  </p>
                )}
              </div>
            </li>
          ))}
        </ul>
      </Section>
    </>
  );
}

// ミッション脅威評価セクション: ティアの導出理由 (規則 + 駆動要因) を常時開示する。
// 評価は server 側 90d 固定窓の計算値 — 表示窓 (time filter) と独立。
function MissionThreatSection({ threat }: { threat: ActorThreat }) {
  const cls = threat.tier ? TIER_STYLE[threat.tier] : undefined;
  return (
    <Section title="ミッション脅威評価 — 関連度 × 能力 (90日固定期間)">
      <div className="flex items-center gap-2 flex-wrap mb-2.5">
        {cls ? (
          <span className={`inline-flex items-center px-2.5 py-0.5 rounded-md text-sm font-bold ${cls}`}>
            {vocabLabel("threat_tier", threat.tier)}
          </span>
        ) : (
          <span className="inline-flex items-center px-2.5 py-0.5 rounded-md text-sm font-semibold border border-border-subtle text-fg-subtle">
            未評価
          </span>
        )}
        <span className="text-xs text-fg-muted bg-surface-2 px-2 py-0.5 rounded">
          {vocabLabel("activity_state", threat.activity_state)}
        </span>
        <span className="text-[11px] text-fg-subtle">{threat.tier_rule}</span>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        <FactorGroup
          label={`① 関連度 R${threat.relevance_band}`}
          hint="対日ミッション関連 (公的な指針＋被害観測のみが証拠。自前の配信判断は使わない)"
          factors={threat.relevance_factors}
        />
        <FactorGroup
          label={`② 能力 ${vocabLabel("capability_band", threat.capability_band)}`}
          hint="技術シグナル (TTP幅 / 装備 / KEV実悪用 / ICS)。証拠不足でティアは下がらない"
          factors={threat.capability_factors}
        />
        <FactorGroup
          label={`③ 活動状態 (ティア非影響)`}
          hint="報道テンポ。休眠は脅威消滅ではない"
          factors={threat.activity_factors}
        />
      </div>
      {threat.coverage_note && (
        <p className="m-0 mt-2.5 text-[11px] text-warning bg-warning-soft rounded px-2.5 py-1.5">
          {threat.coverage_note}
        </p>
      )}
    </Section>
  );
}

function FactorGroup({ label, hint, factors }: { label: string; hint: string; factors: string[] }) {
  return (
    <div className="bg-surface-2 rounded-md p-2.5">
      <div className="text-[10.5px] font-semibold text-fg mb-0.5">{label}</div>
      <div className="text-[9.5px] text-fg-subtle mb-1.5 leading-snug">{hint}</div>
      <ul className="list-none m-0 p-0 space-y-1">
        {factors.map((f, i) => (
          <li key={i} className="text-[11px] text-fg-muted leading-snug flex gap-1.5">
            <span className="text-fg-subtle shrink-0">·</span>
            <span>{f}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function HeroBadge({ children, variant }: { children: React.ReactNode; variant?: "accent" | "mitre" | "warning" }) {
  const styles = variant === "accent"
    ? "bg-accent-subtle text-accent-hover border-transparent"
    : variant === "mitre"
    ? "bg-surface-3 text-fg-muted border-border-emphasized"
    : variant === "warning"
    ? "bg-warning-soft text-warning border-transparent"
    : "bg-surface-3 text-fg-muted border-border-subtle";
  return <span className={`px-2.5 py-0.5 rounded-full text-xs font-medium font-mono border ${styles}`}>{children}</span>;
}

function Stat({ label, value, suffix, trend, trendText, small }: {
  label: string; value: string; suffix?: string; trend?: string; trendText?: string; small?: boolean
}) {
  return (
    <div className="bg-surface-1 px-4 py-3.5">
      <div className="text-[10.5px] text-fg-subtle uppercase tracking-wider font-semibold mb-1">{label}</div>
      <div className={`font-bold text-fg leading-tight tnum tracking-tight ${small ? "text-base font-mono" : "text-xl"}`}>
        {value}{suffix && <span className="text-fg-muted text-xs ml-0.5 font-medium">{suffix}</span>}
      </div>
      {trendText && (
        <div className={`text-xs mt-0.5 ${trend === "up" ? "text-critical" : "text-fg-muted"}`}>{trendText}</div>
      )}
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="px-6 py-4 border-b border-border-subtle last:border-b-0">
      <h4 className="m-0 mb-3 text-[10.5px] uppercase tracking-wider text-fg-muted font-semibold flex items-center gap-2">{title}</h4>
      {children}
    </div>
  );
}

function Pills({ items, mono = true }: { items: string[]; mono?: boolean }) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {items.map((it, i) => (
        <span key={i} className={`bg-surface-2 border border-border-default text-fg px-2.5 py-0.5 rounded-full text-xs ${mono ? "font-mono" : ""} hover:border-accent-soft hover:text-accent-hover transition-colors`}>{it}</span>
      ))}
    </div>
  );
}

// Phase Diamond: 1 軸を表す collapsible group。empty 時も visible にして「データなし」hint を出す。
// Mobile (<md) では default 閉じ、md+ では default 開く (scroll 量を抑える)。
function DiamondGroup({
  title, children, empty, emptyHint, anchorId,
}: { title: string; children: React.ReactNode; empty: boolean; emptyHint?: string; anchorId?: string }) {
  const [defaultOpen] = useState<boolean>(() => {
    if (typeof window === "undefined") return true;
    return window.matchMedia("(min-width: 768px)").matches;
  });
  if (empty) {
    return (
      <div id={anchorId} className="px-6 py-3 border-b border-border-subtle last:border-b-0 bg-surface-1/40 scroll-mt-4">
        <h4 className="m-0 text-[11px] uppercase tracking-wider text-fg-subtle font-semibold flex items-center gap-2">{title}</h4>
        <p className="m-0 mt-1 text-fg-subtle text-xs italic">
          {emptyHint || "該当データなし"}
        </p>
      </div>
    );
  }
  return (
    <details id={anchorId} className="border-b border-border-subtle last:border-b-0 open:bg-surface-1/50 scroll-mt-4" open={defaultOpen}>
      <summary className="cursor-pointer list-none px-6 py-3 hover:bg-surface-2 transition-colors [&::-webkit-details-marker]:hidden">
        <h4 className="m-0 text-[11px] uppercase tracking-wider text-accent-hover font-semibold flex items-center gap-2">
          <span className="text-fg-subtle text-[9px]">▼</span>{title}
        </h4>
      </summary>
      <div className="px-6 pb-4 pt-1 space-y-2.5">
        {children}
      </div>
    </details>
  );
}

function LegendDot({ hex, label }: { hex: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1">
      <span className="w-2 h-2 rounded-full" style={{ backgroundColor: hex }} />
      {label}
    </span>
  );
}

function Subsection({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-[10px] text-fg-subtle mb-1 uppercase tracking-wider">{label}</div>
      {children}
    </div>
  );
}

function Relations({ family, familyMembers, cooccurActors, actorLookup, onSelect }: {
  family: string;
  familyMembers: [string, number][];
  cooccurActors: [string, number][];
  actorLookup: Record<string, string>;
  onSelect: (id: string) => void;
}) {
  return (
    <>
      <div className="p-3.5 border-b border-border-subtle">
        <h4 className="m-0 mb-2.5 text-[10.5px] uppercase tracking-wider text-fg-muted font-semibold flex items-center justify-between gap-2">
          <span>系統</span>
          {family && <span className="bg-accent-subtle text-accent-hover px-1.5 py-0.5 rounded-sm text-[10.5px] font-mono normal-case tracking-normal font-medium">{family}</span>}
        </h4>
        {familyMembers.length === 0 ? <div className="text-fg-subtle text-xs italic">系統メンバーなし</div> : familyMembers.map(([id, n]) => (
          <div key={id} onClick={() => onSelect(id)} className="py-1 px-2 -mx-2 rounded-sm cursor-pointer flex justify-between gap-2 text-sm hover:bg-surface-2 hover:text-accent-hover transition-all">
            <span className="text-fg font-medium truncate">{actorLookup[id] || id}</span>
            <span className="text-fg-subtle text-xs tnum shrink-0">{n}</span>
          </div>
        ))}
      </div>
      <div className="p-3.5 border-b border-border-subtle">
        <h4 className="m-0 mb-2.5 text-[10.5px] uppercase tracking-wider text-fg-muted font-semibold">共起</h4>
        {cooccurActors.length === 0 ? <div className="text-fg-subtle text-xs italic">共起 actor なし</div> : cooccurActors.map(([id, n]) => (
          <div key={id} onClick={() => onSelect(id)} className="py-1 px-2 -mx-2 rounded-sm cursor-pointer flex justify-between gap-2 text-sm hover:bg-surface-2 hover:text-accent-hover transition-all">
            <span className="text-fg font-medium truncate">{actorLookup[id] || id}</span>
            <span className="text-fg-subtle text-xs tnum shrink-0">{n}</span>
          </div>
        ))}
      </div>
    </>
  );
}

// SVG bar chart for actor activity timeline (Plotly 依存を排除、bundle 無影響)。
function TimelineBars({ data }: { data: [string, number][] }) {
  if (data.length === 0) {
    return (
      <div className="h-32 bg-surface-2 rounded-md p-3 flex items-center justify-center text-fg-subtle text-xs">
        記事なし
      </div>
    );
  }
  const max = Math.max(1, ...data.map((d) => d[1]));
  const w = 100; // viewport %
  const barWidth = w / data.length;
  const gap = barWidth * 0.18;
  const innerWidth = barWidth - gap;
  return (
    <div className="h-32 bg-surface-2 rounded-md p-2 flex flex-col">
      <svg viewBox={`0 0 ${w} 100`} preserveAspectRatio="none" className="flex-1 w-full">
        {data.map(([date, count], i) => {
          const h = (count / max) * 90; // top 10% は label 用 padding
          const x = i * barWidth + gap / 2;
          const y = 100 - h;
          return (
            <rect
              key={date}
              x={x}
              y={y}
              width={innerWidth}
              height={h}
              fill="#6b88ff"
              opacity={count === 0 ? 0.15 : 0.9}
            >
              <title>{date}: {count} 件</title>
            </rect>
          );
        })}
      </svg>
      <div className="flex justify-between mt-1 text-[9px] text-fg-subtle tnum">
        <span>{data[0]?.[0].slice(5)}</span>
        <span className="text-fg-muted">max {max}</span>
        <span>{data[data.length - 1]?.[0].slice(5)}</span>
      </div>
    </div>
  );
}
