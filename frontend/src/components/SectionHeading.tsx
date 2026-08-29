import type { ReactNode } from "react";

/** 節の見出し (縦バー + 見出し + 罫線 + 右の導線)。
 *
 *  公開ページ (PublicNewsSite の PortalSection) と **同じ見え方** に揃える。
 *  枠を並べただけでは節の切り替わりが読めない — 罫線が横に伸びることで、
 *  どこで話題が変わったかが一目で分かる (公開ページで実証済みの形)。
 */
export function SectionHeading({
  title,
  note,
  action,
}: {
  title: string;
  /** 見出しの右に小さく添える補足 (並び順・期間など)。 */
  note?: string;
  /** 右端の導線 (「一覧へ →」相当)。 */
  action?: ReactNode;
}) {
  return (
    <div className="flex items-center gap-2 mb-3">
      <span className="w-[3px] h-[15px] rounded-full shrink-0 bg-accent" aria-hidden />
      <h3 className="m-0 text-[15px] font-bold tracking-wide text-fg">{title}</h3>
      {note && <span className="shrink-0 text-[12px] text-fg-subtle">{note}</span>}
      <span className="flex-1 border-b border-border-subtle" />
      {action}
    </div>
  );
}
