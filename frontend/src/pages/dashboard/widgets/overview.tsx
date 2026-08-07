// 概観 (overview) 系 widget。
// 設計原則: 裸のカウントでなく「変化(前期比) + その変化を駆動した内訳」。
// ラベルは「報道/観測 件数」と誠実 (攻撃件数ではない)。信頼度構成を併記 (抑制でなく表示)。
// KPI は high バケツのサイズでなく decision-bearing な「要対応」を主役に (発見2)。
// movers は総量正規化したシェア%・pp差で volume 汚染を回避 (発見1)。

import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Newspaper,
  ShieldAlert,
  Flag,
  Bug,
  Crosshair,
  Flame,
  Sparkles,
  Clock,
} from "lucide-react";
import { dashboardApi } from "../../../api/runs";
import { overviewApi, type Mover, type AttentionItem, type OverviewData } from "../../../api/overview";
import { KpiTile, RankBars, type RankRow } from "../../../components/charts";
import { INTEL, WidgetCard, WidgetShell, WidgetError, Loading, Empty, type WidgetProps } from "../shared";
import { useOverviewWindow, useWidgetWindow } from "../overviewWindow";
import { actorHref } from "../../../utils/intelNav";

function useOverview(config?: Record<string, unknown>) {
  const days = useWidgetWindow(config); // config.days 固定が無ければ共有窓に連動
  return useQuery({
    queryKey: ["dash-overview", days],
    queryFn: () => overviewApi.get(days),
    refetchInterval: 2 * 60_000,
  });
}

// movers → RankRow。share=true は「構成比%・前期比pp」で表示し生カウントを suffix に併記。
function moversToRows(ms: Mover[], opts?: { share?: boolean }): RankRow[] {
  if (opts?.share) {
    return ms.map((m) => ({
      label: m.label,
      value: m.share_pct,
      valueLabel: `${m.share_pct}%`,
      delta: m.share_delta_pp,
      deltaSuffix: "pp",
      deltaTitle: "前期比 (ポイント差)",
      suffix: <span className="text-[10px] text-fg-subtle tnum w-7 text-right shrink-0">{m.current}</span>,
    }));
  }
  return ms.map((m) => ({ label: m.label, value: m.current, delta: m.delta }));
}

function asOfLabel(iso: string | undefined): string {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  } catch {
    return "";
  }
}

// ── 集計時刻表示 (対象期間チップは ページヘッダ GlobalWindowSelector へ移設 2026-07-14:
// 削除可能な widget の中に全体制御を置かない) ──
function WindowSelector({ asOf }: { asOf: string }) {
  const days = useOverviewWindow();
  return (
    <div className="flex items-center gap-2 shrink-0 text-[11px] text-fg-subtle">
      <span>直近{days === 1 ? "24h" : `${days}日`}</span>
      {asOf && (
        <span className="inline-flex items-center gap-1" title="集計時刻">
          <Clock className="h-3 w-3" aria-hidden /> {asOf} 時点
        </span>
      )}
    </div>
  );
}

const ATTENTION_ICON: Record<AttentionItem["kind"], ReactNode> = {
  kev: <ShieldAlert className="h-3.5 w-3.5 text-critical shrink-0" aria-hidden />,
  new_actor: <Sparkles className="h-3.5 w-3.5 text-accent shrink-0" aria-hidden />,
  spike: <Flame className="h-3.5 w-3.5 text-warning shrink-0" aria-hidden />,
  japan: <Flag className="h-3.5 w-3.5 text-accent shrink-0" aria-hidden />,
};

// ── 今日の要注意 (triage): briefing と差別化する「今すぐ見るべき」(発見7) ──
// mobile は横折返しでなく縦 1 行ずつ (狭幅で要素が潰れないように)。
function AttentionStrip({ items, mobile }: { items: AttentionItem[]; mobile?: boolean }) {
  if (items.length === 0) return null;
  return (
    <div
      className={`rounded-md border border-warning/30 bg-warning-soft/40 px-3 py-2 ${
        mobile ? "space-y-1.5" : "flex flex-wrap items-center gap-x-3 gap-y-1.5"
      }`}
    >
      <span className={`text-[11px] font-semibold text-warning ${mobile ? "block mb-1" : "shrink-0"}`}>今日の要注意</span>
      {items.map((it, i) => (
        <a
          key={`${it.kind}-${it.label}-${i}`}
          href={it.href}
          target={it.href.startsWith("http") ? "_blank" : undefined}
          rel="noopener noreferrer"
          className={`${mobile ? "flex" : "inline-flex"} items-center gap-1.5 text-[12px] text-fg hover:text-accent`}
          title={it.detail}
        >
          {ATTENTION_ICON[it.kind]}
          <span className="font-medium truncate">{it.label}</span>
          <span className="text-fg-subtle truncate">{it.detail}</span>
        </a>
      ))}
    </div>
  );
}

