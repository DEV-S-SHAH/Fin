"use client";

import React, { useEffect, useState } from "react";
import { useAuth } from "@/context/AuthContext";
import { FinGraphLogo } from "@/lib/utils";
import { cn } from "@/lib/utils";

export function AuthPage() {
  const { config, login, enabledProviders, oauthConfig, loading, error } = useAuth();
  const [reducedMotion] = useState(() =>
    typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
  const [devMode, setDevMode] = useState(false);

  useEffect(() => {
    if (!loading && config && enabledProviders.length === 0) {
      setDevMode(true);
    }
  }, [loading, config, enabledProviders]);

  const handleOAuthClick = (provider: string) => {
    const oauth = oauthConfig[provider as keyof typeof oauthConfig];
    if (!oauth?.url) return;

    alert(`${provider} OAuth requires server-side configuration. Use "Continue locally (development)" for testing.`);
  };

  return (
    <div className="auth">
      <a className="skip-link" href="#auth-card">
        Skip to sign-in
      </a>

      {!reducedMotion && (
        <div className="auth__space" aria-hidden="true">
          <StarsCanvas />
          <div className="auth__nebula auth__nebula--a" />
          <div className="auth__nebula auth__nebula--b" />
        </div>
      )}

      <main className="auth__main" id="top">
        <a className="auth__back" href="/">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden="true">
            <path d="M19 12H5M11 6l-6 6 6 6" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <span>Back to home</span>
        </a>

        <div className="auth__card" id="auth-card">
          <div className="auth__brand">
            <FinGraphLogo size={40} className="logo" />
            <span className="auth__brand-name">FinGraph</span>
          </div>

          <h1 className="auth__title">Welcome to FinGraph</h1>
          <p className="auth__sub">Sign in to continue to your workspace.</p>

          <div className="auth__notice" id="auth-notice" role="status" aria-live="polite" hidden>
            {error}
          </div>

          <div className="auth__providers" id="providers">
            {enabledProviders.map((provider) => {
              const oauth = oauthConfig[provider as keyof typeof oauthConfig];
              return (
                <button
                  key={provider}
                  className="oauth"
                  type="button"
                  data-provider={provider}
                  onClick={() => handleOAuthClick(provider)}
                  disabled={loading}
                >
                  <span className="oauth__icon">{oauth.icon}</span>
                  <span>Continue with {oauth.label}</span>
                </button>
              );
            })}
          </div>

          {devMode && (
            <div className="auth__dev" id="auth-dev">
              <p className="auth__dev-note">
                No OAuth providers are configured. Set
                <code>FINGRAPH_GOOGLE_CLIENT_ID</code>,
                <code>FINGRAPH_APPLE_CLIENT_ID</code> or
                <code>FINGRAPH_TRADINGVIEW_CLIENT_ID</code> to enable the official flows.
              </p>
              <button
                className={cn("oauth", "oauth--dev")}
                type="button"
                data-provider="dev"
                onClick={() => login("dev")}
                disabled={loading}
              >
                <span className="oauth__icon">{oauthConfig.dev.icon}</span>
                <span>Continue locally (development)</span>
              </button>
            </div>
          )}

          <p className="auth__legal">
            By continuing you agree to FinGraph&apos;s
            <a href="/">Terms</a> and <a href="/">Privacy Policy</a>.
          </p>
        </div>
      </main>
    </div>
  );
}

function StarsCanvas() {
  const canvasRef = React.useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    let stars: Array<{
      x: number;
      y: number;
      r: number;
      base: number;
      amp: number;
      speed: number;
      phase: number;
      drift: number;
    }> = [];
    let w = 0;
    let h = 0;

    const resize = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      w = canvas.clientWidth;
      h = canvas.clientHeight;
      canvas.width = w * dpr;
      canvas.height = h * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const count = Math.round((w * h) / 9000);
      stars = Array.from({ length: count }, () => ({
        x: Math.random() * w,
        y: Math.random() * h,
        r: Math.random() * 1.1 + 0.3,
        base: Math.random() * 0.5 + 0.15,
        amp: Math.random() * 0.3,
        speed: Math.random() * 0.9 + 0.25,
        phase: Math.random() * Math.PI * 2,
        drift: Math.random() * 0.008 + 0.002,
      }));
    };

    const frame = (t: number) => {
      ctx.clearRect(0, 0, w, h);
      for (const s of stars) {
        const twinkle = s.base + s.amp * Math.sin(s.phase + t * 0.001 * s.speed);
        ctx.globalAlpha = Math.max(0.05, Math.min(1, twinkle));
        ctx.fillStyle = "#e8e8ec";
        ctx.beginPath();
        ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
        ctx.fill();
        s.y -= s.drift;
        if (s.y < -2) s.y = h + 2;
      }
      ctx.globalAlpha = 1;
      requestAnimationFrame(frame);
    };

    resize();
    const rafId = requestAnimationFrame(frame);
    window.addEventListener("resize", resize, { passive: true });

    return () => {
      cancelAnimationFrame(rafId);
      window.removeEventListener("resize", resize);
    };
  }, []);

  return <canvas ref={canvasRef} className="auth__stars" id="stars" />;
}