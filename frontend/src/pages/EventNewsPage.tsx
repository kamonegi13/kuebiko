import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { pageContainer } from "../components/Page";
import { Drawer } from "../components/Drawer";
import { formatJstCompact } from "../utils/date";
import { EventNewsDetailBody, SourceChip } from "./eventnews/EventNewsDetail";
import { fetchEventNews } from "../api/eventnews";

const IMPORTANCE_FILTERS: { key: string; label: string }[] = [
  { key: "high", label: "high のみ" },
  { key: "high,medium", label: "high + medium" },
  { key: "", label: "すべて" },
];

export function EventNewsPage() {
  const [openId, setOpenId] = useState<string | null>(null);
  const [imp, setImp] = useState("high,medium");
  const { data, isFetching, error } = useQuery({
    queryKey: ["eventnews-list", imp],
    queryFn: () => fetchEventNews(80, imp || undefined),
    refetchInterval: 5 * 60 * 1000,
  });

  const items = data?.items ?? [];
  const openItem = items.find((i) => i.id === openId) ?? null;

  return (
    <div className={`${pageContainer("wide")} space-y-4`}>
      <div>
        <h2 className="m-0 text-xl font-bold text-fg tracking-tight">事象ニュース</h2>
        <p className="text-fg-muted text-sm mt-1">
          収集した記事を事象単位で読む。複数媒体が報じた事象は 1 本に統合して生成し、新しい記事が加わると更新される。単独報はその記事の要約を表示する。
        </p>
      </div>

      <div className="flex gap-1.5">
        {IMPORTANCE_FILTERS.map((f) => (
          <button
            key={f.key}
            onClick={() => setImp(f.key)}
            className={`text-xs px-3 py-1.5 rounded border transition-colors ${
              imp === f.key ? "border-accent text-accent bg-accent/10" : "border-border-default text-fg-muted hover:text-fg"
            }`}
          >
            {f.label}
          </button>
        ))}
        {data && <span className="ml-auto self-center text-xs text-fg-subtle">{items.length} 件</span>}
      </div>

      {isFetching && !data && <div className="text-fg-subtle text-sm">読み込み中…</div>}
      {error && <div className="text-critical text-sm">エラー: {String(error)}</div>}
      {data && items.length === 0 && (
        <div className="text-fg-muted text-sm bg-surface-1 border border-border-subtle rounded-lg p-4">
          該当する事象がありません。
        </div>
      )}

      <ul className="space-y-1.5">
        {items.map((it) => (
          <li
            key={it.id}
            className="flex items-start gap-2 bg-surface-1 border border-border-subtle rounded-lg px-3 py-2.5"
          >
            <span
              className={`mt-1.5 w-1.5 h-1.5 rounded-full shrink-0 ${
                it.importance === "high" ? "bg-critical" : it.importance === "medium" ? "bg-warning" : "bg-fg-subtle"
              }`}
            />
            <div className="flex-1 min-w-0">
              <button
                onClick={() => setOpenId(it.id)}
                className="block w-full text-left text-sm font-medium leading-snug text-fg hover:text-accent hover:underline"
                title="事象の詳細を開く"
              >
                {it.headline}
              </button>
              {it.preview && (
                <p className="text-xs text-fg-muted leading-relaxed mt-1 line-clamp-4">{it.preview}</p>
              )}
              <div className="text-[11px] flex flex-wrap items-center gap-x-1.5 gap-y-1 mt-1">
                <SourceChip item={it} />
                {it.member_count > 1 && (
                  <span className="px-1 rounded bg-surface-2 text-fg-muted">{it.member_count} 記事を統合</span>
                )}
                {it.status === "updated" && (
                  <span className="px-1 rounded bg-accent/15 text-accent">更新</span>
                )}
                {it.best_source_tier && it.best_source_tier !== "news" && (
                  <span className="px-1 rounded bg-surface-2 text-fg-muted">{it.best_source_tier}</span>
                )}
                <span className="ml-auto shrink-0 text-fg-subtle" title="最新報道の時刻">
                  {formatJstCompact(it.last_reported_at)}
                </span>
              </div>
            </div>
          </li>
        ))}
      </ul>

      <Drawer
        isOpen={openId !== null}
        onClose={() => setOpenId(null)}
        title={openItem?.headline ?? "事象"}
        widthClass="md:w-[46rem]"
        mobileGutter
        swipeToClose
      >
        {openId && <EventNewsDetailBody id={openId} />}
      </Drawer>
    </div>
  );
}
