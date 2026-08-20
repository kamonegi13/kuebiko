// ops 通知 (post_ops_message) の永続化ビューア (2026-08-21)。
//
// なぜ画面が要るか: 従来 post_ops_message は Discord #ops チャンネルへ送るだけで DB に
// 残さず、webhook 不達・未設定の警告が痕跡なく消えていた (access_audit と同じ確立教訓
// 「stdout は監査にならない」「成功も記録しないと沈黙の意味を決められない」に反する)。
// パイプライン失敗・週次監査 (fill_rate_audit) 等の通知を Web UI からも追えるようにする。
//
// 置き場が「設定 → 履歴・監査」タブである理由: アクセス監査と同じ「後から遡って調べる」
// 記録であり、設定そのものではないため。

import { useQuery } from "@tanstack/react-query";
import { Bell } from "lucide-react";
import { pagesApi } from "../api/pages";
import { formatJst } from "../utils/date";
import { vocabLabel } from "../hooks/useVocab";

const IMPORTANCE_CLASS: Record<string, string> = {
  high: "bg-critical-soft text-critical",
  medium: "bg-warning-soft text-warning",
  low: "bg-surface-3 text-fg-muted",
};

export function OpsNoticesCard() {
  const { data, isError } = useQuery({
    queryKey: ["ops-notices"],
    queryFn: () => pagesApi.opsNotices(50),
    refetchInterval: 5 * 60_000,
  });

  const notices = data?.notices ?? [];

  return (
    <div className="bg-surface-1 border border-border-subtle rounded-lg p-4 space-y-3">
      <div className="flex items-center gap-2">
        <Bell className="h-4 w-4 text-fg-muted" />
        <h3 className="m-0 text-sm font-semibold text-fg">運用通知 (ops)</h3>
      </div>

      <p className="m-0 text-[11px] text-fg-subtle leading-relaxed">
        パイプライン失敗・週次監査の警告など Discord #ops チャンネルへの通知を、送信の成否に
        関わらず記録しています。webhook 未設定・不達でも痕跡が残ります。
      </p>

      <div className="space-y-1.5">
        <h4 className="m-0 text-[10.5px] uppercase tracking-wider text-fg-muted font-semibold">
          直近 {notices.length} 件 (保持 180 日)
        </h4>
        {isError || data?.error ? (
          <div className="text-[11px] text-critical">
            通知を読み出せませんでした{data?.error ? `: ${data.error}` : ""}
          </div>
        ) : notices.length === 0 ? (
          <div className="text-[11px] text-fg-subtle">まだ記録がありません。</div>
        ) : (
          <div className="space-y-2">
            {notices.map((n) => (
              <div key={n.id} className="border-t border-border-subtle pt-2 text-[11px]">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-fg-subtle whitespace-nowrap">{formatJst(n.created_at)}</span>
                  <span
                    className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${
                      IMPORTANCE_CLASS[n.importance] ?? IMPORTANCE_CLASS.low
                    }`}
                  >
                    {vocabLabel("importance", n.importance)}
                  </span>
                  {!n.sent && (
                    <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-critical-soft text-critical">
                      ⚠️ 未送信
                    </span>
                  )}
                </div>
                <div className="text-fg font-medium mt-0.5 break-words">{n.title}</div>
                <div className="text-fg-subtle mt-0.5 max-h-24 overflow-y-auto whitespace-pre-wrap break-words">
                  {n.body}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
