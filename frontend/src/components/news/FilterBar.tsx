// ニュース検索・事象ニュースが共有する絞り込みバー (docs/news_filter_ux.md §2)。
//
// 常に出すのは 検索語 / 重要度 / 関係 / 期間 / 並び の 4+1。残りは「詳細な絞り込み」に
// 畳む。畳んでいる条件もチップで見える (× で外す・「すべて解除」)。
//
// 「関係」は旧「関連性」(relevantOnly) と「日本との関係」(jp) を 1 本に統合した選択。
// 日本が標的・被害は関連性ありの一部なので、両方が同時に効く組み合わせは作らない —
// jp が入っていればそれを優先する (旧 URL で両方指定されていた場合も同じ優先順位)。

import { useMemo, useState, type KeyboardEvent, type ReactNode } from "react";
import { ChevronDown, ChevronUp } from "lucide-react";
import {
  MIN_SEVERITY_OPTS, SEARCH_MODE_OPTS, Sel, SINCE_OPTS, SORT_OPTS, type FacetOptions, type Opt,
  type SearchMode, type SeverityFacetState, VendorInput,
} from "./facets";

export type RelationValue = "" | "relevant" | "targeted_affected" | "mentioned";

/** jp と relevantOnly から「関係」の単一値を導く。jp が入っていれば jp を優先する
 *  (旧 URL で relevant_only=1 と jp が両方指定されていた場合の後方互換)。 */
export function relationFromState(jp: string, relevantOnly: boolean): RelationValue {
  if (jp === "targeted_affected" || jp === "mentioned") return jp;
  return relevantOnly ? "relevant" : "";
}

/** 「関係」の選択 → (jp, relevantOnly)。排他 (同時に両方効くことはない)。 */
export function applyRelation(value: RelationValue): { jp: "" | "targeted_affected" | "mentioned"; relevantOnly: boolean } {
  if (value === "targeted_affected" || value === "mentioned") return { jp: value, relevantOnly: false };
  if (value === "relevant") return { jp: "", relevantOnly: true };
  return { jp: "", relevantOnly: false };
}

export const RELATION_OPTS: Opt[] = [
  { value: "", label: "関係: すべて" },
  { value: "relevant", label: "関係: 関連性あり" },
  { value: "targeted_affected", label: "関係: 日本が標的・被害" },
  { value: "mentioned", label: "関係: 日本に触れるもの" },
];

export interface EventOnlyFilters {
  minSources: boolean;
  onMinSources: (v: boolean) => void;
  hasNews: boolean;
  onHasNews: (v: boolean) => void;
  newFacts: boolean;
  onNewFacts: (v: boolean) => void;
}

export interface FilterBarProps {
  facetOpts: FacetOptions;

  searchValue: string;
  onSearchChange: (v: string) => void;
  searchPlaceholder: string;
  /** 事象ニュースの意味検索ボタン等、検索 box の隣に置く追加要素。 */
  searchExtra?: ReactNode;
  /** 事象ニュースは Enter 確定 (打鍵ごとに走らせない)。省略時は通常の即時入力。 */
  onSearchKeyDown?: (e: KeyboardEvent<HTMLInputElement>) => void;

  /** 検索モード (キーワード / 意味も含める)。ニュース検索・事象ニュースが共有する
   *  統一コントロール (2026-10-04)。省略時は select 自体を出さない — 写し
   *  (embedding が使えない) は呼び手が undefined を渡して隠す。 */
  searchMode?: SearchMode;
  onSearchMode?: (v: SearchMode) => void;

  severity: SeverityFacetState;
  onSeverity: (s: SeverityFacetState) => void;

  /** 関係 (関連性あり / 日本が標的・被害 / 日本に触れるもの)。旧「関連性」と
   *  「日本との関係」を 1 本に統合した選択 (relationFromState/applyRelation)。 */
  relation: RelationValue;
  onRelation: (v: RelationValue) => void;

  since: string;
  onSince: (v: string) => void;

  /** 並び順。省略時は並び替え無し (ニュース検索の検索ビューと同じ)。 */
  sort?: string;
  onSort?: (v: string) => void;

  category: string;
  onCategory: (v: string) => void;
  feed: string;
  onFeed: (v: string) => void;
  intent: string;
  onIntent: (v: string) => void;
  pir: string;
  onPir: (v: string) => void;
  actor: string;
  onActor: (v: string) => void;
  vendorRaw: string;
  onVendorRaw: (v: string) => void;
  vendorApplied: string;

  /** 本文の有無。ニュース検索のみ。 */
  body?: { value: string; onChange: (v: string) => void };
  /** 購読チャンネル。運用画面のみ (呼び手が写しでは渡さない)。 */
  channel?: { value: string; onChange: (v: string) => void };
  eventOnly?: EventOnlyFilters;