// ── 観測の足場 (信頼度構成): 抑制でなく表示。KPI 帯に常設昇格 (発見3) ──
export function ConfidenceStrip({ c }: { c: Record<string, number> }) {
  const auth = (c.official ?? 0) + (c.research ?? 0);
  const news = c.news ?? 0;
  const social = c.social ?? 0;
  const total = auth + news + social + (c.unknown ?? 0);
  if (total === 0) return null;
  const pct = (n: number) => Math.round((n / total) * 100);
  const seg = (n: number) => `${(n / total) * 100}%`;
  return (
    <div className="bg-surface-1 border border-border-subtle rounded-lg p-4 h-full flex flex-col gap-2">
      <span className="text-xs text-fg-muted">観測の足場 (信頼度構成)</span>
      <div className="flex h-3 rounded-sm overflow-hidden bg-surface-3">
        <div className="bg-success/60" style={{ width: seg(auth) }} title={`一次/研究 ${auth}`} />
        <div className="bg-accent/50" style={{ width: seg(news) }} title={`ニュース ${news}`} />
        <div className="bg-warning/50" style={{ width: seg(social) }} title={`SNS ${social}`} />
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-fg-muted tnum">
        <span className="inline-flex items-center gap-1"><span className="w-1.5 h-1.5 rounded-full bg-success" /> 一次/研究 {pct(auth)}%</span>
        <span className="inline-flex items-center gap-1"><span className="w-1.5 h-1.5 rounded-full bg-accent" /> ニュース {pct(news)}%</span>
        <span className="inline-flex items-center gap-1"><span className="w-1.5 h-1.5 rounded-full bg-warning" /> SNS {pct(social)}%</span>
      </div>
    </div>
  );
}

// 重要度内訳 (生 high はここに降格。主 KPI は「要対応」)。
function ImportanceBreakdown({ ov }: { ov: OverviewData }) {
  const imp = ov.importance;
  const total = imp.high + imp.medium + imp.low;
  const seg = (n: number) => (total > 0 ? `${(n / total) * 100}%` : "0%");
  return (
    <div className="bg-surface-1 border border-border-subtle rounded-lg p-4 h-full flex flex-col gap-2">
      <span className="text-xs text-fg-muted">重要度の内訳 (観測 {total.toLocaleString()})</span>
      {total > 0 ? (
        <>
          <div className="flex h-3 rounded-sm overflow-hidden bg-surface-3">
            <div className="bg-critical/70" style={{ width: seg(imp.high) }} title={`高 ${imp.high}`} />
            <div className="bg-warning/70" style={{ width: seg(imp.medium) }} title={`中 ${imp.medium}`} />
            <div className="bg-fg-subtle/40" style={{ width: seg(imp.low) }} title={`低 ${imp.low}`} />
          </div>
          <div className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-fg-muted tnum">
            <span className="inline-flex items-center gap-1"><span className="w-1.5 h-1.5 rounded-full bg-critical" /> 高 {imp.high}</span>
            <span className="inline-flex items-center gap-1"><span className="w-1.5 h-1.5 rounded-full bg-warning" /> 中 {imp.medium}</span>
            <span className="inline-flex items-center gap-1"><span className="w-1.5 h-1.5 rounded-full bg-fg-subtle" /> 低 {imp.low}</span>
          </div>
        </>
      ) : (
        <Empty>データなし</Empty>
      )}
    </div>
  );
}

