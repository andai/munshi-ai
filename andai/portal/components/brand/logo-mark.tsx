import { cn } from "@/lib/utils";

// The munshi app icon: a lowercase "m" drawn as waveform bars on an indigo tile, the last bar in
// saffron. The colours are fixed (it is the app icon), so it reads the same in light and dark.
// app/icon.svg is the same drawing for the browser tab.

const BARS = [12, 15, 13, 9.5, 13, 15, 12];
const BAR_WIDTH = 2.4;
const STEP = 3.2;
const BASELINE = 23.5;
const START = (32 - ((BARS.length - 1) * STEP + BAR_WIDTH)) / 2;

export const INK = "#3C3489";
export const INK_BRIGHT = "#534AB7";
export const SAFFRON = "#EF9F27";

export function LogoMark({
  size = 32,
  className,
  title,
}: {
  size?: number;
  className?: string;
  /** Accessible name; omit when the mark sits next to the wordmark (then it is decorative). */
  title?: string;
}) {
  return (
    <svg
      viewBox="0 0 32 32"
      width={size}
      height={size}
      className={cn("shrink-0", className)}
      role={title ? "img" : undefined}
      aria-label={title}
      aria-hidden={title ? undefined : true}
      focusable="false"
    >
      <defs>
        <linearGradient id="munshi-tile" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor={INK_BRIGHT} />
          <stop offset="1" stopColor={INK} />
        </linearGradient>
      </defs>
      <rect width="32" height="32" rx="7.5" fill="url(#munshi-tile)" />
      {BARS.map((height, index) => (
        <rect
          key={index}
          x={START + index * STEP}
          y={BASELINE - height}
          width={BAR_WIDTH}
          height={height}
          rx={BAR_WIDTH / 2}
          fill={index === BARS.length - 1 ? SAFFRON : "#FFFFFF"}
        />
      ))}
    </svg>
  );
}
