// 脅威マップページ (2 ペイン: 左=地図 / 右=記事・詳細パネル)。ビューポート固定でページ
// スクロール不要 → 地図ホイールズームと競合しない。プロット click で右パネルに記事を出し、
// 記事 click でパネル内インライン詳細。LOD: 縮小=国件数バブル / 拡大=組織・都市の精密点。

import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { LeafletThreatMap } from "../components/geo/LeafletThreatMap";
import type { ColorBy, MapFocus, MapLayer } from "../components/geo/LeafletThreatMap";
import { sectorColor, SECTOR_UNKNOWN_KEYS } from "../components/geo/sectorColors";
import { sectorLabel } from "../components/geo/sectorColors";
import { countryLabel } from "../utils/countryLabels";
import { SectorMiniBar, ConfidenceDot, LedgerTag } from "../components/geo/RankingParts";
import { useMapKnobs } from "../components/geo/mapKnobs";
import {
  fetchCountryArticles,
  fetchCyberMap,
  fetchSubCountryPoints,
  fetchTrend,
  type CountryArticle,
  type CyberMapResponse,
  type GeoDomain,
  type GeoEventNode,
  type GeoNode,
  type MinImportance,
  type PmesiiAxis,
  type SourceStatus,
  type SubCountryPoint,
  type ThreatClass,
} from "../api/geo";
import { TrendChart } from "../components/geo/TrendChart";
import { fetchArticleDetail } from "../api/article";
import { formatJst } from "../utils/date";
import { intentHex, intentLabel } from "../utils/diamond";
import { usePersistedState } from "../utils/usePersistedState";
import { useVocab, vocabLabel } from "../hooks/useVocab";

const WINDOWS: { label: string; days: number }[] = [
  { label: "24時間", days: 1 },
  { label: "7日", days: 7 },
  { label: "30日", days: 30 },
  { label: "90日", days: 90 },
  { label: "全期間", days: 0 },
];
const LAYERS: { label: string; value: MapLayer }[] = [
  { label: "サイバー", value: "cyber" },
  { label: "地政学", value: "geopolitical" },
  { label: "両方", value: "both" },
];
// 塗りの色分け次元: 動機=両レイヤー統一(一貫) / セクター=cyber のみ(地政学は動機のまま)。
const COLOR_BYS: { label: string; value: ColorBy }[] = [
  { label: "動機", value: "intent" },
  { label: "セクター", value: "sector" },
];
const THREAT_CLASSES: { label: string; value: ThreatClass }[] = [
  { label: "両方", value: "all" },
  { label: "ランサム", value: "ransomware" },
  { label: "その他", value: "other" },
];
// 収集経路: すべて / 報道(triage済ニュース) / 台帳(ransomware.live 等の生レジストリ・未投稿)。
// 報道と台帳の差分が「収集の盲点」を示す (報道に出ない国が台帳に出る)。
const SOURCE_STATUSES: { label: string; value: SourceStatus }[] = [
  { label: "すべて", value: "all" },
  { label: "報道", value: "posted" },
  { label: "台帳", value: "collected" },
];
// 意義の地図 (A): バブルを生件数でなく重要度しきい値内の件数に。importance は PIR 駆動 triage の
// 出力で日本/セクター/PIR を内包する holistic な significance (別スコアは作らない)。
const IMPORTANCE_OPTS: { label: string; value: MinImportance }[] = [
  { label: "全件", value: "all" },
  { label: "中+", value: "medium_up" },
  { label: "高のみ", value: "high" },
];
// PMESII 直交フィルタ (地政学レイヤー): 色=動機(intent) と直交する「どの領域に作用するか」。
// 地政学で主要な 4 軸 + 全領域 (情報/環境/技術軸は地政学では稀なので UI からは省く)。
const PMESII_OPTS: { label: string; value: PmesiiAxis }[] = [
  { label: "全領域", value: "all" },
  { label: "政治", value: "p" },
  { label: "軍事", value: "m" },
  { label: "経済", value: "e" },
  { label: "社会", value: "s" },
];
const IMPORTANCE_TONE: Record<string, string> = {
  high: "bg-critical",
  medium: "bg-warning",
  low: "bg-fg-faint",
};

type Bounds = [[number, number], [number, number]];
// 地域プリセット。先頭「全体」は filter 解除 (世界へ flyTo) を兼ねる。それ以外を選ぶと
// その bounds で地図減光・右パネル・推移・セクターを実フィルタする (P2 地域ブラッシング)。
const REGIONS: { label: string; bounds: Bounds }[] = [
  { label: "全体", bounds: [[-50, -165], [70, 178]] },
  { label: "日本周辺", bounds: [[30, 128], [46, 146]] },
  { label: "東アジア", bounds: [[18, 100], [46, 148]] },
  { label: "欧州", bounds: [[35, -11], [60, 40]] },
  { label: "中東", bounds: [[12, 34], [40, 60]] },
  { label: "北米", bounds: [[15, -130], [55, -60]] },
];

// 緯度経度が地域 bounds 内かを判定 (地図 node の lat/lon で client-side に絞り込む)。
function inBounds(lat: number, lon: number, b: Bounds): boolean {
  return lat >= b[0][0] && lat <= b[1][0] && lon >= b[0][1] && lon <= b[1][1];
}

type Lens = { kind: "none" } | { kind: "actor"; actorId: string };
type Panel =
  | { mode: "idle" }
  | { mode: "country"; iso: string; domain: GeoDomain }
  | { mode: "article"; id: string; back: Panel };

interface SectorTotal {
  sector: string;
  label: string;
  count: number;
}

