// 「標準 / アドバンスド」切替。どちらが今の面かを明示し、他方への導線を出す。
//
// 公開サイト (標準) のヘッダと、アドバンスド (旧写し) の帯 (MirrorBanner) の両方から
// 使う共有の見た目。2026-10-05 にアドバンスドを公開サイトと同じドメインの `/app/`
// 配下へ統合したため、両面は常に同一オリジン — 相手サイトのオリジンを渡す必要が
// 無くなり、リンクは常に有効 (無効表示の分岐は廃止)。
import { mirrorSwitchHref, publicSwitchHref } from "../utils/siteSwitch";

interface SiteSwitchProps {
  /** 今どちらの面を見ているか。 */
  current: "standard" | "advanced";
}

export function SiteSwitch({ current }: SiteSwitchProps) {
  const otherHref = current === "standard" ? mirrorSwitchHref() : publicSwitchHref();
  return (
    <div
      className="inline-flex shrink-0 items-center gap-0.5 whitespace-nowrap rounded-full border border-border-subtle bg-surface-2 p-0.5 text-[11px]"
      role="group"
      aria-label="標準・拡張の切替"
    >
      <SwitchSegment label="標準" active={current === "standard"} href={current === "standard" ? null : otherHref} />
      <SwitchSegment label="拡張" active={current === "advanced"} href={current === "advanced" ? null : otherHref} />
    </div>
  );
}

function SwitchSegment({
  label,
  active,
  href,
}: {
  label: string;
  active: boolean;
  /** null = 今の面 (リンクにしない)。 */
  href: string | null;
}) {
  if (active) {
    return (
      <span
        aria-current="page"
        className="rounded-full bg-accent px-2.5 py-1 font-medium text-white"
      >
        {label}
      </span>
    );
  }
  return (
    <a href={href ?? undefined} className="rounded-full px-2.5 py-1 text-fg-subtle hover:bg-surface-3 hover:text-fg">
      {label}
    </a>
  );
}
