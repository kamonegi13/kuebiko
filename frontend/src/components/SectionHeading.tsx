import type { ReactNode } from "react";

/** 節の見出し (縦バー + 見出し + 罫線 + 右の操作)。
 *
 *  公開ページ (PublicNewsSite の PortalSection) と **同じ見え方** に揃える。
 *  枠を並べただけでは節の切り替わりが読めない — 罫線が横に伸びることで、
 *  どこで話題が変わったかが一目で分かる (公開ページで実証済みの形)。
 */
export function SectionHeading({
  title,
  note,
  action,
  sticky = false,
}: {
  title: string;
  /** 見出しの右に小さく添える補足 (期間など)。狭い画面では隠す。 */
  note?: string;
  /** 右端の操作 (期間の切替など)。 */
  action?: ReactNode;
  /** スクロールしても上に残す。**何を見ているかが常に見えている**必要がある節に使う
   *  (長い本文を読み進むと、どの期間の総括か分からなくなる — 2026-08-29 利用者指摘)。 */
  sticky?: boolean;
}) {
  return (
    <div
      className={
        "flex items-center gap-2 mb-3 " +
        // sticky の天井は TopBar (h-12)。デスクトップはさらに ControlBar (md:top-12
        // で貼り付く) の下 = 92px。地色を敷かないと本文が透けて重なる。
        (sticky
          ? "sticky top-12 md:top-[5.75rem] z-10 -mx-1 px-1 py-2 bg-bg/95 backdrop-blur-md"
          : "")
      }
    >
      <span className="w-[3px] h-[15px] rounded-full shrink-0 bg-accent" aria-hidden />
      {/* ⚠ 折り返させない。狭い画面で「日次総 / 括」と割れる (実機で発生)。 */}
      <h3 className="m-0 text-[15px] font-bold tracking-wide text-fg whitespace-nowrap">{title}</h3>
      {note && (
        <span className="hidden sm:inline shrink-0 text-[12px] text-fg-subtle whitespace-nowrap">
          {note}
        </span>
      )}
      <span className="flex-1 border-b border-border-subtle min-w-[8px]" />
      {action}
    </div>
  );
}