function sectorTotals(nodes: GeoNode[]): SectorTotal[] {
  const totals = new Map<string, SectorTotal>();
  for (const n of nodes) {
    for (const s of n.sectors) {
      if (SECTOR_UNKNOWN_KEYS.has(s.sector)) continue;
      const cur = totals.get(s.sector);
      if (cur) cur.count += s.count;
      else totals.set(s.sector, { sector: s.sector, label: s.label, count: s.count });
    }
  }
  return [...totals.values()].sort((a, b) => b.count - a.count);
}

interface IntentTotal {
  intent: string;
  count: number;
}

// 表示中マーカ (layer に応じた cyber nodes + geo events) の動機別合計。動機ファセット凡例に使う
// (セクター凡例と同形: 件数つきチップ + 全体)。
function intentTotals(nodes: GeoNode[], events: GeoEventNode[], layer: MapLayer): IntentTotal[] {
  const totals = new Map<string, number>();
  const add = (arr: { intent: string; count: number }[]): void => {
    for (const i of arr) totals.set(i.intent, (totals.get(i.intent) ?? 0) + i.count);
  };
  if (layer !== "geopolitical") for (const n of nodes) add(n.intents);
  if (layer !== "cyber") for (const g of events) add(g.intents);
  return [...totals.entries()]
    .map(([intent, count]) => ({ intent, count }))
    .sort((a, b) => b.count - a.count);
}

function deriveLens(
  data: CyberMapResponse | undefined,
  lens: Lens,
): { focus: MapFocus | null; highlightIso: string | null } {
  if (!data || lens.kind === "none") return { focus: null, highlightIso: null };
  const a = data.actors.find((x) => x.actor_id === lens.actorId);
  if (!a) return { focus: null, highlightIso: null };
  // アクター中心ビュー: そのアクターの被害国を強調 (弧は廃止、被害国 iso 集合のみ)。
  const countries = new Set<string>(a.victims.map((v) => v.iso));
  return { focus: { countries }, highlightIso: a.nation };
}

