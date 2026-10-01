"use client";

import { useEffect, useState } from "react";
import { FinGraphLogo } from "@/lib/utils";

export function AuthCallbackPage() {
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const handleCallback = async () => {
      // Parse token from URL fragment (implicit flow) or query string
      const source = window.location.hash && window.location.hash.length > 1
        ? window.location.hash.slice(1)
        : window.location.search.replace(/^\?/, "");
      const params = new URLSearchParams(source);

      const provider = (params.get("provider") || "").trim();
      const token = (params.get("access_token") || params.get("id_token") || params.get("code") || "").trim();
      const state = (params.get("state") || "").trim();

      if (!provider || !token) {
        setError("The provider did not return an authentication token.");
        return;
      }

      // Verify state parameter
      let expected = null;
      try {
        expected = sessionStorage.getItem("fin.auth.state");
      } catch {
        // private mode
      }
      if (expected && state && state !== expected) {
        setError("The sign-in response did not match this browser session. Please try again.");
        return;
      }
      try {
        sessionStorage.removeItem("fin.auth.state");
      } catch {
        // already gone
      }

      try {
        const res = await fetch("/api/auth/session", {
          method: "POST",
          headers: { "content-type": "application/json" },
          credentials: "include",
          body: JSON.stringify({ provider, token }),
        });

        if (!res.ok) {
          let detail = `HTTP ${res.status}`;
          try {
            detail = (await res.json()).error || detail;
          } catch {
            // non-JSON
          }
          setError(`The server rejected the sign-in (${detail}).`);
          return;
        }

        window.location.replace("/app");
      } catch {
        setError("Could not reach the FinGraph server. Is it still running?");
      }
    };

    handleCallback();
  }, []);

  return (
    <div className="auth">
      <div className="auth__space" aria-hidden="true">
        <div className="auth__nebula auth__nebula--a" />
        <div className="auth__nebula auth__nebula--b" />
        <div className="auth__moon-glow" />
        <div className="auth__moon" />
      </div>

      <main className="auth__main">
        <div className="auth__card" id="callback-card">
          <div className="auth__brand">
            <FinGraphLogo size={40} className="logo" />
            <span className="auth__brand-name">FinGraph</span>
          </div>

          <h1 className="auth__title" id="callback-title">
            {error ? "Sign-in failed" : "Signing you in…"}
          </h1>
          <p className="auth__sub" id="callback-sub">
            {error ? "You can go back and try again." : "Finishing authentication with your provider."}
          </p>

          {error && (
            <p className="auth__notice" id="callback-error" role="alert">
              {error}
            </p>
          )}

          {error && (
            <a className="oauth" href="/auth" style={{ marginTop: "22px" }}>
              Back to sign-in
            </a>
          )}
        </div>
      </main>
    </div>
  );
}