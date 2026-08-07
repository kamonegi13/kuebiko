// 国家情勢 + 脅威マップ の dashboard widget。
// 脅威マップページの 3 要素 (地図 / 被害国ランキング / 日次推移) をそのまま widget 化する:
// - SituationWidget  : 自国+敵対の 攻撃者/標的/地政学 を凝縮 (国家情勢タブの要点)。旧 pmesii_spike を置換。
// - MiniMapWidget    : 実 Leaflet 地図を小型埋め込み (ページと同一データ・同一描画)。
// - GeoRankingWidget : 被害国ランキング (ページ右パネルと同じ行部品 = セクター構成 + 信頼度ドット)。
// - GeoTrendWidget   : 日次件数推移 (ページ下部と同じ TrendChart、国別/セクター別・折れ線/積み上げ)。
// いずれもドリルで本機能 (/app/intel/pmesii, /app/map) へ。

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../../api/client";
import { fetchCyberMap, fetchTrend } from "../../../api/geo";
import { LeafletThreatMap } from "../../../components/geo/LeafletThreatMap";
import { TrendChart } from "../../../components/geo/TrendChart";
import { SectorMiniBar, ConfidenceDot, LedgerTag } from "../../../components/geo/RankingParts";
import { useMapKnobs } from "../../../components/geo/mapKnobs";
import { WidgetCard, Loading, Empty, WidgetError, cfgStr, type WidgetProps } from "../shared";
import { useWidgetWindow } from "../overviewWindow";

const SITU = "/app/intel/pmesii";
const MAP = "/app/map";

// ── 国家情勢 (自国 + 敵対 を凝縮、各国 攻撃者/標的/地政学) ──
export function SituationWidget({ config }: WidgetProps = {}) {
  const days = useWidgetWindow(config); // 既定=共有窓に連動、⚙ で個別固定可
  const { data, isError } = useQuery({
    queryKey: ["dash-situation", days],
    queryFn: () => api.situationNations(String(days)),
    refetchInterval: 5 * 60_000,
  });
  const nations = data?.nations ?? [];
  const rows = [
    ...nations.filter((n) => n.role === "home"),
    ...nations.filter((n) => n.role === "adversary"),
  ];
  return (
    <WidgetCard title="国家情勢" href={SITU} linkLabel="情勢 →">
      {isError ? (
        <WidgetError />
      ) : !data ? (
        <Loading />
      ) : rows.length === 0 ? (
        <Empty>情勢データなし。</Empty>
      ) : (
        <div className="space-y-0.5">
          <div className="flex items-center gap-2 px-1 pb-0.5 text-[10px] text-fg-subtle">
            <span className="flex-1">国</span>
            <span className="w-9 text-right text-accent">攻撃者</span>
            <span className="w-9 text-right text-cyan-400">標的</span>
            <span className="w-9 text-right text-warning">地政</span>
          </div>
          {rows.map((n) => (
            <a
              key={n.iso}
              href={`${SITU}#nation=${n.iso}`}
              className="flex items-center gap-2 rounded px-1 py-0.5 text-sm hover:bg-surface-2"
            >
              <span className="min-w-0 flex-1 truncate text-fg">
                {n.role === "home" && (
                  <span className="mr-1 rounded bg-accent-soft px-1 text-[9px] text-accent-hover">自国</span>
                )}
                {n.label}
              </span>
              <span className="w-9 text-right tnum text-accent">{n.cyber}</span>
              <span className="w-9 text-right tnum text-cyan-400">{n.cyber_target}</span>
              <span className="w-9 text-right tnum text-warning">{n.geopol}</span>
            </a>
          ))}
        </div>
      )}
    </WidgetCard>
  );
}

// ── ミニ Leaflet 地図 (任意配置、実地図) ──
export function MiniMapWidget({ config }: WidgetProps = {}) {
  const days = useWidgetWindow(config); // 窓のみ共有窓連動 (⚙ で固定可)、他はページ設定を鏡写し
  const k = useMapKnobs();
  const { data, isError } = useQuery({
    queryKey: ["dash-geo-map", days, k.threatClass, k.sourceStatus, k.minImportance, k.pmesii, k.timeBasis],
    queryFn: () => fetchCyberMap(days, k.threatClass, k.sourceStatus, k.minImportance, k.pmesii, k.timeBasis),
    refetchInterval: 10 * 60_000,
  });
  return (
    <WidgetCard title="脅威マップ" href={`${MAP}?days=${days}`} linkLabel="脅威マップ →">
      {isError ? (
        <WidgetError />
      ) : !data ? (
        <Loading />
      ) : (
        // isolate=stacking context で Leaflet の高 z-index を widget 内へ封じ込め (ツールボックスの
        // 上に地図が飛び出す問題を解消)。h-full=widget の割当高さに追従 (min-h で auto 時の潰れ防止)。
        // 地図本体は absolute inset-0 で親の実寸に張る — min-height は子の height:100% の
        // 解決基準にならない (percentage trap) ため、モバイル (自然高) で leaflet が 0px に
        // 潰れていた不具合の恒久修正 (2026-07-15)。
        // view は専用キーで保存 (full map と独立に good な view を保持)。
        <div className="relative isolate h-full min-h-[240px] overflow-hidden rounded-md border border-border-subtle">
          <div className="absolute inset-0">
          <LeafletThreatMap
            data={data}
            selectedSector={k.selectedSector}
            selectedIntent={k.selectedIntent}
            colorBy={k.colorBy}
            layer={k.layer}
            persistViewKey="cti.map.mini.view"
            onCountryClick={() => {
              window.location.href = `${MAP}?days=${days}`;
            }}
          />
          </div>
        </div>
      )}
    </WidgetCard>
  );
}

