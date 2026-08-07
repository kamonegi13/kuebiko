import { useState } from "react";
import { goToActor } from "../utils/intelNav";
import type { ActorBrief, SnapshotResponse } from "../api/types";

export function DiscoveryPanel({ snapshot }: { snapshot: SnapshotResponse }) {
  const [open, setOpen] = useState(
    () => typeof window !== "undefined" && window.location.pathname.includes("/app/intel/threats"),
  );
  const d = snapshot.discovery;

  return (
    <details
      open={open}
      onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}
      className="bg-surface-1 border border-border-subtle rounded-lg overflow-hidden mt-2"
    >
      <summary className="list-none flex items-center gap-3 px-4 py-2 text-sm cursor-pointer text-fg-muted hover:bg-surface-2 transition-colors [&::-webkit-details-marker]:hidden">
        <span className={`text-[9px] text-fg-subtle transition-transform ${open ? "rotate-90" : ""}`}>▶</span>
        <span className="font-semibold text-fg">変化・異常</span>
        <div className="flex gap-3.5 ml-auto text-[11px]">
          <span>新規 <strong className="text-fg tnum font-semibold">{d.new_actors.length}</strong></span>
          <span>急増 <strong className="text-fg tnum font-semibold">{d.spiking_actors.length}</strong></span>
          <span>再活性化 <strong className="text-fg tnum font-semibold">{d.waking_actors.length}</strong></span>
          <span>未特定 <strong className="text-fg tnum font-semibold">{d.unknown_bucket_count}</strong></span>
        </div>
      </summary>
      <div className="grid grid-cols-2 lg:grid-cols-4 border-t border-border-subtle">
        <Col title="新規アクター" actors={d.new_actors} suffix={(a) => `${a.total_articles}件`} onSelect={goToActor} />
        <Col title="急増" actors={d.spiking_actors} suffix={(a) => a.spike_ratio ? `×${a.spike_ratio.toFixed(1)}` : "NEW"} onSelect={goToActor} />
        <Col title="再活性化" actors={d.waking_actors} suffix={(a) => `${a.total_articles}件`} onSelect={goToActor} />
        <div className="p-4 border-l border-border-subtle bg-gradient-to-br from-surface-1 to-warning-soft">
          <h4 className="m-0 mb-2 text-[11px] text-fg-muted uppercase tracking-wider font-semibold">アクター未特定</h4>
          <div className="text-xl font-bold text-warning leading-none mb-1 tnum">{d.unknown_bucket_count}</div>
          <div className="text-[11px] text-fg-muted leading-snug">
            アクター未抽出。<a href="/app/intel/operations" className="text-accent cursor-pointer">→ 運用</a>
          </div>
        </div>
      </div>
    </details>
  );
}

function Col({ title, actors, suffix, onSelect }: {
  title: string;
  actors: ActorBrief[];
  suffix: (a: ActorBrief) => string;
  onSelect: (id: string) => void;
}) {
  return (
    <div className="p-4 border-r border-border-subtle last:border-r-0 min-h-[110px]">
      <h4 className="m-0 mb-2 text-[11px] text-fg-muted uppercase tracking-wider font-semibold">{title}</h4>
      {actors.length === 0 && <div className="text-[11px] text-fg-subtle italic">該当なし</div>}
      {actors.map((a) => (
        <div
          key={a.actor_id}
          onClick={() => onSelect(a.actor_id)}
          className="py-1 cursor-pointer text-sm flex justify-between items-baseline gap-2 text-fg hover:text-accent-hover hover:pl-1 transition-all"
        >
          <span>{a.canonical}</span>
          <span className="text-[11px] text-fg-subtle tnum">{suffix(a)}</span>
        </div>
      ))}
    </div>
  );
}
