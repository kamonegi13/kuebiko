// ニュース検索と事象ニュースが **共有** する絞り込み UI と選択肢。
//
// 2 画面に同じ facet を置くにあたり、カテゴリ・チャンネル・意図のラベル辞書を
// 複製しない (CLAUDE.md §7: ラベルは SSoT を参照)。選択肢はすべて backend 配信の
// 語彙 / live registry / 実データ由来で、ここは組み立てるだけ。

import { useMemo, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { pirApi } from "../../api/pir";
import { fetchActorOptions, fetchAffectedVendors, fetchFeedOptions } from "../../api/search";
import { useChannels } from "../channel";
import { useVocabOptions, vocabLabel } from "../../hooks/useVocab";
import { label } from "../../utils/labels";

export interface Opt {
  value: string;
  label: string;
}

/** 期間。記事も事象も同じ刻みで選べるようにする (画面ごとに刻みが違うと比較できない)。 */
export const SINCE_OPTS: Opt[] = [
  { value: "0", label: "全期間" },
  { value: "24", label: "24時間" },
  { value: "72", label: "3日" },
  { value: "168", label: "7日" },
  { value: "720", label: "30日" },
  { value: "2160", label: "90日" },
];

/** 記事側の重要度 (単一値)。事象側は "high,medium" のような複数指定を使うため別定義。 */
export const IMPORTANCE_OPTS: Opt[] = [
  { value: "", label: "全重要度" },
  { value: "high", label: "High" },
  { value: "medium", label: "Medium" },
];

/** 深刻さ・関連性・戦略上の重みの 3 独立 facet (2026-10-04、利用者決定)。
 *
 *  旧 1 本化 facet (``level_filter``: top/notable/relevant 単一選択) は「関連性ありの
 *  重大」のように表せない組み合わせがあったため、3 つの独立コントロールに置き換えた。
 *  値は API の ``min_severity`` / ``relevant_only`` / ``include_strategic`` パラメータと
 *  一致。生の enum (S3/S2/S1) は画面に出さない (CLAUDE.md §UI 文言規約)。
 */
export interface SeverityFacetState {
  /** ""=すべて / "S3"=重大のみ / "S2"=注意以上 / "S1"=参考以上 (severity 記録済み全部)。 */
  minSeverity: "" | "S3" | "S2" | "S1";
  /** 関連性 (日本・注視国・SIR) ありの記事だけ。深刻さの設定と独立に効く。 */
  relevantOnly: boolean;
  /** ``minSeverity`` 指定時のみ意味を持つ: 深刻さ無し (政策・地政学) で
   *  strategic_weight='heavy' の記事も合わせて含める。 */
  includeStrategic: boolean;
}

export const EMPTY_SEVERITY_FACET: SeverityFacetState = {
  minSeverity: "",
  relevantOnly: false,
  includeStrategic: false,
};

/** 深刻さ facet の選択肢 (単一 select、関連性・戦略上の重みは別コントロール)。 */
export const MIN_SEVERITY_OPTS: Opt[] = [
  { value: "", label: "深刻さ: すべて" },
  { value: "S3", label: "深刻さ: 重大のみ" },
  { value: "S2", label: "深刻さ: 注意以上" },
  { value: "S1", label: "深刻さ: 参考以上" },
];

/** 旧 1 本化 ``level_filter`` (top/notable/relevant) → 新 3 facet への写像 (後方互換)。
 *  SSoT は ``src/cti/importance_v2.py:legacy_level_filter_to_severity`` と対照
 *  (同じ意味論。食い違えば API と UI の挙動がずれる)。
 *  "notable" は旧仕様で軸なし heavy も含んでいたため includeStrategic=true に写す。
 *  "relevant" は旧仕様が severity 記録済みのみを対象にしていたため
 *  minSeverity="S1" (参考以上) + relevantOnly=true に写す。 */
export function legacyLevelFilterToSeverityFacet(
  levelFilter: string | null | undefined,
): SeverityFacetState {
  if (levelFilter === "top") return { minSeverity: "S3", relevantOnly: false, includeStrategic: false };
  if (levelFilter === "notable") return { minSeverity: "S2", relevantOnly: false, includeStrategic: true };
  if (levelFilter === "relevant") return { minSeverity: "S1", relevantOnly: true, includeStrategic: false };
  return EMPTY_SEVERITY_FACET;
}

/** 旧 widget 設定 (``importance``: high/medium/low) → 旧 ``level_filter`` への移行
 *  (2026-10-04 より前の保存済み設定用)。high→top (重大のみ) /
 *  medium or "high,medium"→notable (注意以上) / 不明な値→"" (すべて)。 */
export function migrateImportanceToLevelFilter(old: string | null | undefined): string {
  if (!old) return "";
  const v = old.trim();
  if (v === "high") return "top";
  if (v === "medium" || v === "high,medium" || v === "medium,high") return "notable";
  return "";
}

/** 旧設定 (``level_filter`` または更に古い ``importance``) → 新 3 facet への移行。
 *  新 facet の値が 1 つでも明示されていればそれを使う (呼び手が判定する)。 */
export function migrateLegacyToSeverityFacet(
  levelFilter: string | null | undefined,
  legacyImportance?: string | null,
): SeverityFacetState {
  const lf = levelFilter || migrateImportanceToLevelFilter(legacyImportance);
  return legacyLevelFilterToSeverityFacet(lf);
}

/** widget 保存設定 (すべて文字列) から ``SeverityFacetState`` を読む。``URLSearchParams``
 *  が無い文脈 (dashboard widget の ``config: Record<string, unknown>``) 用。新 3 facet の
 *  いずれかが明示されていればそれを優先し、無ければ旧 ``level_filter``/``importance`` を
 *  移行する。呼び手は ``cfgStr(config, key, "")`` で素の文字列を渡す。 */
export function severityFacetFromConfigStrings(
  minSeverityRaw: string,
  relevantOnlyRaw: string,
  includeStrategicRaw: string,
  levelFilterRaw: string,
  legacyImportanceRaw: string,
  fallback: SeverityFacetState,
): SeverityFacetState {
  if (minSeverityRaw || relevantOnlyRaw || includeStrategicRaw) {
    const minSeverity =
      minSeverityRaw === "S3" || minSeverityRaw === "S2" || minSeverityRaw === "S1"
        ? minSeverityRaw
        : "";
    return {
      minSeverity,
      relevantOnly: relevantOnlyRaw === "1",
      includeStrategic: includeStrategicRaw === "1",
    };
  }
  if (levelFilterRaw || legacyImportanceRaw) {
    return migrateLegacyToSeverityFacet(levelFilterRaw, legacyImportanceRaw);
  }
  return fallback;
}

/** URLSearchParams から ``SeverityFacetState`` を読む。新 3 facet のいずれかが
 *  明示されていればそれを優先し、無ければ旧 ``level_filter``/``importance`` を移行する。
 *  いずれも無ければ ``fallback`` (画面ごとの既定: ニュース検索はすべて、事象ニュースは
 *  注意以上)。 */
export function readSeverityFacet(
  p: URLSearchParams,
  fallback: SeverityFacetState = EMPTY_SEVERITY_FACET,
): SeverityFacetState {
  if (p.has("min_severity") || p.has("relevant_only") || p.has("include_strategic")) {
    const raw = p.get("min_severity") ?? "";
    const minSeverity = raw === "S3" || raw === "S2" || raw === "S1" ? raw : "";
    return {
      minSeverity,
      relevantOnly: p.get("relevant_only") === "1",
      includeStrategic: p.get("include_strategic") === "1",
    };
  }
  const levelFilterRaw = p.get("level_filter");
  const legacyImportance = p.get("importance");
  if (levelFilterRaw || legacyImportance) {
    return migrateLegacyToSeverityFacet(levelFilterRaw, legacyImportance);
  }
  return fallback;
}

/** ``SeverityFacetState`` を URLSearchParams へ書く (読み戻しと対で保つ)。 */
export function writeSeverityFacet(q: URLSearchParams, s: SeverityFacetState): void {
  if (s.minSeverity) q.set("min_severity", s.minSeverity);
  if (s.relevantOnly) q.set("relevant_only", "1");
  if (s.minSeverity && s.includeStrategic) q.set("include_strategic", "1");
}

/** API 呼び出し用の query パラメータへ変換 (``ArticleFeedParams`` / ``EventNewsQuery``
 *  共通のフィールド名)。すべて未設定なら空オブジェクト (絞り込み無しのまま送らない)。 */
export function severityFacetQueryParams(s: SeverityFacetState): {
  min_severity?: "" | "S3" | "S2" | "S1";
  relevant_only?: boolean;
  include_strategic?: boolean;
} {
  return {
    min_severity: s.minSeverity || undefined,
    relevant_only: s.relevantOnly || undefined,
    include_strategic: s.minSeverity && s.includeStrategic ? true : undefined,
  };
}

/** 本文由来フィルタ: 全文取得できた記事 / 切り株 (フィード抜粋のみ)。記事画面専用。 */
export const BODY_OPTS: Opt[] = [
  { value: "", label: "本文: 全て" },
  { value: "full", label: "全文取得済" },
  { value: "stump", label: "切り株のみ" },
];

/** 日本との関係 (2026-10-04、article_importance_v2.jp 由来)。チャンネルより上位の絞り込みとして
 *  ニュース検索・事象ニュース・ダッシュボード「最新ニュース」の 3 画面が共有する。
 *  生の enum (targeted/affected/mentioned/none) は画面に出さない (CLAUDE.md §UI文言規約)。 */
export const JP_OPTS: Opt[] = [
  { value: "", label: "日本との関係: すべて" },
  { value: "targeted_affected", label: "日本が標的・被害" },
  { value: "mentioned", label: "日本に触れるもの" },
];

/** 並び順。ニュース検索・事象ニュースが共有する (2026-10-04)。
 *  "level" = 重要度 6 段階 (深刻さ×関連性、src/cti/importance_v2.py)。 */
export const SORT_OPTS: Opt[] = [
  { value: "", label: "新しい順" },
  { value: "level", label: "重要度順" },
];

/** 重要度 6 段階のラベル (1 が最上位)。SSoT は
 *  src/cti/importance_v2.py の importance_level()。◎ = 関連性あり
 *  (日本・注視国・SIR)。生の severity/level を画面に出さない
 *  (CLAUDE.md §UI 文言規約)。 */
const LEVEL_LABELS: Record<number, string> = {
  1: "重大◎",
  2: "重大",
  3: "注意◎",
  4: "注意",
  5: "参考◎",
  6: "参考",
};

export function levelLabel(level: number | null | undefined): string | null {
  if (level == null) return null;
  return LEVEL_LABELS[level] ?? null;
}

/** 重要度バッジ。level が無い (未記録) 記事・事象には何も出さない。 */
export function LevelBadge({ level }: { level: number | null | undefined }) {
  const label = levelLabel(level);
  if (!label) return null;
  return (
    <span className="px-1 rounded bg-surface-2 text-fg-muted" title="重要度 (深刻さ×関連性)">
      {label}
    </span>
  );
}

export interface FacetOptions {
  category: Opt[];
  channel: Opt[];
  feed: Opt[];
  intent: Opt[];
  pir: Opt[];
  actor: Opt[];
  /** 影響ベンダ/製品は datalist 補完 (自由入力を許す)。 */
  vendor: string[];
  pirLabel: Map<string, string>;
  actorLabel: Map<string, string>;
}

/**
 * facet の選択肢をまとめて解決する。
 *
 * すべて staleTime 付きの useQuery なので、2 画面が同じ key を共有して 1 回しか
 * 取りに行かない (画面を行き来しても再取得しない)。
 */
export function useFacetOptions(): FacetOptions {
  const category: Opt[] = [
    { value: "", label: "全カテゴリ" },
    { value: "vuln", label: vocabLabel("category_group", "vuln") },
    { value: "threat", label: vocabLabel("category_group", "threat") },
    { value: "incident_breach", label: vocabLabel("category_group", "incident_breach") },
    { value: "vulnerability", label: vocabLabel("category", "vulnerability") },
    { value: "breach", label: vocabLabel("category", "breach") },
    { value: "malware", label: vocabLabel("category", "malware") },
    { value: "apt", label: vocabLabel("category", "apt") },
    { value: "geopolitical", label: "地政" },
    { value: "policy", label: "サイバー政策" },
    { value: "research", label: vocabLabel("category", "research") },
    { value: "advisory", label: vocabLabel("category", "advisory") },
  ];

  // チャンネルは live registry から (custom / ops も含む。固定マップは stale 化する)
  const channel: Opt[] = [
    { value: "", label: "全チャンネル" },
    ...useChannels().map((c) => ({ value: c.id, label: c.label })),
  ];

  const intent: Opt[] = [
    { value: "", label: "全意図" },
    ...useVocabOptions("intent").map((i) => ({ value: i.value, label: i.label })),
  ];

  // 情報源は実データ由来 (購読一覧だと Grok 等の購読外経路が選べない — 2026-08-15)
  const { data: feedList } = useQuery({
    queryKey: ["facet-feeds"],
    queryFn: () => fetchFeedOptions(),
    staleTime: 10 * 60_000,
  });
  const feed = useMemo(
    () => [
      { value: "", label: "全サイト" },
      ...(feedList ?? [])
        .map((f) => ({ value: f.title, label: f.title }))
        .sort((a, b) => a.label.localeCompare(b.label)),
    ],
    [feedList],
  );

  // ⚠ `pirApi.list()` は KPI 付き (30 日 × 15,000 記事の走査、cold 1.9 秒、DB 接続を
  // 1 本占有)。dropdown から呼ぶと全ページ読み込みで走り、接続プールを使い切る
  // (2026-08-25 に実際にアプリ全体を停止させた)。選択肢は軽量経路から取る。
  const { data: pirList } = useQuery({
    queryKey: ["facet-pir-options"],
    queryFn: () => pirApi.options(),
    staleTime: 10 * 60_000,
  });
  const pir = useMemo(
    () => [
      { value: "", label: "全SIR" },
      ...(pirList ?? []).filter((p) => p.enabled).map((p) => ({ value: p.id, label: p.title })),
    ],
    [pirList],
  );
  const pirLabel = useMemo(
    () => new Map((pirList ?? []).map((p) => [p.id, p.title])),
    [pirList],
  );

  const { data: actorList } = useQuery({
    queryKey: ["facet-actors"],
    queryFn: () => fetchActorOptions(),
    staleTime: 30 * 60_000,
  });
  const actor = useMemo(
    () => [{ value: "", label: "全アクター" }, ...(actorList ?? []).map((a) => ({ value: a.id, label: a.name }))],
    [actorList],
  );
  const actorLabel = useMemo(
    () => new Map((actorList ?? []).map((a) => [a.id, a.name])),
    [actorList],
  );

  const { data: vendorOpts } = useQuery({
    queryKey: ["facet-vendors"],
    queryFn: () => fetchAffectedVendors(),
    staleTime: 30 * 60_000,
  });

  return { category, channel, feed, intent, pir, actor, vendor: vendorOpts ?? [], pirLabel, actorLabel };
}

/** facet の select。値が入っていると accent 枠で「効いている」ことを示す。 */
export function Sel({
  value,
  onChange,
  opts,
  title,
}: {
  value: string;
  onChange: (v: string) => void;
  opts: Opt[];
  title?: string;
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      title={title}
      className={`h-8 pl-2.5 pr-7 bg-surface-2 border rounded-md text-sm cursor-pointer max-w-[180px] focus:outline-none ${
        value ? "border-accent text-accent-hover" : "border-border-subtle text-fg"
      }`}
    >
      {opts.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
    </select>
  );
}

/** on/off トグル。select と違い「効いている状態」が一目で分かる (事象ニュースの
 *  複数媒体/統合済み/新事実あり と同じ見た目)。``disabled`` は深刻さ無指定時に
 *  「政策・地政学を含める」を無効化するために使う。 */
export function FacetToggle({
  on,
  onClick,
  title,
  disabled,
  children,
}: {
  on: boolean;
  onClick: () => void;
  title: string;
  disabled?: boolean;
  children: ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      title={title}
      aria-pressed={on}
      disabled={disabled}
      className={`text-xs px-3 py-1.5 rounded border transition-colors ${
        disabled
          ? "border-border-subtle text-fg-subtle opacity-50 cursor-not-allowed"
          : on
            ? "border-accent text-accent bg-accent/10"
            : "border-border-default text-fg-muted hover:text-fg"
      }`}
    >
      {children}
    </button>
  );
}

/** 深刻さ・関連性・戦略上の重みの 3 独立コントロール。ニュース検索・事象ニュースが
 *  共有する (2026-10-04)。select + 2 トグルの並びを両画面で揃える。 */
export function SeverityFacetControls({
  state,
  onChange,
}: {
  state: SeverityFacetState;
  onChange: (next: SeverityFacetState) => void;
}) {
  return (
    <>
      <Sel
        value={state.minSeverity}
        onChange={(v) => onChange({ ...state, minSeverity: v as SeverityFacetState["minSeverity"] })}
        opts={MIN_SEVERITY_OPTS}
      />
      <FacetToggle
        on={state.relevantOnly}
        onClick={() => onChange({ ...state, relevantOnly: !state.relevantOnly })}
        title="日本 (標的・被害)・注視国 (中国・ロシア・北朝鮮・イラン)・SIR の該当がある記事だけ"
      >
        関連性ありのみ
      </FacetToggle>
      <FacetToggle
        on={state.includeStrategic}
        disabled={!state.minSeverity}
        onClick={() => onChange({ ...state, includeStrategic: !state.includeStrategic })}
        title="深刻さを絞り込み中、政策・地政学 (注視国が主体) の記事も合わせて含める"
      >
        政策・地政学を含める
      </FacetToggle>
    </>
  );
}

/** 影響ベンダ/製品の自由入力 + datalist 補完 (NVD cache 由来)。 */
export function VendorInput({
  raw,
  onChange,
  applied,
  options,
  listId,
}: {
  raw: string;
  onChange: (v: string) => void;
  applied: string;
  options: string[];
  listId: string;
}) {
  return (
    <>
      <input
        list={listId}
        value={raw}
        onChange={(e) => onChange(e.target.value)}
        placeholder="影響 ベンダ/製品…"
        title="指定したベンダ/製品の脆弱性に言及する記事を絞り込む"
        className={`h-8 px-2.5 bg-surface-2 border rounded-md text-sm w-[130px] placeholder:text-fg-subtle focus:outline-none focus:border-accent ${
          applied ? "border-accent text-accent-hover" : "border-border-subtle text-fg"
        }`}
      />
      <datalist id={listId}>
        {options.map((v) => <option key={v} value={v} />)}
      </datalist>
    </>
  );
}


/** ダッシュボードの部品のタイトルに添える、深刻さ・関連性・政策地政学の短い印 (2026-10-04)。 */
export function severityTitleTags(s: SeverityFacetState): string[] {
  const tags: string[] = [];
  if (s.minSeverity === "S3") tags.push("重大");
  else if (s.minSeverity === "S2") tags.push("注意以上");
  else if (s.minSeverity === "S1") tags.push("参考以上");
  if (s.relevantOnly) tags.push("関連性あり");
  if (s.minSeverity && s.includeStrategic) tags.push("政策・地政学を含む");
  return tags;
}

/** 日本との関係の絞り込みをタイトルに添える印。選択肢の表示名 (JP_OPTS) と同じ言い方にする。 */
export function jpTitleTag(jp: string): string {
  if (jp === "targeted_affected") return "日本が標的・被害";
  if (jp === "mentioned") return "日本に触れるもの";
  return "";
}

/** カテゴリの絞り込みをタイトルの見出し語に変換する (記事フィード・事象ニュース共有)。
 *  合成カテゴリ (vuln/threat/incident_breach) は専用の言い方、個別カテゴリは
 *  backend 配信 vocab (categoryLabelMap) のラベル、未指定は呼び手が渡す既定語。 */
export function categoryTitleBase(
  category: string,
  categoryLabelMap: Record<string, string>,
  defaultBase: string,
): string {
  if (!category) return defaultBase;
  if (category === "vuln") return "脆弱性情報";
  if (category === "threat") return "脅威情報";
  if (category === "incident_breach") return "侵害・インシデント";
  if (category === "geopolitical") return "地政情勢";
  if (category === "research") return "研究ウォッチ";
  return label(categoryLabelMap, category) || category;
}

/** ダッシュボード widget の見出しを組み立てる (記事フィード・事象ニュース共有)。
 *  feed 指定時はそのサイト名を見出しとして返し (他の絞り込みより優先)、それ以外は
 *  category から導いた見出し語に 深刻さ/関連性/政策地政学・日本との関係・チャンネル名を
 *  括弧書きで添える。 */
export function buildFacetedTitle({
  category, feed, channelLabel, severity, jp, categoryLabelMap, defaultBase,
}: {
  category: string; feed: string; channelLabel: string; severity: SeverityFacetState; jp: string;
  categoryLabelMap: Record<string, string>; defaultBase: string;
}): string {
  if (feed) return feed;
  const base = categoryTitleBase(category, categoryLabelMap, defaultBase);
  const tags: string[] = [...severityTitleTags(severity)];
  const jpTag = jpTitleTag(jp);
  if (jpTag) tags.push(jpTag);
  if (channelLabel) tags.push(channelLabel);
  return tags.length > 0 ? `${base} (${tags.join(" · ")})` : base;
}