// ── 被害国ランキング (脅威マップページの右パネルと同じ行部品) ──
// セクター構成ミニバー + カバレッジ信頼度ドット + 台帳のみタグ。行クリックで脅威マップへ。
export function GeoRankingWidget({ config }: WidgetProps = {}) {
  const days = useWidgetWindow(config); // 窓のみ共有窓連動 (⚙ で固定可)、他はページ設定を鏡写し
  const k = useMapKnobs();
  const { data, isError } = useQuery({
    queryKey: ["dash-geo-map", days, k.threatClass, k.sourceStatus, k.minImportance, k.pmesii, k.timeBasis],
    queryFn: () => fetchCyberMap(days, k.threatClass, k.sourceStatus, k.minImportance, k.pmesii, k.timeBasis),
    refetchInterval: 10 * 60_000,
  });
  const nodes = data ? [...data.nodes].sort((a, b) => b.count - a.count) : [];
  return (
    <WidgetCard title={k.minImportance === "all" ? "被害国ランキング" : "重要被害国ランキング"}
      href={`${MAP}?days=${days}`} linkLabel="脅威マップ →">
      {isError ? (
        <WidgetError />
      ) : !data ? (
        <Loading />
      ) : nodes.length === 0 ? (
        <Empty>被害データなし。</Empty>
      ) : (
        <ul className="divide-y divide-border-subtle">
          {nodes.map((n) => (
            <li key={n.iso}>
              <a href={`${MAP}?days=${days}`} className="flex w-full items-center gap-2 py-1.5 text-left text-sm hover:bg-surface-2">
                <SectorMiniBar sectors={n.sectors} total={n.count} />
                <ConfidenceDot
                  confidence={n.confidence}
                  sourceCount={n.source_count}
                  share={n.top_source_share}
                  topSource={n.top_source}
                />
                <span className="min-w-0 truncate text-fg">{n.label}</span>
                {n.posted_count === 0 && n.collected_count > 0 && <LedgerTag />}
                <span className="ml-auto tnum text-fg-muted">{n.count}</span>
              </a>
            </li>
          ))}
        </ul>
      )}
    </WidgetCard>
  );
}

// ── 日次件数推移 (脅威マップページ下部と同じ TrendChart) ──
// config: 系列 (国別/セクター別) と表示 (折れ線/積み上げ)。凡例クリックの系列表示切替は
// widget ローカル (ページ側は localStorage 永続だがここは一時)。24h 窓は推移にならないので 7d 下限。
export function GeoTrendWidget({ config }: WidgetProps = {}) {
  const days = useWidgetWindow(config);
  const trendDays = days === 1 ? 7 : days;
  const group = cfgStr(config, "group", "country") as "country" | "sector";
  const mode = cfgStr(config, "mode", "line") as "line" | "stacked";
  const [hiddenKeys, setHiddenKeys] = useState<string[]>([]);
  const k = useMapKnobs();
  // ページと同じ: 地政学レイヤー選択時は地政学イベントの推移、それ以外はサイバー被害。
  const domain = k.layer === "geopolitical" ? ("geopolitical" as const) : ("cyber" as const);
  const { data, isError } = useQuery({
    queryKey: ["dash-geo-trend", trendDays, group, domain, k.threatClass, k.sourceStatus, k.minImportance, k.pmesii],
    queryFn: () => fetchTrend(trendDays, k.threatClass, group, domain, k.sourceStatus, undefined, k.minImportance, k.pmesii),
    refetchInterval: 10 * 60_000,
  });
  return (
    <WidgetCard title="日次推移 (被害報道)" href={`${MAP}?days=${days}`} linkLabel="脅威マップ →">
      {isError ? (
        <WidgetError />
      ) : !data ? (
        <Loading />
      ) : data.buckets.length === 0 ? (
        <Empty>推移データなし。</Empty>
      ) : (
        <TrendChart
          data={data}
          mode={mode}
          hiddenKeys={hiddenKeys}
          onToggleSeries={(key) =>
            setHiddenKeys((prev) => (prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key]))
          }
        />
      )}
    </WidgetCard>
  );
}