// ── KPI 概観: 要注意 + ミッション KPI + 内訳 + 足場 (司令塔ヘッダ) ──
export function KpiRowWidget({ mobile }: WidgetProps = {}) {
  const summary = useQuery({ queryKey: ["dashboard"], queryFn: () => dashboardApi.summary(7), refetchInterval: 60_000 });
  const ov = useOverview();
  if (summary.isError || ov.isError) {
    return (
      <WidgetShell>
        <WidgetError />
      </WidgetShell>
    );
  }
  if (!ov.data) {
    return (
      <WidgetShell>
        <Loading />
      </WidgetShell>
    );
  }
  const d = ov.data;
  const s = summary.data?.summary;
  const dp = s?.daily_posts ?? [];
  const today = dp.length > 0 ? dp[dp.length - 1][1] : 0;
  const yesterday = dp.length > 1 ? dp[dp.length - 2][1] : 0;
  const trend = dp.map(([, n]) => n);

  // ミッション KPI: 今日の脈 + decision-bearing な要対応/日本/KEV/APT。
  // 配列化して desktop(横5列) と mobile(コンパクト2列) で同一定義を共有。
  type Tile = Parameters<typeof KpiTile>[0];
  const tiles: Tile[] = [
    { label: "今日の観測", value: today, delta: today - yesterday, deltaTitle: "前日比", trend, href: "/app", note: "報道/観測 件数 (攻撃件数ではない)", icon: <Newspaper className="h-3.5 w-3.5" /> },
    { label: "要対応", value: d.actionable.current, delta: d.actionable.delta, deltaTitle: "前期比", tone: "critical", note: "重要度高でKEVまたは日本関連 + 日本のランサム被害 + 日本の事前配置", href: `${INTEL}/threats`, icon: <ShieldAlert className="h-3.5 w-3.5" /> },
    { label: "日本標的", value: d.japan_targeted.current, delta: d.japan_targeted.delta, deltaTitle: "前期比", tone: "warning", note: "日本の被害・日本監視対象・重要度 高+中", href: `${INTEL}/threats`, icon: <Flag className="h-3.5 w-3.5" /> },
    { label: "KEV 悪用", value: d.kev.current, delta: d.kev.delta, deltaTitle: "前期比", note: `実悪用CVE・新規 +${d.kev.new_count}`, icon: <Bug className="h-3.5 w-3.5" /> },
    { label: "稼働 敵対APT", value: d.active_apt.current, delta: d.active_apt.delta, deltaTitle: "前期比", note: `中露朝イ・急増 ${d.active_apt.spiking}`, href: `${INTEL}/threats`, icon: <Crosshair className="h-3.5 w-3.5" /> },
  ];

  return (
    <div className={`flex flex-col ${mobile ? "gap-2" : "gap-3"}`}>
      <div className="flex items-center justify-between gap-3">
        <span className="text-sm font-semibold text-fg">KPI 概観</span>
        <WindowSelector asOf={asOfLabel(d.as_of)} />
      </div>

      <AttentionStrip items={d.attention} mobile={mobile} />

      {mobile ? (
        // コンパクト 2 列。最後の 1 枚 (稼働APT) は全幅で割り切れない余白を埋める。
        <div className="grid grid-cols-2 gap-2">
          {tiles.map((t, i) => (
            <div key={t.label} className={i === tiles.length - 1 ? "col-span-2" : ""}>
              <KpiTile {...t} compact />
            </div>
          ))}
        </div>
      ) : (
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
          {tiles.map((t) => (
            <KpiTile key={t.label} {...t} />
          ))}
        </div>
      )}

      {/* 重要度内訳 (生 high はここ) + 観測の足場 */}
      <div className={`grid grid-cols-1 lg:grid-cols-2 ${mobile ? "gap-2" : "gap-3"}`}>
        <ImportanceBreakdown ov={d} />
        <ConfidenceStrip c={d.source_confidence} />
      </div>
    </div>
  );
}

// ── 脅威の構成と変化 (二眼): 戦術=セクター / 戦略=敵対国籍・地域。構成比%・前期比pp ──
// mobile は各分類 top3 + RankBars compact (ラベルを行上に・バー全幅)。
export function ThreatPictureWidget({ config, mobile }: WidgetProps = {}) {
  const { data, isError } = useOverview(config);
  if (isError) {
    return (
      <WidgetCard title="脅威の構成と変化">
        <WidgetError />
      </WidgetCard>
    );
  }
  if (!data) {
    return (
      <WidgetCard title="脅威の構成と変化">
        <Loading />
      </WidgetCard>
    );
  }
  const cap = mobile ? 3 : 6;
  const rows = (ms: Mover[]) => moversToRows(ms.slice(0, cap), { share: true });
  return (
    <WidgetCard title="脅威の構成と変化 (構成比・前期比pp)" href={`${INTEL}/pmesii`} linkLabel="情勢 →">
      <div className="space-y-4">
        <Section title="戦術: 標的セクター (構成比%)">
          <RankBars items={rows(data.sectors)} max={100} emptyLabel="セクター観測なし" compact={mobile} />
        </Section>
        <Section title="戦略: 敵対国籍の活動 (中露朝イ ほか)">
          <RankBars items={rows(data.nations)} max={100} emptyLabel="該当なし" compact={mobile} />
        </Section>
        <Section title="戦略: 地域別 (被害観測)">
          <RankBars items={rows(data.regions)} max={100} emptyLabel="該当なし" compact={mobile} />
        </Section>
      </div>
      <p className="text-[11px] text-fg-subtle mt-3 pt-2 border-t border-border-subtle">
        %=各分類の観測に占める割合 / pp=前期比のポイント差{mobile ? "" : " / 右端=件数"}。
        収集量の増減に左右されない比較です。
      </p>
    </WidgetCard>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="text-xs font-semibold text-fg-muted mb-1.5">{title}</div>
      {children}
    </div>
  );
}

