import { useEffect, useRef, useState } from "react";
import { pageContainer } from "../components/Page";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { runsApi, type LogLine } from "../api/runs";
import { StatusBadge } from "./DashboardPage";
import { formatJst } from "../utils/date";
import { vocabLabel } from "../hooks/useVocab";

export function RunDetailPage({ runId }: { runId: number }) {
  const qc = useQueryClient();
  const { data: run } = useQuery({
    queryKey: ["run", runId],
    queryFn: () => runsApi.get(runId),
    refetchInterval: (q) => (q.state.data?.status === "running" ? 2_000 : false),
  });

  const [lines, setLines] = useState<LogLine[]>([]);
  const [autoScroll, setAutoScroll] = useState(true);
  const [streamStatus, setStreamStatus] = useState<"connecting" | "open" | "done" | "closed">("connecting");
  const logRef = useRef<HTMLDivElement>(null);
  const lastSeqRef = useRef<number>(0);

  // 1. Fetch initial log lines
  useEffect(() => {
    let cancelled = false;
    runsApi.log(runId, 0).then((d) => {
      if (cancelled) return;
      setLines(d.lines);
      lastSeqRef.current = d.lines.length > 0 ? d.lines[d.lines.length - 1].seq : 0;
    }).catch(() => {});
    return () => { cancelled = true; };
  }, [runId]);

  // 2. SSE subscription for live updates
  useEffect(() => {
    if (!run || run.status !== "running") {
      setStreamStatus("done");
      return;
    }
    const es = new EventSource(`/runs/${runId}/stream`);
    setStreamStatus("connecting");

    es.onopen = () => setStreamStatus("open");

    es.addEventListener("line", (e: MessageEvent) => {
      try {
        const data = JSON.parse(e.data) as { seq: number; line: string };
        setLines((prev) => {
          // dedup by seq (in case of reconnection overlap)
          if (prev.some((l) => l.seq === data.seq)) return prev;
          return [...prev, { seq: data.seq, ts: "", stream: "stdout", line: data.line }];
        });
        lastSeqRef.current = data.seq;
      } catch {
        // skip malformed
      }
    });

    es.addEventListener("done", () => {
      setStreamStatus("done");
      es.close();
      qc.invalidateQueries({ queryKey: ["run", runId] });
    });

    es.addEventListener("status", (e: MessageEvent) => {
      try {
        const data = JSON.parse(e.data) as { status: string };
        if (data.status !== "running") {
          setStreamStatus("done");
          qc.invalidateQueries({ queryKey: ["run", runId] });
        }
      } catch {}
    });

    es.onerror = () => {
      setStreamStatus("closed");
      // EventSource auto-reconnects, but we mark visual state
    };

    return () => { es.close(); };
  }, [run?.status, runId, qc]);

  // 3. Auto-scroll on new lines
  useEffect(() => {
    if (autoScroll && logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
    }
  }, [lines, autoScroll]);

  if (!run) {
    return <div className="p-8 text-fg-muted">読み込み中...</div>;
  }

  return (
    <div className={`${pageContainer("wide")} space-y-4`}>

      {/* Header */}
      <div className="flex items-baseline justify-between flex-wrap gap-3">
        <div className="flex items-baseline gap-3">
          <h2 className="m-0 text-xl font-bold text-fg tracking-tight tnum">実行 #{run.id}</h2>
          <StatusBadge status={run.status} />
          <span className="text-fg-muted text-sm">{run.pipeline}</span>
          {run.dry_run && <span className="text-fg-subtle text-xs bg-surface-2 px-2 py-0.5 rounded">試験実行(投稿なし)</span>}
        </div>
        <a href="/app/runs" className="text-accent text-sm hover:underline">← 実行一覧</a>
      </div>

      {/* Meta */}
      <div className="bg-surface-1 border border-border-subtle rounded-lg p-4 grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
        <Meta label="開始" value={formatJst(run.started_at)} />
        <Meta label="終了" value={formatJst(run.finished_at)} />
        <Meta label="実行きっかけ" value={vocabLabel("trigger", run.triggered_by)} />
        <Meta label="ログ行数" value={`${run.log_line_count}${run.log_truncated ? " (打ち切り)" : ""}`} />
        <Meta label="取得" value={String(run.total_fetched)} />
        <Meta label="要約" value={String(run.summarized)} />
        <Meta label="投稿" value={String(run.posted)} accent={run.posted > 0} />
        <Meta label="エラー" value={String(run.error_count)} danger={run.error_count > 0} />
      </div>

      {/* Live log */}
      <div className="bg-surface-1 border border-border-subtle rounded-lg overflow-hidden">
        <div className="px-4 py-2.5 border-b border-border-subtle flex items-center justify-between">
          <div className="flex items-center gap-3">
            <h3 className="m-0 text-sm font-semibold text-fg">実行ログ</h3>
            <StreamStatus status={streamStatus} />
            <span className="text-fg-subtle text-xs tnum">{lines.length} 行</span>
          </div>
          <label className="flex items-center gap-2 text-xs text-fg-muted cursor-pointer">
            <input type="checkbox" checked={autoScroll} onChange={(e) => setAutoScroll(e.target.checked)} className="accent-accent" />
            自動スクロール
          </label>
        </div>
        <div
          ref={logRef}
          className="bg-black/60 font-mono text-xs text-fg leading-relaxed p-3 max-h-[480px] overflow-y-auto whitespace-pre-wrap break-words"
        >
          {lines.length === 0 && (
            <div className="text-fg-subtle italic">ログを待機中...</div>
          )}
          {lines.map((ln) => (
            <div key={ln.seq} className="flex gap-3">
              <span className="text-fg-faint shrink-0 tnum text-[10px] mt-0.5 select-none">{ln.seq}</span>
              <span className={lineColor(ln.line)}>{ln.line}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function Meta({ label, value, accent, danger }: { label: string; value: string; accent?: boolean; danger?: boolean }) {
  return (
    <div>
      <div className="text-fg-subtle text-xs uppercase tracking-wider font-semibold">{label}</div>
      <div className={`mt-0.5 font-semibold tnum ${danger ? "text-critical" : accent ? "text-accent" : "text-fg"}`}>{value}</div>
    </div>
  );
}

function StreamStatus({ status }: { status: "connecting" | "open" | "done" | "closed" }) {
  const map: Record<string, { label: string; cls: string }> = {
    connecting: { label: "接続中", cls: "bg-warning-soft text-warning" },
    open: { label: "配信中", cls: "bg-success-soft text-success animate-pulse" },
    done: { label: "完了", cls: "bg-surface-3 text-fg-muted" },
    closed: { label: "切断", cls: "bg-critical-soft text-critical" },
  };
  const s = map[status];
  return <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-semibold ${s.cls}`}><span className="w-1.5 h-1.5 rounded-full bg-current" />{s.label}</span>;
}

function lineColor(line: string): string {
  // JSON log line の level で色付け
  if (/"level":\s*"error"/i.test(line) || /\berror\b/i.test(line)) return "text-critical";
  if (/"level":\s*"warn/i.test(line) || /\bwarning\b/i.test(line)) return "text-warning";
  if (/"level":\s*"info"/i.test(line)) return "text-fg";
  return "text-fg-muted";
}
