"use client";

import React from "react";
import { motion } from "motion/react";
import { cn } from "@/lib/utils";

interface GradientBarsBackgroundProps {
  className?: string;
  children?: React.ReactNode;
  barCount?: number;
  primaryColor?: string;
  secondaryColor?: string;
}

export function GradientBarsBackground({
  className,
  children,
  barCount = 18,
  primaryColor = "#FF3C00",
  secondaryColor = "#FF6B35",
}: GradientBarsBackgroundProps) {
  const bars = Array.from({ length: barCount }, (_, i) => i);

  return (
    <div
      className={cn(
        "relative min-h-[480px] w-full overflow-hidden bg-[#030405] text-[#F5F7FA]",
        className
      )}
    >
      {/* Background gradient bars layer */}
      <div className="pointer-events-none absolute inset-0 flex items-center justify-around opacity-35 blur-[32px]">
        {bars.map((i) => {
          const isPrimary = i % 2 === 0;
          const delay = (i * 0.15) % 2;
          const duration = 4 + (i % 3);

          return (
            <motion.div
              key={i}
              initial={{ scaleY: 0.6, opacity: 0.3 }}
              animate={{
                scaleY: [0.6, 1.25, 0.6],
                opacity: [0.3, 0.7, 0.3],
                translateY: ["-10%", "10%", "-10%"],
              }}
              transition={{
                duration,
                repeat: Infinity,
                ease: "easeInOut",
                delay,
              }}
              style={{
                background: `linear-gradient(180deg, ${isPrimary ? primaryColor : secondaryColor} 0%, transparent 100%)`,
                width: `${100 / (barCount * 1.5)}%`,
                height: "120%",
                borderRadius: "9999px",
              }}
            />
          );
        })}
      </div>

      {/* Radial depth vignette */}
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(ellipse_80%_80%_at_50%_50%,transparent_20%,#030405_95%)]" />

      {/* Content wrapper */}
      <div className="relative z-10">{children}</div>
    </div>
  );
}
