import { LogoMark } from "@/components/brand/logo-mark";
import { cn } from "@/lib/utils";

// The munshi wordmark: "munshi" in lowercase with a saffron headline bar across the tops of the
// letters (the shirorekha of Devanagari script), and मुंशी beneath it. Used wherever the product
// is named on screen.

const SIZES = {
  sm: { word: "text-xl", hindi: "text-[0.6rem]", mark: 28, gap: "gap-2" },
  md: { word: "text-2xl", hindi: "text-[0.7rem]", mark: 34, gap: "gap-2.5" },
  lg: { word: "text-4xl", hindi: "text-sm", mark: 48, gap: "gap-3" },
} as const;

export function Wordmark({
  size = "md",
  withMark = true,
  className,
}: {
  size?: keyof typeof SIZES;
  withMark?: boolean;
  className?: string;
}) {
  const s = SIZES[size];
  return (
    <span
      role="img"
      aria-label="munshi"
      className={cn("inline-flex items-center select-none", s.gap, className)}
    >
      {withMark && <LogoMark size={s.mark} />}
      <span className="inline-flex flex-col items-start leading-none" aria-hidden="true">
        <span className={cn("relative font-bold tracking-tight text-ink", s.word)}>
          <span
            data-testid="wordmark-headline"
            className="absolute top-[0.26em] -right-[0.06em] -left-[0.06em] h-[0.085em] rounded-full bg-saffron"
          />
          <span className="relative">munshi</span>
        </span>
        <span lang="hi" className={cn("mt-1 font-medium tracking-wide text-muted-foreground", s.hindi)}>
          मुंशी
        </span>
      </span>
    </span>
  );
}
