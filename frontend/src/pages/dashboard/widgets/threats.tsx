// 脅威アクター / PMESII 系 widget。
// TopActors / ActorDossier。

import { useQuery } from "@tanstack/react-query";
import { Flame, Moon, Sparkles, Settings } from "lucide-react";
import { api } from "../../../api/client";
import type { ActorBrief } from "../../../api/types";
import { formatJstCompact } from "../../../utils/date";
import { actorHref } from "../../../utils/intelNav";
import { sectorLabel } from "../../../components/geo/sectorColors";
import {
  WidgetCard, Loading, Empty, WidgetError, ImportanceDot, cfgStr, cfgNum, type WidgetProps,
} from "../shared";

// アクター変化タグ → 表示アイコン (spike=炎 / new=新着 / waking=休眠から復帰)。
type ActorTag = "spike" | "new" | "waking" | "";
function TagIcon({ tag }: { tag: ActorTag }) {
  if (tag === "spike") return <Flame className="h-3.5 w-3.5 shrink-0" />;
  if (tag === "new") return <Sparkles className="h-3.5 w-3.5 shrink-0" />;
  if (tag === "waking") return <Moon className="h-3.5 w-3.5 shrink-0" />;
  return null;
}

// Intel Graph の該当タブへ deep-link (widget の主題に合わせ着地タブを正す)
const INTEL_THREATS = "/app/intel/threats";

function ActorMiniList({ tagged, limit = 6 }: { tagged: { a: ActorBrief; tag: ActorTag }[]; limit?: number }) {
  if (tagged.length === 0) return null;
  return (
    <ul className="space-y-0.5">
      {tagged.slice(0, limit).map(({ a, tag }) => (
        <li key={a.actor_id}>
          <a href={actorHref(a.actor_id)} className="flex items-center gap-2 px-1 py-0.5 rounded hover:bg-surface-2 no-underline group" title={`${a.canonical}${a.nation ? ` (${a.nation})` : ""}`}>
            <TagIcon tag={tag} />
            <span className="flex-1 min-w-0 truncate text-sm text-fg group-hover:text-accent-hover">{a.canonical}</span>
            {a.nation && <span className="text-[10px] text-fg-subtle shrink-0 uppercase">{a.nation}</span>}
            {a.sparkline && (
              <span className="sparkline-svg shrink-0 inline-flex items-center" dangerouslySetInnerHTML={{ __html: a.sparkline }} />
            )}
            <span className="tnum text-xs text-fg-muted w-8 text-right shrink-0">{a.total_articles}</span>
          </a>
        </li>
      ))}
    </ul>
  );
}

// ── 主要アクター (追跡量 top-N、国フィルタ可) ──
export function TopActorsWidget({ config }: WidgetProps) {
  const nation = cfgStr(config, "nation", "");
  const per = cfgNum(config, "per", 8);
  const { data, isError } = useQuery({
    queryKey: ["dash-threats", nation],
    queryFn: () => api.threats({ time: "30", nation: nation || undefined }),
    refetchInterval: 5 * 60_000,
  });
  const actors = [...(data?.actors ?? [])].sort((a, b) => b.total_articles - a.total_articles).slice(0, per);
  return (
    <WidgetCard title={`主要アクター${nation ? ` (${nation})` : ""}`} href={INTEL_THREATS} linkLabel="Threats →">
      {isError ? <WidgetError /> : !data ? <Loading /> : actors.length === 0 ? <Empty>該当アクターなし。</Empty> : (
        <ActorMiniList tagged={actors.map((a) => ({ a, tag: a.is_spike ? "spike" : a.is_new ? "new" : "" }))} limit={per} />
      )}
    </WidgetCard>
  );
}

// ── アクター・ドシエ (config.actor_id で固定したアクターの詳細) ──
export function ActorDossierWidget({ config }: WidgetProps) {
  const actorId = cfgStr(config, "actor_id", "");
  const { data, isError } = useQuery({
    queryKey: ["dash-actor", actorId],
    queryFn: () => api.actorDetail(actorId, "30"),
    enabled: actorId !== "",
    refetchInterval: 5 * 60_000,
  });
  if (actorId === "") {
    return <WidgetCard title="アクター・ドシエ">
      <Empty>
        <span className="inline-flex items-center gap-1">「<Settings className="h-3.5 w-3.5" />カスタマイズ」</span>でアクターを選択してください。
      </Empty>
    </WidgetCard>;
  }
  const act = data?.activity;
  return (
    <WidgetCard title={act?.canonical ?? actorId} href={actorHref(actorId)} linkLabel="Threats →">
      {isError ? <WidgetError /> : !data ? <Loading /> : !data.found || !act ? <Empty>このアクターのデータがありません。</Empty> : (
        <div className="space-y-2 text-xs">
          <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-fg-muted">
            {act.nation && <span>国: <b className="text-fg uppercase">{act.nation}</b></span>}
            {act.family && <span>系統: <b className="text-fg">{act.family}</b></span>}
            {act.mitre_group && <span>ATT&CK: <b className="text-fg">{act.mitre_group}</b></span>}
            <span>記事: <b className="text-fg tnum">{act.total_articles}</b></span>
            {act.japan_targeted_count > 0 && <span className="text-critical">日本標的 {act.japan_targeted_count}</span>}
          </div>
          <DossierChips label="TTP" items={act.top_ttps.slice(0, 6)} />
          <DossierChips label="CVE" items={act.top_cves.slice(0, 6)} />
          <DossierChips label="標的" items={act.top_sectors.slice(0, 5).map(([s]) => sectorLabel(s))} />
          <DossierChips label="マルウェア" items={act.top_malware_families.slice(0, 5).map(([s]) => s)} />
          {(data.recent_articles?.length ?? 0) > 0 && (
            <ul className="space-y-0.5 pt-1 border-t border-border-subtle">
              {(data.recent_articles ?? []).slice(0, 3).map((r) => (
                <li key={r.article_id} className="flex items-start gap-1.5 leading-snug">
                  <ImportanceDot importance={r.importance} className="mt-1" />
                  <a href={`/app/article/${encodeURIComponent(r.article_id)}`} className="min-w-0 text-fg hover:text-accent hover:underline line-clamp-1" title={r.title}>{r.title}</a>
                  <span className="text-fg-subtle shrink-0 ml-auto">{formatJstCompact(r.created_at)}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </WidgetCard>
  );
}

function DossierChips({ label, items }: { label: string; items: string[] }) {
  if (items.length === 0) return null;
  return (
    <div className="flex items-start gap-1.5">
      <span className="text-[10px] text-fg-subtle uppercase tracking-wider shrink-0 w-12 pt-0.5">{label}</span>
      <div className="flex flex-wrap gap-1">
        {items.map((it) => (
          <span key={it} className="px-1.5 py-0.5 rounded bg-surface-2 text-fg-muted text-[11px] truncate max-w-[140px]" title={it}>{it}</span>
        ))}
      </div>
    </div>
  );
}
