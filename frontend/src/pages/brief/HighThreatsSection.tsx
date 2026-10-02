// 高脅威の安全網 (2026-10-02): high と判定したのに alert (即時通知) に流れなかった脅威の一覧。
// 従来は Discord の要点にだけ出ていて、Web のブリーフには入っていなかった
// (JadePuffer / Storm-3168 の high 7 本が Web のどこにも出ていなかった)。
// 日本が標的のものを先頭に並べる (並びは backend の collect_high_threats が決める)。

import { SUBHEAD } from "../../components/headings";
import type { HighThreatsPayload } from "../../api/types";

export function HighThreatsSection({ data }: { data: HighThreatsPayload }) {
  const more = data.total - data.items.length;
  return (
    <section className="space-y-2">
      <h4 className={`${SUBHEAD} border-t border-border-subtle pt-3 m-0`}>
        高脅威 — 即時通知していない high ({data.total} 件)
      </h4>
      <ul className="space-y-1 m-0 p-0 list-none">
        {data.items.map((it) => (
          <li key={it.article_id} className="flex items-start gap-2 min-w-0">
            <span className="text-[12px] px-1.5 rounded border border-border-default text-fg-subtle whitespace-nowrap">
              {it.category_label}
            </span>
            {it.is_japan && (
              <span className="text-[12px] px-1.5 rounded border border-critical/40 text-critical whitespace-nowrap">
                日本
              </span>
            )}
            <a
              href={it.url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-sm text-accent hover:underline min-w-0 break-words"
            >
              {it.title}
            </a>
          </li>
        ))}
      </ul>
      {more > 0 && <p className="text-xs text-fg-subtle m-0">ほか {more} 件</p>}
    </section>
  );
}