export function MapPage() {
  // 都市・地域ピンの精度ラベル (生 enum 直接表示禁止 — UI 表示文言の規約)。
  const geoPrecisionLabel = useVocab("geo_precision");
  // フィルタ選択は localStorage 永続 (画面更新後も保持)。リセットボタンで既定に戻す。
  const [days, setDays] = usePersistedState("cti.map.days", 30);
  // ダッシュボードの geo widget からのドリルは ?days=N で窓を引き継ぐ
  // (widget で見ていた窓とページの窓を一致させ、「クリックしたら見え方が変わる」を防ぐ)。
  useEffect(() => {
    const q = new URLSearchParams(window.location.search).get("days");
    if (q !== null && ["0", "1", "7", "30", "90"].includes(q)) setDays(Number(q));
    // 初回マウント時のみ (setDays は usePersistedState の安定 setter)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  // 表示フィルタ (layer/sector/intent/threat/sourceStatus/minImportance/pmesii/colorBy/timeBasis)
  // は components/geo/mapKnobs.ts に一元定義 (dashboard の geo widget が同じ見え方を鏡写しする)。
  const {
    layer, setLayer,
    selectedSector, setSelectedSector,
    selectedIntent, setSelectedIntent,
    threatClass, setThreatClass,
    sourceStatus, setSourceStatus,
    minImportance, setMinImportance,
    pmesii, setPmesii,
    colorBy, setColorBy,
    timeBasis, setTimeBasis,
    subCountryPins, setSubCountryPins,
  } = useMapKnobs();
  // 日本中心 (C): JP 強調 + 起動時に東アジアへ flyTo + 右パネルに日本カード。
  const [japanFocus, setJapanFocus] = usePersistedState("cti.map.japanFocus", false);
  const [trendGroup, setTrendGroup] = usePersistedState<"country" | "sector">(
    "cti.map.trendGroup",
    "country",
  );
  const [trendMode, setTrendMode] = usePersistedState<"line" | "stacked">(
    "cti.map.trendMode",
    "line",
  );
  // 推移グラフ凡例で非表示にした系列 (国/セクター) も永続化 (どれを表示するかを保持)。
  const [trendHidden, setTrendHidden] = usePersistedState<string[]>("cti.map.trendHidden", []);
  const [trendSmooth, setTrendSmooth] = usePersistedState("cti.map.trendSmooth", false);
  const toggleTrendSeries = (key: string) =>
    setTrendHidden((prev) => (prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key]));
  // 地域フィルタ (永続): どのプリセットで絞っているか。null=全体 (絞らない)。
  const [regionLabel, setRegionLabel] = usePersistedState<string | null>("cti.map.region", null);
  // 一過性 (永続しない): drill/lens/flyTo トリガ。
  const [lens, setLens] = useState<Lens>({ kind: "none" });
  const [panel, setPanel] = useState<Panel>({ mode: "idle" });
  const [region, setRegion] = useState<Bounds | null>(null);

  const resetFilters = () => {
    setDays(30);
    setLayer("cyber");
    setSelectedSector(null);
    setSelectedIntent(null);
    setThreatClass("all");
    setSourceStatus("all");
    setMinImportance("medium_up");
    setPmesii("all");
    setColorBy("intent");
    setJapanFocus(false);
    setTrendGroup("country");
    setTrendMode("line");
    setTrendHidden([]);
    setTrendSmooth(false);
    setLens({ kind: "none" });
    setRegion(null);
    setRegionLabel(null);
    setTimeBasis("report");
    setSubCountryPins(true);
  };

  // 台帳(ransomware.live)はすべてランサム被害 → 脅威種別「その他」と交差すると構造的に空
  // (バグに見える)。台帳選択時は脅威種別を all に固定し (toggle も無効化)、空集合を防ぐ。
  const effThreatClass: ThreatClass = sourceStatus === "collected" ? "all" : threatClass;
  // PMESII は地政学レイヤー (geopolitical / both の地政学ダイヤ) のみに効く。cyber 専用表示では無効化。
  const effPmesii: PmesiiAxis = layer === "cyber" ? "all" : pmesii;

  const { data, error } = useQuery({
    queryKey: ["cyber-map", days, effThreatClass, sourceStatus, minImportance, effPmesii, timeBasis],
    queryFn: () =>
      fetchCyberMap(days, effThreatClass, sourceStatus, minImportance, effPmesii, timeBasis),
  });

  // 地域ブラッシング (P2): 選択地域 bounds 内の国 iso を node/geo_events の lat/lon から決める。
  // この集合で地図減光・右パネル・推移・セクター凡例を一括フィルタする (null=全体)。
  const regionBounds = useMemo(
    () => REGIONS.find((r) => r.label === regionLabel && r.label !== "全体")?.bounds ?? null,
    [regionLabel],
  );
  const regionIsos = useMemo<Set<string> | null>(() => {
    if (!regionBounds || !data) return null;
    const s = new Set<string>();
    for (const n of data.nodes) if (inBounds(n.lat, n.lon, regionBounds)) s.add(n.iso);
    for (const g of data.geo_events) if (inBounds(g.lat, g.lon, regionBounds)) s.add(g.iso);
    return s;
  }, [regionBounds, data]);
  const regionIsoList = useMemo(() => (regionIsos ? [...regionIsos] : undefined), [regionIsos]);

  // 地図下の日次件数推移 (地図の window/threat_class/レイヤー/収集経路/地域に連動)。24h 窓は推移にならないので 7d 下限。
  const trendDays = days === 1 ? 7 : days;
  // レイヤーが地政学なら地政学イベントの推移、それ以外 (cyber/both) はサイバー被害の推移。
  const trendDomain: GeoDomain = layer === "geopolitical" ? "geopolitical" : "cyber";
  const trend = useQuery({
    queryKey: [
      "geo-trend",
      trendDays,
      effThreatClass,
      trendGroup,
      trendDomain,
      sourceStatus,
      regionLabel,
      minImportance,
      effPmesii,
    ],
    queryFn: () =>
      fetchTrend(
        trendDays,
        effThreatClass,
        trendGroup,
        trendDomain,
        sourceStatus,
        regionIsoList,
        minImportance,
        effPmesii,
      ),
  });

  // 都市・地域ピン (Phase 1b 後半): victim_city を CITY/ADMIN1 tier で解決した精密点。
  // cyber バブルと同じフィルタ集合 (threat_class/source_status/min_importance/time_basis)。
  // トグル off、または地政学のみ表示中 (victim_city と無関係) は fetch しない。
  const subCountryQuery = useQuery({
    queryKey: ["geo-sub-country-points", days, effThreatClass, sourceStatus, minImportance, timeBasis],
    queryFn: () => fetchSubCountryPoints(days, effThreatClass, sourceStatus, minImportance, timeBasis),
    enabled: subCountryPins && layer !== "geopolitical",
  });
  // 地域フィルタ適用後 (bounds 内のみ) のピン。地域プリセットを選ぶと地図・右パネル・推移が
  // 連動して絞られるのと揃え、地域外のピンだけ地図に残る違和感を防ぐ。
  const visibleSubCountryPoints = useMemo<SubCountryPoint[]>(() => {
    if (!subCountryPins || !subCountryQuery.data) return [];
    const points = subCountryQuery.data.points;
    return regionBounds ? points.filter((p) => inBounds(p.lat, p.lon, regionBounds)) : points;
  }, [subCountryPins, subCountryQuery.data, regionBounds]);

  // 地域フィルタ適用後の表示 node / geo_events (凡例の合計に使う)。
  const visibleNodes = useMemo<GeoNode[]>(() => {
    if (!data) return [];
    return regionIsos ? data.nodes.filter((n) => regionIsos.has(n.iso)) : data.nodes;
  }, [data, regionIsos]);
  const visibleEvents = useMemo<GeoEventNode[]>(() => {
    if (!data) return [];
    return regionIsos ? data.geo_events.filter((g) => regionIsos.has(g.iso)) : data.geo_events;
  }, [data, regionIsos]);
  const sectors = useMemo(() => sectorTotals(visibleNodes), [visibleNodes]);
  const intents = useMemo(
    () => intentTotals(visibleNodes, visibleEvents, layer),
    [visibleNodes, visibleEvents, layer],
  );
  // 地域フィルタ時の絞り込み後カウント (操作バー右端の表示用)。
  const regionTotals = useMemo(
    () =>
      regionIsos
        ? { events: visibleNodes.reduce((s, n) => s + n.count, 0), countries: visibleNodes.length }
        : null,
    [regionIsos, visibleNodes],
  );
  const { focus, highlightIso } = useMemo(() => deriveLens(data, lens), [data, lens]);
  // 地図減光の focus = アクターレンズ ∩ 地域 (両方あれば積、片方ならそれ、無ければ null)。
  const mapFocus = useMemo<MapFocus | null>(() => {
    const lensSet = focus?.countries ?? null;
    if (!lensSet && !regionIsos) return null;
    if (lensSet && regionIsos)
      return { countries: new Set([...lensSet].filter((i) => regionIsos.has(i))) };
    return { countries: new Set(lensSet ?? (regionIsos as Set<string>)) };
  }, [focus, regionIsos]);
  const lensActive = lens.kind !== "none";
  // 強調 (リング): ドリル国 > アクターレンズの拠点国 > 日本中心の JP。
  const highlight =
    panel.mode === "country" ? panel.iso : (highlightIso ?? (japanFocus ? "JP" : null));
  // 日本中心トグル: ON 時に東アジアへ flyTo (JP 強調と日本カードは state で連動)。
  const toggleJapanFocus = () => {
    setJapanFocus((v) => {
      if (!v) {
        const ea = REGIONS.find((r) => r.label === "東アジア");
        if (ea) setRegion([[...ea.bounds[0]], [...ea.bounds[1]]] as Bounds);
      }
      return !v;
    });
  };

  return (
    // mobile=通常スクロール (地図は固定 60vh で潰れない) / md+=ビューポート固定 (1画面に収める)
    <div className="flex flex-col bg-bg md:h-[calc(100vh-3rem)]">
      {/* 操作バー (コンパクト 1 段) */}
      <div className="shrink-0 flex flex-wrap items-center gap-2 border-b border-border-subtle px-3 py-2">
        <Seg
          items={WINDOWS.map((w) => ({ label: w.label, active: days === w.days, on: () => setDays(w.days) }))}
        />
        <Seg
          items={LAYERS.map((l) => ({
            label: l.label,
            active: layer === l.value,
            on: () => setLayer(l.value),
          }))}
        />
        {/* 色分け: 動機=両レイヤー統一(一貫した1凡例) / セクター=cyber をセクター比に
            (地政学はセクターが無いので動機のまま)。形=レイヤー / 大きさ=件数 は不変。 */}
        <Seg
          title="塗りの意味を切替。動機=両レイヤー一貫 / セクター=サイバーのみセクター比"
          items={COLOR_BYS.map((c) => ({
            label: c.label,
            active: colorBy === c.value,
            on: () => setColorBy(c.value),
          }))}
        />
        {/* 脅威種別: ランサム / その他 で被害を絞る (category 直交フラグ is_ransomware)。
            地政学レイヤーはサイバー被害でないので無効化 (ランサム概念が適用されない)。 */}
        <Seg
          disabled={layer === "geopolitical" || sourceStatus === "collected"}
          title={
            layer === "geopolitical"
              ? "地政学レイヤーでは脅威種別は適用されません"
              : sourceStatus === "collected"
                ? "台帳(ransomware.live)はすべてランサム被害のため脅威種別は適用されません"
                : undefined
          }
          items={THREAT_CLASSES.map((t) => ({
            label: t.label,
            active: effThreatClass === t.value,
            on: () => setThreatClass(t.value),
          }))}
        />
        {/* 収集経路: 報道(triage済) / 台帳(ransomware.live 生レジストリ)。両者の差分が収集の盲点を示す。
            地政学レイヤーは全て posted なので無効化。 */}
        <Seg
          disabled={layer === "geopolitical"}
          title={
            layer === "geopolitical"
              ? "地政学レイヤーは収集経路の区別がありません"
              : "報道=選別済みニュース / 台帳=ransomware.live 等の未加工リスト"
          }
          items={SOURCE_STATUSES.map((s) => ({
            label: s.label,
            active: sourceStatus === s.value,
            on: () => setSourceStatus(s.value),
          }))}
        />
        {/* 時間軸: 報道時刻(collection が今出している・全件) / 発生時刻(実際に起きた・dated のみ・
            過去事象に偏る)。両者の差分が「再報道 vs 新規」を示す。 */}
        <Seg
          title="報道時刻=記事が出た時刻(全件) / 発生時刻=事象が実際に起きた時刻(発生日が判明した記事のみ・過去に偏る・1年の期間で効く)"
          items={[
            { label: "報道時刻", active: timeBasis === "report", on: () => setTimeBasis("report") },
            { label: "発生時刻", active: timeBasis === "event", on: () => setTimeBasis("event") },
          ]}
        />
        {timeBasis === "event" && data?.time_coverage && (
          <span className="self-center text-[10px] leading-snug text-fg-subtle">
            発生日付き {data.time_coverage.dated}/{data.time_coverage.total}・未抽出は
            <span className="text-warning">非表示(不明≠無し)</span>
          </span>
        )}
        {/* 意義の地図: 重要度しきい値で「対象」を絞る (バブル=しきい値内の件数)。地政学レイヤーは
            別 importance 体系なので cyber のみ適用 → 地政学では無効化。 */}
        <Seg
          disabled={layer === "geopolitical"}
          title={
            layer === "geopolitical"
              ? "地政学レイヤーでは意義しきい値は適用されません"
              : "重要度しきい値で絞る (中+ で低重要度の件数の多さに引きずられない)"
          }
          items={IMPORTANCE_OPTS.map((o) => ({
            label: o.label,
            active: minImportance === o.value,
            on: () => setMinImportance(o.value),
          }))}
        />
        {/* PMESII 直交フィルタ: 地政学イベントを「どの領域に作用するか」で絞る (色=動機 と直交)。
            cyber 専用レイヤーでは無効 (地政学ダイヤが出ないため)。 */}
        <Seg
          disabled={layer === "cyber"}
          title={
            layer === "cyber"
              ? "PMESII フィルタは地政学レイヤー用です (レイヤーを地政学/両方に)"
              : "地政学イベントを領域で絞る (色=動機 とは別の軸。例: 軍事領域の事象のみ)"
          }
          items={PMESII_OPTS.map((o) => ({
            label: o.label,
            active: effPmesii === o.value,
            on: () => setPmesii(o.value),
          }))}
        />
        {/* 日本中心 (C): JP 強調 + 東アジアへ flyTo + 右パネルに日本カード。significance は変えない。 */}
        <button
          onClick={toggleJapanFocus}
          className={`px-2.5 py-1.5 text-sm rounded-md border transition-colors ${
            japanFocus
              ? "border-accent text-accent bg-accent/10"
              : "border-border-default text-fg-muted hover:text-fg"
          }`}
          title="日本を強調し東アジアへ移動 (自地域の重要な脅威を見る)"
        >
          日本中心
        </button>
        {/* 都市・地域ピン (Phase 1b 後半): victim_city を CITY(市区)/ADMIN1(州・県) tier で
            解決した精密点レイヤーの on/off。国バブルとは別レイヤーなので既定 on のまま独立に切替。 */}
        <button
          onClick={() => setSubCountryPins((v) => !v)}
          disabled={layer === "geopolitical"}
          className={`px-2.5 py-1.5 text-sm rounded-md border transition-colors disabled:opacity-40 disabled:cursor-not-allowed ${
            subCountryPins
              ? "border-accent text-accent bg-accent/10"
              : "border-border-default text-fg-muted hover:text-fg"
          }`}
          title={
            layer === "geopolitical"
              ? "都市・地域ピンは被害地(victim_city)由来のためサイバーレイヤーでのみ表示されます"
              : "被害都市/州県が判明した記事を精密点で表示 (● 市区 / ○ 州・県)"
          }
        >
          都市・地域ピン
        </button>
        {data && data.actors.length > 0 && (
          <select
            value={lens.kind === "actor" ? lens.actorId : ""}
            onChange={(e) =>
              setLens(e.target.value ? { kind: "actor", actorId: e.target.value } : { kind: "none" })
            }
            className="px-2 py-1.5 text-sm rounded-md border border-border-default bg-surface-1 text-fg-muted max-w-[200px]"
            title="選択したアクターの被害国を地図上で強調"
          >
            <option value="">アクター中心 (被害国を強調)…</option>
            {data.actors.map((a) => (
              <option key={a.actor_id} value={a.actor_id}>
                {a.label}
                {a.nation_label ? ` (${a.nation_label})` : ""} — {a.total}
              </option>
            ))}
          </select>
        )}
        {lensActive && (
          <button
            onClick={() => setLens({ kind: "none" })}
            className="px-2 py-1.5 text-xs rounded-md border border-border-subtle text-fg-subtle hover:text-fg"
          >
            レンズ解除
          </button>
        )}
        {/* 地域フィルタ: flyTo に加え bounds で全体を絞る (全体=解除)。地域選択で地図減光・
            右パネル・推移・セクターが連動。 */}
        <select
          value={regionLabel ?? ""}
          onChange={(e) => {
            const r = REGIONS.find((x) => x.label === e.target.value);
            if (r) setRegion([[...r.bounds[0]], [...r.bounds[1]]] as Bounds); // flyTo
            setRegionLabel(r && r.label !== "全体" ? r.label : null); // filter (全体=解除)
          }}
          className={`px-2 py-1.5 text-sm rounded-md border bg-surface-1 ${
            regionLabel ? "border-accent text-accent" : "border-border-default text-fg-muted"
          }`}
          title="選択地域で地図・右パネル・推移を絞り込む"
        >
          <option value="">地域フィルタ…</option>
          {REGIONS.map((r) => (
            <option key={r.label} value={r.label}>
              {r.label}
            </option>
          ))}
        </select>
        <button
          onClick={resetFilters}
          className="px-2 py-1.5 text-xs rounded-md border border-border-subtle text-fg-subtle hover:text-fg"
          title="フィルタ選択を既定に戻す"
        >
          リセット
        </button>
        {data && (
          <span className="ml-auto text-xs text-fg-subtle">
            {regionTotals && (
              <span className="mr-2 text-accent">
                {regionLabel} <span className="font-semibold">{regionTotals.events}</span> /{" "}
                {regionTotals.countries}ヶ国 ·{" "}
              </span>
            )}
            サイバー被害 <span className="text-fg font-semibold">{data.totals.placed_events}</span> /{" "}
            {data.totals.countries}ヶ国 · 地図外{" "}
            <span className="text-warning">
              {data.excluded.non_cyber + data.excluded.accidental + data.excluded.unplaced}
            </span>
          </span>
        )}
      </div>

      {/* 常設の正直化ラベル: 地図は世界でなく「我々の収集網が見たもの」。暗域≠安全を明示。 */}
      <div className="shrink-0 border-b border-border-subtle bg-surface-1 px-3 py-1 text-[10px] leading-snug text-fg-subtle">
        地図＝収集網の観測（攻撃分布ではない）。暗い国は「攻撃が少ない」ではなく「我々が見ていない」可能性。
        丸の大きさ＝<span className="text-fg-muted">期間内の相対件数</span>（絶対数・情報源の信頼度は丸の上／
        右パネルで確認）。
        {subCountryPins && layer !== "geopolitical" && (
          <>
            {" "}
            精密点: <span className="text-fg-muted">● {geoPrecisionLabel("city")}</span> /{" "}
            <span className="text-fg-muted">○ {geoPrecisionLabel("admin1")}</span>
            （被害地が判明した記事のみ。国バブルの内数）。
          </>
        )}
      </div>

      {error && (
        <div className="m-3 rounded-md border border-critical/40 bg-critical-soft p-3 text-sm text-critical">
          地図データの取得に失敗しました: {(error as Error).message}
        </div>
      )}

      {/* モバイル=縦stack (地図 上 / パネル 下)、md+=横並び。固定 400px パネルが地図を
          潰してモバイルで地図が消えていた問題を解消。 */}
      <div className="flex flex-col md:min-h-0 md:flex-1 md:flex-row">
        {/* 左(md+)/上(mobile): 地図 + セクター凡例。mobile=固定 60vh / md+=領域いっぱい。 */}
        <div className="flex h-[60vh] min-w-0 flex-col md:h-auto md:min-h-0 md:flex-1">
          {/* isolate: Leaflet の高 z-index (controls/panes z~1000) を stacking context 内に
              封じ込め、mobile の sticky TopBar / fixed BottomTabBar (z-40) を貫いて地図が
              最前面に飛び出す表示崩れを防ぐ (mini-map と同じ対策)。 */}
          <div className="relative isolate min-h-0 flex-1">
            {data && (
              <LeafletThreatMap
                data={data}
                selectedSector={selectedSector}
                selectedIntent={selectedIntent}
                layer={layer}
                colorBy={colorBy}
                focus={mapFocus}
                highlightIso={highlight}
                onCountryClick={(iso, domain) => setPanel({ mode: "country", iso, domain })}
                region={region}
                persistViewKey="cti.map.view"
                subCountryPoints={visibleSubCountryPoints}
              />
            )}
          </div>
          {/* セクター凡例 ＝ cyber のセクター ファセット (色分け「セクター」モードのみ)。 */}
          {colorBy === "sector" && (
            <div className="shrink-0 flex items-center gap-1.5 overflow-x-auto border-t border-border-subtle px-3 py-1.5">
              <span className="text-xs text-fg-subtle shrink-0">セクター:</span>
              <SectorChip active={selectedSector === null} onClick={() => setSelectedSector(null)} label="全体" />
              {sectors.map((s) => (
                <SectorChip
                  key={s.sector}
                  active={selectedSector === s.sector}
                  onClick={() => setSelectedSector((c) => (c === s.sector ? null : s.sector))}
                  label={s.label}
                  count={s.count}
                  color={sectorColor(s.sector)}
                />
              ))}
            </div>
          )}
          {/* 動機 凡例 ＝ 動機 ファセット (色分け「動機」モード)。セクター凡例と同形:
              件数つきチップ + 全体。クリックでその動機を両レイヤーに地理的に絞り込む。 */}
          {colorBy === "intent" && (
            <div className="shrink-0 flex items-center gap-1.5 overflow-x-auto border-t border-border-subtle px-3 py-1.5">
              <span className="text-xs text-fg-subtle shrink-0">動機:</span>
              <SectorChip active={selectedIntent === null} onClick={() => setSelectedIntent(null)} label="全体" />
              {intents.map((it) => (
                <SectorChip
                  key={it.intent}
                  active={selectedIntent === it.intent}
                  onClick={() => setSelectedIntent((c) => (c === it.intent ? null : it.intent))}
                  label={intentLabel(it.intent)}
                  count={it.count}
                  color={intentHex(it.intent)}
                />
              ))}
            </div>
          )}
        </div>

        {/* 右(md+)/下(mobile): 記事・詳細パネル。mobile=全幅で下半分(flex-1)、md+=固定400px */}
        <aside className="w-full border-t border-border-default bg-surface-1 md:w-[400px] md:flex-none md:shrink-0 md:overflow-y-auto md:border-l md:border-t-0">
          <RightPanel
            panel={panel}
            setPanel={setPanel}
            days={days}
            threatClass={effThreatClass}
            sourceStatus={sourceStatus}
            minImportance={minImportance}
            pmesii={effPmesii}
            japanFocus={japanFocus}
            layer={layer}
            data={data}
            regionIsos={regionIsos}
          />
        </aside>
      </div>

      {/* 地図下: 日次件数推移 (地図の window+threat_class に連動、国別/セクター別) */}
      <div className="shrink-0 border-t border-border-default bg-surface-1 px-3 py-1.5">
        <div className="mb-0.5 flex flex-wrap items-center gap-1.5">
          <span className="text-[11px] font-medium text-fg-muted">日次推移</span>
          <Seg
            items={[
              { label: "国別", active: trendGroup === "country", on: () => setTrendGroup("country") },
              { label: "セクター別", active: trendGroup === "sector", on: () => setTrendGroup("sector") },
            ]}
          />
          <Seg
            items={[
              { label: "推移", active: trendMode === "line", on: () => setTrendMode("line") },
              { label: "構成", active: trendMode === "stacked", on: () => setTrendMode("stacked") },
            ]}
          />
          {/* 平滑化 (7日移動平均)。折れ線のみ有効、構成(積み上げ)では非活性。 */}
          <button
            onClick={() => setTrendSmooth((v) => !v)}
            disabled={trendMode !== "line"}
            title={trendMode !== "line" ? "平滑化は折れ線のみ" : "生の推移（主線）に7日移動平均を控えめな点線で重ねる"}
            className={`px-2 py-1.5 text-xs rounded-md border transition-colors disabled:opacity-40 disabled:cursor-not-allowed ${
              trendSmooth && trendMode === "line"
                ? "border-accent text-accent bg-accent/10"
                : "border-border-default text-fg-muted hover:text-fg"
            }`}
          >
            平滑
          </button>
          <span className="text-[10px] text-fg-subtle">
            直近 {trendDays} 日 ·{" "}
            {trendDomain === "geopolitical"
              ? "地政学"
              : effThreatClass === "all"
                ? "全脅威"
                : effThreatClass === "ransomware"
                  ? "ランサム"
                  : "その他"}
          </span>
        </div>
        {trend.data ? (
          <TrendChart
            data={trend.data}
            mode={trendMode}
            smooth={trendSmooth}
            hiddenKeys={trendHidden}
            onToggleSeries={toggleTrendSeries}
          />
        ) : (
          <div style={{ height: 84 }} />
        )}
      </div>
    </div>
  );
}

// ── 小物 ──

function Seg({
  items,
  disabled = false,
  title,
}: {
  items: { label: string; active: boolean; on: () => void }[];
  disabled?: boolean;
  title?: string;
}) {
  return (
    <div
      className={`flex rounded-md border border-border-default overflow-hidden ${disabled ? "opacity-40" : ""}`}
      title={title}
    >
      {items.map((it) => (
        <button
          key={it.label}
          onClick={disabled ? undefined : it.on}
          disabled={disabled}
          className={`px-2.5 py-1.5 text-sm transition-colors ${
            it.active ? "bg-accent/20 text-accent" : "text-fg-muted hover:text-fg hover:bg-surface-2"
          } ${disabled ? "cursor-not-allowed" : ""}`}
        >
          {it.label}
        </button>
      ))}
    </div>
  );
}

function SectorChip({
  active,
  onClick,
  label,
  count,
  color,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  count?: number;
  color?: string;
}) {
  return (
    <button
      onClick={onClick}
      className={`flex shrink-0 items-center gap-1.5 rounded border px-2 py-1 text-xs transition-colors ${
        active ? "border-fg text-fg bg-surface-2" : "border-border-subtle text-fg-muted hover:text-fg"
      }`}
    >
      {color && <span className="inline-block h-2.5 w-2.5 rounded-full" style={{ background: color }} />}
      {label}
      {count != null && <span className="text-fg-subtle">{count}</span>}
    </button>
  );
}

function ArticleRow({ a, onOpen }: { a: CountryArticle; onOpen: () => void }) {
  // セクターは左端の縦カラーバーで示す。バーは行内のインライン要素にし、py 余白で記事ごとに
  // 隙間が空く (li 枠線だと全高で連続して見えるため)。rounded で短いピル状に。
  return (
    <li className="py-1.5" title={a.victim_sector_label || a.victim_sector || "未分類"}>
      <button onClick={onOpen} className="flex w-full items-stretch gap-2.5 text-left">
        <span
          className="w-[3px] shrink-0 self-stretch rounded-full"
          style={{ background: sectorColor(a.victim_sector) }}
        />
        <span className="min-w-0 flex-1">
          <span className="block text-sm text-fg hover:text-accent line-clamp-2">
            {a.title || a.url || a.article_id}
          </span>
          <span className="mt-0.5 flex flex-wrap items-center gap-x-2 text-xs text-fg-subtle">
            {a.status === "collected" && (
              <span
                className="rounded-sm border border-border-subtle px-1 text-[10px] text-fg-subtle"
                title="台帳: ransomware.live 等の未加工リスト由来 (未投稿・地図専用)"
              >
                台帳
              </span>
            )}
            {a.socio_political_intent && a.socio_political_intent !== "unknown" && (
              <span
                className="inline-flex items-center gap-1 rounded-sm px-1 text-[10px]"
                style={{
                  color: intentHex(a.socio_political_intent),
                  border: `1px solid ${intentHex(a.socio_political_intent)}55`,
                }}
                title="動機（統一した意図の分類）"
              >
                {intentLabel(a.socio_political_intent)}
              </span>
            )}
            {a.category && <span>{vocabLabel("category", a.category)}</span>}
            {(a.victim_sector_label || a.victim_sector) && (
              <span>· {a.victim_sector_label || a.victim_sector}</span>
            )}
            {a.created_at && <span>· {formatJst(a.created_at)}</span>}
          </span>
        </span>
      </button>
    </li>
  );
}

function ArticleList({
  articles,
  loading,
  count,
  onOpen,
}: {
  articles: CountryArticle[] | undefined;
  loading: boolean;
  count?: number;
  onOpen: (id: string) => void;
}) {
  if (loading) return <p className="px-3 py-2 text-xs text-fg-subtle">読み込み中…</p>;
  if (!articles || articles.length === 0)
    return <p className="px-3 py-2 text-xs text-fg-subtle">該当する記事がありません。</p>;
  return (
    <ul className="divide-y divide-border-subtle px-3">
      {count != null && <li className="py-1.5 text-xs text-fg-subtle">{count} 件</li>}
      {articles.map((a) => (
        <ArticleRow key={a.article_id} a={a} onOpen={() => onOpen(a.article_id)} />
      ))}
    </ul>
  );
}

function RightPanel({
  panel,
  setPanel,
  days,
  threatClass,
  sourceStatus,
  minImportance,
  pmesii,
  japanFocus,
  layer,
  data,
  regionIsos,
}: {
  panel: Panel;
  setPanel: (p: Panel) => void;
  days: number;
  threatClass: ThreatClass;
  sourceStatus: SourceStatus;
  minImportance: MinImportance;
  pmesii: PmesiiAxis;
  japanFocus: boolean;
  layer: MapLayer;
  data: CyberMapResponse | undefined;
  regionIsos: Set<string> | null;
}) {
  const open = (id: string) => setPanel({ mode: "article", id, back: panel });
  // idle ランキングのドメイン: 地政学レイヤーは地政学イベント、それ以外はサイバー被害。
  const rankDomain: GeoDomain = layer === "geopolitical" ? "geopolitical" : "cyber";

  // 国ドリル (domain は click 元 = panel.domain。地政学ダイヤ→geopolitical / バブル→cyber)。
  const countryDomain = panel.mode === "country" ? panel.domain : "cyber";
  const country = useQuery({
    queryKey: [
      "geo-country",
      panel.mode === "country" ? panel.iso : null,
      days,
      threatClass,
      countryDomain,
      sourceStatus,
      minImportance,
      pmesii,
    ],
    queryFn: () =>
      fetchCountryArticles(
        (panel as { iso: string }).iso,
        days,
        threatClass,
        countryDomain,
        sourceStatus,
        minImportance,
        pmesii,
      ),
    enabled: panel.mode === "country",
  });
  // 記事詳細
  const article = useQuery({
    queryKey: ["article-detail", panel.mode === "article" ? panel.id : null],
    queryFn: () => fetchArticleDetail((panel as { id: string }).id),
    enabled: panel.mode === "article",
  });

  if (panel.mode === "idle") {
    if (rankDomain === "geopolitical") {
      const events = data
        ? [...data.geo_events]
            .filter((g) => !regionIsos || regionIsos.has(g.iso))
            .sort((a, b) => b.count - a.count)
        : [];
      return (
        <div className="p-3">
          <h3 className="m-0 text-sm font-bold text-fg">地政学イベント 国ランキング</h3>
          <p className="mt-0.5 text-xs text-fg-subtle">国をクリックで関連記事一覧。</p>
          <ul className="mt-2 divide-y divide-border-subtle">
            {events.map((g) => (
              <li key={g.iso}>
                <button
                  onClick={() => setPanel({ mode: "country", iso: g.iso, domain: "geopolitical" })}
                  className="flex w-full items-center gap-2 py-1.5 text-left text-sm"
                >
                  <span
                    className="inline-block h-2.5 w-2.5 shrink-0 rotate-45"
                    style={{ background: g.top_intent ? intentHex(g.top_intent) : "#586273" }}
                  />
                  <span className="text-fg">{g.label}</span>
                  {g.top_intent && (
                    <span className="text-[11px] text-fg-subtle">{intentLabel(g.top_intent)}</span>
                  )}
                  <span className="ml-auto text-fg-muted">{g.count}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      );
    }
    const nodes = data
      ? [...data.nodes]
          .filter((n) => !regionIsos || regionIsos.has(n.iso))
          .sort((a, b) => b.count - a.count)
      : [];
    const jp = data?.nodes.find((n) => n.iso === "JP");
    return (
      <div className="p-3">
        <h3 className="m-0 text-sm font-bold text-fg">
          {minImportance === "all" ? "被害国 ランキング" : "重要被害国 ランキング"}
        </h3>
        <p className="mt-0.5 text-xs text-fg-subtle">
          国をクリックで被害報道の一覧。<span className="text-fg-subtle">●</span>＝観測の網羅度。
        </p>
        {/* 日本中心: 日本の重要被害カードを最上部に固定 (自地域の状況に直答)。 */}
        {japanFocus && (
          <button
            onClick={() => setPanel({ mode: "country", iso: "JP", domain: "cyber" })}
            className="mt-2 flex w-full items-center gap-2 rounded-md border border-accent/40 bg-accent/5 px-2.5 py-2 text-left"
          >
            <span className="text-sm font-bold text-fg">日本</span>
            <span className="text-xs text-fg-subtle">
              {minImportance === "all" ? "被害" : "重要被害"}
            </span>
            <span className="ml-auto text-lg font-bold text-accent tnum">{jp?.count ?? 0}</span>
            <span className="text-xs text-fg-subtle">件 →</span>
          </button>
        )}
        <ul className="mt-2 divide-y divide-border-subtle">
          {nodes.map((n) => (
            <li key={n.iso}>
              <button
                onClick={() => setPanel({ mode: "country", iso: n.iso, domain: "cyber" })}
                className="flex w-full items-center gap-2 py-1.5 text-left text-sm"
              >
                <SectorMiniBar sectors={n.sectors} total={n.count} />
                <ConfidenceDot
                  confidence={n.confidence}
                  sourceCount={n.source_count}
                  share={n.top_source_share}
                  topSource={n.top_source}
                />
                <span className="text-fg">{n.label}</span>
                {n.posted_count === 0 && n.collected_count > 0 && <LedgerTag />}
                <span className="ml-auto text-fg-muted">{n.count}</span>
              </button>
            </li>
          ))}
        </ul>
      </div>
    );
  }

  if (panel.mode === "country") {
    const geo = panel.domain === "geopolitical";
    const total = geo
      ? (data?.geo_events.find((g) => g.iso === panel.iso)?.count ?? country.data?.count ?? 0)
      : (data?.nodes.find((n) => n.iso === panel.iso)?.count ?? country.data?.count ?? 0);
    return (
      <div>
        <PanelHeader onBack={() => setPanel({ mode: "idle" })} title={country.data?.label ?? panel.iso}>
          {geo ? `地政学イベント ${total} 件` : `被害報道 ${total} 件`}
        </PanelHeader>
        <ArticleList
          articles={country.data?.articles}
          loading={country.isFetching}
          onOpen={open}
        />
      </div>
    );
  }

  // article 詳細
  const a = article.data?.article;
  const actorGroup = article.data?.entities.find((e) => e.type === "actor");
  return (
    <div>
      <PanelHeader onBack={() => setPanel(panel.back)} title={a?.title ?? "記事"} />
      {article.isFetching && <p className="px-3 py-2 text-xs text-fg-subtle">読み込み中…</p>}
      {a && (
        <div className="space-y-2 px-3 pb-4 text-sm">
          <div className="flex flex-wrap gap-x-2 gap-y-1 text-xs text-fg-subtle">
            {a.importance && (
              <span
                className={`rounded px-1.5 py-0.5 text-fg ${IMPORTANCE_TONE[a.importance] ?? "bg-fg-faint"}/20`}
              >
                {vocabLabel("importance", a.importance)}
              </span>
            )}
            {a.category && <span>{vocabLabel("category", a.category)}</span>}
            {a.victim_country && <span>· 被害国 {countryLabel(a.victim_country)}</span>}
            {a.victim_sector && <span>· {sectorLabel(a.victim_sector)}</span>}
            {a.created_at && <span>· {formatJst(a.created_at)}</span>}
          </div>
          {actorGroup && actorGroup.values.length > 0 && (
            <div className="text-xs text-fg-muted">
              アクター: <span className="text-fg">{actorGroup.values.join(", ")}</span>
            </div>
          )}
          {a.summary && <p className="text-fg-muted leading-relaxed">{a.summary}</p>}
          <div className="flex gap-3 pt-1 text-xs">
            <a href={`/app/article/${encodeURIComponent(a.article_id)}`} className="text-accent hover:underline">
              詳細ページ →
            </a>
            {a.url && (
              <a href={a.url} target="_blank" rel="noreferrer" className="text-accent hover:underline">
                元記事 ↗
              </a>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function PanelHeader({
  onBack,
  title,
  children,
}: {
  onBack: () => void;
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className="sticky top-0 z-10 border-b border-border-subtle bg-surface-1 px-3 py-2">
      <button onClick={onBack} className="text-xs text-fg-subtle hover:text-fg">
        ← 戻る
      </button>
      <h3 className="m-0 mt-1 text-sm font-bold text-fg line-clamp-2">{title}</h3>
      {children && <p className="mt-0.5 text-xs text-fg-muted">{children}</p>}
    </div>
  );
}