// ── 今動いているアクター: カウントでなく実体 (spike/新規の triage) ──
export function NotableActorsWidget({ config, mobile }: WidgetProps = {}) {
  const { data, isError } = useOverview(config);
  if (isError) {
    return (
      <WidgetCard title="今動いているアクター">
        <WidgetError />
      </WidgetCard>
    );
  }
  const list = (data?.notable_actors ?? []).slice(0, mobile ? 5 : 8);
  return (
    <WidgetCard title="今動いているアクター (急増 / 新規)" href={`${INTEL}/threats`} linkLabel="脅威 →">
      {list.length === 0 ? (
        <Empty>直近で急増 / 新規のアクターはありません。</Empty>
      ) : (
        <ul className="space-y-1.5">
          {list.map((a) => (
            <li key={a.actor_id} className="flex items-center gap-2 text-[13px]">
              {a.tag === "spike" ? (
                <Flame className="h-3.5 w-3.5 text-warning shrink-0" aria-label="spike" />
              ) : (
                <Sparkles className="h-3.5 w-3.5 text-accent shrink-0" aria-label="新規" />
              )}
              <a href={actorHref(a.actor_id)} className="font-medium text-fg hover:text-accent truncate">
                {a.canonical}
              </a>
              {a.nation && <span className="text-[11px] text-fg-subtle shrink-0">{a.nation}</span>}
              <span
                className="ml-auto shrink-0 text-accent inline-flex items-center"
                aria-hidden
                dangerouslySetInnerHTML={{ __html: a.sparkline }}
              />
              <span className="tnum text-fg-muted text-xs w-7 text-right shrink-0">{a.total}</span>
            </li>
          ))}
        </ul>
      )}
    </WidgetCard>
  );
}

// ── 重要脆弱性: KEV (実悪用) / 高CVSS を上に。まとめ記事由来は CVE 主体表示 (発見4) ──
export function VulnerabilitiesWidget({ config, mobile }: WidgetProps = {}) {
  const { data, isError } = useOverview(config);
  if (isError) {
    return (
      <WidgetCard title="重要脆弱性">
        <WidgetError />
      </WidgetCard>
    );
  }
  const list = (data?.vulnerabilities ?? []).slice(0, mobile ? 4 : 6);
  return (
    <WidgetCard title="重要脆弱性 (KEV / 高CVSS)">
      {list.length === 0 ? (
        <Empty>直近で該当する脆弱性報告はありません。</Empty>
      ) : (
        <ul className="space-y-1.5">
          {list.map((v) => (
            <li key={v.cve} className="border-l-2 border-border-emphasized pl-2.5 py-0.5">
              <div className="flex items-center gap-2 text-[12px]">
                <a href={v.is_roundup ? v.nvd_url : v.url} target="_blank" rel="noopener noreferrer" className="font-mono text-accent hover:underline">
                  {v.cve}
                </a>
                {v.kev && (
                  <span className="px-1 rounded bg-critical-soft text-critical text-[10px] font-semibold">KEV 悪用中</span>
                )}
                {v.cvss != null && <span className="tnum text-fg-muted">CVSS {v.cvss}</span>}
              </div>
              {v.is_roundup ? (
                // まとめ記事 title は情報ゼロ → NVD 詳細へ誘導し、出典はまとめとして控えめに
                <a href={v.nvd_url} target="_blank" rel="noopener noreferrer" className="block text-[12px] text-fg-muted hover:text-accent">
                  NVD 詳細を見る <span className="text-fg-subtle">(検出元: まとめ記事)</span>
                </a>
              ) : (
                <a href={`/app/article/${encodeURIComponent(v.article_id)}`} className="block text-[13px] text-fg hover:text-accent line-clamp-1">
                  {v.title}
                </a>
              )}
            </li>
          ))}
        </ul>
      )}
    </WidgetCard>
  );
}
