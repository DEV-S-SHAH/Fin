import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatPrice(value: number | null, digits = 2): string {
  return value == null ? "—" : Number(value).toFixed(digits);
}

export function formatSigned(value: number | null, digits = 2): string {
  if (value == null) return "—";
  return (value > 0 ? "+" : "") + Number(value).toFixed(digits);
}

export function formatPct(value: number | null): string {
  if (value == null) return "—";
  return (value > 0 ? "+" : "") + Number(value).toFixed(2) + "%";
}

export function getDirection(pct: number | null): "up" | "down" | "flat" {
  if (pct == null || pct === 0) return "flat";
  return pct > 0 ? "up" : "down";
}

export const ARROW_UP = (
  <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
    <path d="M12 5l7 9H5z" />
  </svg>
);

export const ARROW_DOWN = (
  <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
    <path d="M12 19l-7-9h14z" />
  </svg>
);

export const ARROW_FLAT = (
  <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
    <path d="M5 11h14v2H5z" />
  </svg>
);

export function getArrow(dir: "up" | "down" | "flat") {
  return dir === "up" ? ARROW_UP : dir === "down" ? ARROW_DOWN : ARROW_FLAT;
}

export function FinGraphLogo({ size = 32, className }: { size?: number; className?: string }) {
  const nodes = [
    [16, 20, 7.5, "main"],
    [7, 9, 3.6, "sat"],
    [26, 8, 3.2, "sat"],
    [25.5, 22, 2.6, "sat"],
  ] as const;
  const links = [
    [16, 20, 7, 9],
    [16, 20, 26, 8],
    [16, 20, 25.5, 22],
  ] as const;
  const dots = [
    [11.5, 23.5, 1.7],
    [21, 12.5, 1.5],
  ] as const;

  const linkLines = links.map(([x1, y1, x2, y2]) => (
    <line
      key={`${x1}-${y1}-${x2}-${y2}`}
      className="logo__link"
      x1={x1}
      y1={y1}
      x2={x2}
      y2={y2}
      vectorEffect="non-scaling-stroke"
    />
  ));

  const nodeEls = nodes.map(([x, y, r, kind]) => (
    <circle key={`${x}-${y}`} className={`logo__node--${kind}`} cx={x} cy={y} r={r} />
  ));

  const dotEls = dots.map(([x, y, r]) => (
    <circle key={`${x}-${y}`} className="logo__node--dot" cx={x} cy={y} r={r} />
  ));

  return (
    <svg
      className={`logo__svg ${className || ""}`}
      viewBox={`0 0 ${size} ${size}`}
      fill="none"
      aria-hidden="true"
      focusable="false"
    >
      {linkLines}
      {nodeEls}
      {dotEls}
    </svg>
  );
}