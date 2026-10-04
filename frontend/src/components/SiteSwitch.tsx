// 「標準 / アドバンスド」切替。どちらが今の面かを明示し、他方への導線を出す。
//
// 公開サイト (標準) のヘッダと、写し (アドバンスド) の帯 (MirrorBanner) の両方から
// 使う共有の見た目。他方のオリジンが未設定のとき (ビルド時に導線先が無い) は
// リンクを無効表示にする — 押せないのに押せそうに見せると、落ちているのかと
// 誤解させる (§4 の原則「導線は常に動くものだけ出す」と同根)。
import { mirrorSwitchHref, publicSwitchHref } from "../utils/siteSwitch";

interface SiteSwitchProps {
  /** 今どちらの面を見ているか。 */
  current: "standard" | "advanced";
  /** 相手サイトのオリジン (未設定なら無効表示)。 */
  otherOrigin: string;
}

export function SiteSwitch({ current, otherOrigin }: SiteSwitchProps) {
  const otherHref =
    current === "standard" ? mirrorSwitchHref(otherOrigin) : publicSwitchHref(otherOrigin);
  return (
    <div
      className="inline-flex items-center gap-0.5 rounded-full border border-border-subtle bg-surface-2 p-0.5 text-[11px]"
      role="group"
      aria-label="標準・アドバンスド切替"
    >
      <SwitchSegment label="標準" active={current === "standard"} href={current === "standard" ? null : otherHref} />
      <SwitchSegment label="アドバンスド" active={current === "advanced"} href={current === "advanced" ? null : otherHref} />
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
  /** null = 今の面 (リンクにしない)。undefined = 相手サイトのオリジン未設定 (無効表示)。 */
  href: string | null | undefined;
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
  if (href) {
    return (
      <a href={href} className="rounded-full px-2.5 py-1 text-fg-subtle hover:bg-surface-3 hover:text-fg">
        {label}
      </a>
    );
  }
  return (
    <span className="cursor-not-allowed rounded-full px-2.5 py-1 text-fg-subtle/40" title="未設定">
      {label}
    </span>
  );
}
