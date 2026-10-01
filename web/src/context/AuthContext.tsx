"use client";

import React, { createContext, useContext, useEffect, useState, useCallback } from "react";
import { api } from "@/lib/api";
import type { AuthConfig, AuthSession } from "@/types";

interface AuthContextType {
  config: AuthConfig | null;
  session: AuthSession | null;
  loading: boolean;
  error: string | null;
  login: (provider: "google" | "apple" | "tradingview" | "dev") => Promise<void>;
  logout: () => Promise<void>;
  refreshSession: () => Promise<void>;
  enabledProviders: string[];
  oauthConfig: typeof OAUTH_CONFIG;
}

const AuthContext = createContext<AuthContextType | null>(null);

const OAUTH_CONFIG = {
  google: {
    label: "Google",
    url: (clientId: string, state: string, redirectUri: string) =>
      "https://accounts.google.com/o/oauth2/v2/auth"
      + `?client_id=${encodeURIComponent(clientId)}`
      + `&redirect_uri=${encodeURIComponent(redirectUri)}`
      + "&response_type=token"
      + "&scope=" + encodeURIComponent("openid email profile")
      + `&state=${encodeURIComponent(state)}`,
    icon: (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path fill="#4285F4" d="M23.5 12.27c0-.85-.08-1.66-.22-2.45H12v4.64h6.45a5.52 5.52 0 0 1-2.39 3.62v3h3.87c2.26-2.09 3.57-5.16 3.57-8.81z"/>
        <path fill="#34A853" d="M12 24c3.24 0 5.96-1.07 7.94-2.91l-3.87-3c-1.07.72-2.44 1.15-4.07 1.15-3.13 0-5.78-2.11-6.73-4.96H1.29v3.1A12 12 0 0 0 12 24z"/>
        <path fill="#FBBC05" d="M5.27 14.28A7.2 7.2 0 0 1 4.89 12c0-.79.14-1.56.38-2.28v-3.1H1.29a12 12 0 0 0 0 10.76l3.98-3.1z"/>
        <path fill="#EA4335" d="M12 4.77c1.76 0 3.34.61 4.58 1.8l3.44-3.44A11.98 11.98 0 0 0 12 0 12 12 0 0 0 1.29 6.62l3.98 3.1C6.22 6.88 8.87 4.77 12 4.77z"/>
      </svg>
    ),
  },
  apple: {
    label: "Apple",
    url: (clientId: string, state: string, redirectUri: string) =>
      "https://appleid.apple.com/auth/authorize"
      + `?client_id=${encodeURIComponent(clientId)}`
      + `&redirect_uri=${encodeURIComponent(redirectUri)}`
      + "&response_type=" + encodeURIComponent("code id_token")
      + "&scope=" + encodeURIComponent("name email")
      + "&response_mode=fragment"
      + `&state=${encodeURIComponent(state)}`,
    icon: (
      <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
        <path d="M17.05 12.54c-.03-2.89 2.36-4.27 2.47-4.34-1.35-1.97-3.44-2.24-4.18-2.27-1.78-.18-3.47 1.05-4.37 1.05-.9 0-2.29-1.02-3.77-1-1.94.03-3.72 1.13-4.72 2.86-2.01 3.49-.51 8.66 1.45 11.5.96 1.39 2.1 2.94 3.6 2.88 1.45-.06 2-.93 3.74-.93s2.24.93 3.77.9c1.56-.03 2.55-1.41 3.5-2.8 1.1-1.61 1.55-3.17 1.58-3.25-.04-.02-3.03-1.16-3.07-4.6zM14.16 4.06c.8-.97 1.34-2.32 1.19-3.66-1.15.05-2.55.77-3.38 1.74-.74.86-1.39 2.23-1.22 3.55 1.29.1 2.6-.65 3.41-1.63z"/>
      </svg>
    ),
  },
  tradingview: {
    label: "TradingView",
    url: (clientId: string, state: string, redirectUri: string) =>
      "https://www.tradingview.com/authorize/"
      + `?client_id=${encodeURIComponent(clientId)}`
      + `&redirect_uri=${encodeURIComponent(redirectUri)}`
      + "&response_type=token"
      + "&scope=read"
      + `&state=${encodeURIComponent(state)}`,
    icon: (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="7.5" cy="16.5" r="4.5" fill="none" stroke="#2962FF" strokeWidth="2.2"/>
        <circle cx="16.5" cy="7.5" r="4.5" fill="none" stroke="#3BB3E3" strokeWidth="2.2"/>
        <path d="M10.5 13.5 13.5 10.5" stroke="#2962FF" strokeWidth="2.2" strokeLinecap="round"/>
      </svg>
    ),
  },
  dev: {
    label: "Development",
    url: null,
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
        <path d="M12 3l7 4v5c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V7z" strokeLinejoin="round"/>
      </svg>
    ),
  },
} as const;

export function AuthProviderComponent({ children }: { children: React.ReactNode }) {
  const [config, setConfig] = useState<AuthConfig | null>(null);
  const [session, setSession] = useState<AuthSession | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadConfig = useCallback(async () => {
    try {
      const data = await api.auth.config();
      setConfig(data);
    } catch (err) {
      console.error("Failed to load auth config:", err);
      setConfig({ google: false, apple: false, tradingview: false });
    }
  }, []);

  const refreshSession = useCallback(async () => {
    try {
      const data = await api.auth.session();
      setSession(data);
    } catch (err) {
      console.error("Failed to refresh session:", err);
      setSession({ provider: null, authenticated: false });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadConfig();
    refreshSession();
  }, [loadConfig, refreshSession]);

  const login = useCallback(async (provider: "google" | "apple" | "tradingview" | "dev") => {
    setError(null);

    if (provider === "dev") {
      try {
        await api.auth.login("dev", "local");
        await refreshSession();
        window.location.href = "/app";
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to sign in locally");
      }
      return;
    }

    if (!config?.[provider]) {
      setError(`${provider} is not configured`);
      return;
    }

    const state = crypto.randomUUID();
    try {
      sessionStorage.setItem("fin.auth.state", state);
    } catch {
      // private mode
    }

    const oauth = OAUTH_CONFIG[provider];
    if (!oauth.url) {
      setError("Invalid provider");
      return;
    }

    // We need the client ID from the server. The server doesn't expose it directly,
    // so we need to get it from the config endpoint. However, the config only returns
    // boolean flags. For now, we'll use the dev flow or require the client ID to be
    // available. In a real implementation, the server would provide the authorize URL.
    // For this implementation, we'll simulate by checking if we have a configured provider
    // and redirecting to a generic OAuth flow.

    // Since the server doesn't expose client IDs, we'll use a different approach:
    // The auth page will fetch the config and if a provider is enabled, it will
    // redirect to the provider's OAuth URL with a placeholder. The actual client ID
    // must be configured on the server side.
    // For now, we'll just show an error if not dev mode.
    setError(`OAuth for ${provider} requires server-side client ID configuration. Use "Continue locally (development)" for testing.`);
  }, [config, refreshSession]);

  const logout = useCallback(async () => {
    try {
      await api.auth.logout();
      setSession({ provider: null, authenticated: false });
      window.location.href = "/";
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to logout");
    }
  }, []);

  const enabledProviders = config
    ? (Object.entries(config)
        .filter(([, enabled]) => enabled)
        .map(([id]) => id as keyof typeof OAUTH_CONFIG)
      )
    : [];

  return (
    <AuthContext.Provider
      value={{
        config,
        session,
        loading,
        error,
        login,
        logout,
        refreshSession,
        enabledProviders,
        oauthConfig: OAUTH_CONFIG,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return ctx;
}