  /** 見出し/要約・クイック/精密 等、常時行の末尾に置く表示切替。 */
  trailing?: ReactNode;
}

function FacetToggleSmall({ on, onClick, title, children }: {
  on: boolean; onClick: () => void; title: string; children: ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      title={title}
      aria-pressed={on}
      className={`text-xs px-3 py-1.5 rounded border transition-colors ${
        on ? "border-accent text-accent bg-accent/10" : "border-border-default text-fg-muted hover:text-fg"
      }`}
    >
      {children}
    </button>
  );
}

interface Chip { key: string; text: string; onClear: () => void }

export function FilterBar(props: FilterBarProps) {
  const [detailsOpen, setDetailsOpen] = useState(false);

  return (
    <div className="space-y-2">
      {/* 常時行: 検索語 / 重要度 / 関係 / 期間 / 並び */}
      <div className="md:sticky md:top-12 z-20 bg-bg/95 backdrop-blur-md border-b border-border-subtle -mx-4 px-4 md:mx-0 md:px-0 py-2 flex flex-wrap items-center gap-2">
        <input
          value={props.searchValue}
          onChange={(e) => props.onSearchChange(e.target.value)}
          onKeyDown={props.onSearchKeyDown}
          placeholder={props.searchPlaceholder}
          className="h-8 px-3 bg-surface-2 border border-border-subtle rounded-md text-sm min-w-[180px] flex-1 max-w-[320px] placeholder:text-fg-subtle focus:outline-none focus:border-accent"
        />
        {props.searchExtra}
        {props.searchMode !== undefined && props.onSearchMode && (
          <Sel
            value={props.searchMode}
            onChange={(v) => props.onSearchMode!(v as SearchMode)}
            opts={SEARCH_MODE_OPTS}
            title="検索モード (キーワード = 文字列一致 / 意味も含める = 言い換え・多言語も拾う)"
          />
        )}
        <Sel
          value={props.severity.minSeverity}
          onChange={(v) => props.onSeverity({ ...props.severity, minSeverity: v as SeverityFacetState["minSeverity"] })}
          opts={MIN_SEVERITY_OPTS}
          title="重要度 (深刻さ)"
        />
        <Sel
          value={props.relation}
          onChange={(v) => props.onRelation(v as RelationValue)}
          opts={RELATION_OPTS}
          title="関係 (関連性・日本との関係)"
        />
        <Sel value={props.since} onChange={props.onSince} opts={SINCE_OPTS} title="期間" />
        {props.sort !== undefined && props.onSort && (
          <Sel value={props.sort} onChange={props.onSort} opts={SORT_OPTS} title="並び" />
        )}
        {props.trailing}
        <button
          onClick={() => setDetailsOpen((v) => !v)}
          className="h-8 px-2.5 inline-flex items-center gap-1 rounded-md border border-border-subtle text-fg-muted hover:text-fg text-sm ml-auto"
        >
          詳細な絞り込み
          {detailsOpen ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
        </button>
      </div>

      {detailsOpen && (
        <div className="flex flex-wrap items-center gap-2 p-2 rounded-md bg-surface-1 border border-border-subtle">
          <Sel value={props.category} onChange={props.onCategory} opts={props.facetOpts.category} />
          <Sel value={props.feed} onChange={props.onFeed} opts={props.facetOpts.feed} />
          <Sel value={props.intent} onChange={props.onIntent} opts={props.facetOpts.intent} />
          <Sel value={props.pir} onChange={props.onPir} opts={props.facetOpts.pir} />
          <Sel value={props.actor} onChange={props.onActor} opts={props.facetOpts.actor} />
          <VendorInput
            raw={props.vendorRaw}
            onChange={props.onVendorRaw}
            applied={props.vendorApplied}
            options={props.facetOpts.vendor}
            listId="filterbar-vendor-list"
          />
          {props.body && (
            <select
              value={props.body.value}
              onChange={(e) => props.body!.onChange(e.target.value)}
              className="h-8 pl-2.5 pr-7 bg-surface-2 border border-border-subtle rounded-md text-sm"
            >
              <option value="">本文: 全て</option>
              <option value="full">全文取得済</option>
              <option value="stump">切り株のみ</option>
            </select>
          )}
          {props.channel && (
            <Sel value={props.channel.value} onChange={props.channel.onChange} opts={props.facetOpts.channel} />
          )}
          <FacetToggleSmall
            on={props.severity.includeStrategic}
            onClick={() => props.onSeverity({ ...props.severity, includeStrategic: !props.severity.includeStrategic })}
            title="深刻さを絞り込み中、政策・地政学 (注視国が主体) の記事も合わせて含める"
          >
            政策・地政学を含める
          </FacetToggleSmall>
          {props.eventOnly && (
            <>
              <FacetToggleSmall on={props.eventOnly.minSources} onClick={() => props.eventOnly!.onMinSources(!props.eventOnly!.minSources)} title="独立した 2 媒体以上が報じた事象だけ">
                複数媒体
              </FacetToggleSmall>
              <FacetToggleSmall on={props.eventOnly.hasNews} onClick={() => props.eventOnly!.onHasNews(!props.eventOnly!.hasNews)} title="統合本文を持つ事象だけ">
                統合済み
              </FacetToggleSmall>
              <FacetToggleSmall on={props.eventOnly.newFacts} onClick={() => props.eventOnly!.onNewFacts(!props.eventOnly!.newFacts)} title="初報のあと新しい事実が加わった事象">
                新事実あり
              </FacetToggleSmall>
            </>
          )}
        </div>
      )}

      <ActiveChips {...props} />
    </div>
  );
}

function ActiveChips(props: FilterBarProps): ReactNode {
  const chips = useMemo<Chip[]>(() => {
    const out: Chip[] = [];
    if (props.severity.minSeverity) {
      const text = MIN_SEVERITY_OPTS.find((o) => o.value === props.severity.minSeverity)?.label ?? props.severity.minSeverity;
      out.push({ key: "min_severity", text, onClear: () => props.onSeverity({ ...props.severity, minSeverity: "" }) });
    }
    if (props.relation) {
      const text = RELATION_OPTS.find((o) => o.value === props.relation)?.label ?? props.relation;
      out.push({ key: "relation", text, onClear: () => props.onRelation("") });
    }
    if (props.category) out.push({ key: "category", text: `カテゴリ: ${labelOf(props.facetOpts.category, props.category)}`, onClear: () => props.onCategory("") });
    if (props.feed) out.push({ key: "feed", text: `サイト: ${props.feed}`, onClear: () => props.onFeed("") });
    if (props.intent) out.push({ key: "intent", text: `意図: ${labelOf(props.facetOpts.intent, props.intent)}`, onClear: () => props.onIntent("") });
    if (props.pir) out.push({ key: "pir", text: `SIR: ${labelOf(props.facetOpts.pir, props.pir)}`, onClear: () => props.onPir("") });
    if (props.actor) out.push({ key: "actor", text: `アクター: ${labelOf(props.facetOpts.actor, props.actor)}`, onClear: () => props.onActor("") });
    if (props.vendorApplied) out.push({ key: "vendor", text: `影響: ${props.vendorApplied}`, onClear: () => { props.onVendorRaw(""); } });
    if (props.body?.value) out.push({ key: "body", text: props.body.value === "full" ? "全文取得済" : "切り株のみ", onClear: () => props.body!.onChange("") });
    if (props.channel?.value) out.push({ key: "channel", text: `チャンネル: ${labelOf(props.facetOpts.channel, props.channel.value)}`, onClear: () => props.channel!.onChange("") });
    if (props.severity.includeStrategic) out.push({ key: "strategic", text: "政策・地政学を含む", onClear: () => props.onSeverity({ ...props.severity, includeStrategic: false }) });
    if (props.searchMode === "semantic" && props.onSearchMode) out.push({ key: "search_mode", text: "意味も含める", onClear: () => props.onSearchMode!("keyword") });
    if (props.eventOnly?.minSources) out.push({ key: "minsrc", text: "複数媒体", onClear: () => props.eventOnly!.onMinSources(false) });
    if (props.eventOnly?.hasNews) out.push({ key: "hasnews", text: "統合済み", onClear: () => props.eventOnly!.onHasNews(false) });
    if (props.eventOnly?.newFacts) out.push({ key: "newfacts", text: "新事実あり", onClear: () => props.eventOnly!.onNewFacts(false) });
    return out;
  }, [props]);

  if (chips.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5 text-xs">
      <span className="text-fg-subtle">絞り込み:</span>
      {chips.map((c) => (
        <button
          key={c.key}
          onClick={c.onClear}
          className="inline-flex items-center gap-1 bg-accent/10 border border-accent-soft rounded px-2 py-0.5 text-accent hover:bg-accent/20"
        >
          {c.text} <span className="text-fg-subtle">×</span>
        </button>
      ))}
      <button
        onClick={() => {
          props.onCategory("");
          props.onFeed("");
          props.onIntent("");
          props.onPir("");
          props.onActor("");
          props.onVendorRaw("");
          props.body?.onChange("");
          props.channel?.onChange("");
          props.onSeverity({ minSeverity: "", relevantOnly: false, includeStrategic: false });
          props.onRelation("");
          props.onSince("0");
          props.onSort?.("");
          props.eventOnly?.onMinSources(false);
          props.eventOnly?.onHasNews(false);
          props.eventOnly?.onNewFacts(false);
        }}
        className="text-fg-subtle hover:text-accent underline"
      >
        すべて解除
      </button>
    </div>
  );
}

function labelOf(opts: { value: string; label: string }[], value: string): string {
  return opts.find((o) => o.value === value)?.label ?? value;
}
