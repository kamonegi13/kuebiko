// ニュース検索と事象ニュースが **共有** する絞り込み UI と選択肢。
//
// 2 画面に同じ facet を置くにあたり、カテゴリ・チャンネル・意図のラベル辞書を
// 複製しない (CLAUDE.md §7: ラベルは SSoT を参照)。選択肢はすべて backend 配信の
// 語彙 / live registry / 実データ由来で、ここは組み立てるだけ。

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { pirApi } from "../../api/pir";
import { fetchActorOptions, fetchAffectedVendors, fetchFeedOptions } from "../../api/search";
import { useChannels } from "../channel";
import { useVocabOptions, vocabLabel } from "../../hooks/useVocab";

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
