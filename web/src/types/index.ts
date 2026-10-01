export interface MarketQuote {
  ticker: string;
  price: number;
  change: number | null;
  pct: number | null;
}

export interface Company {
  ticker: string;
  name: string;
  filings: number;
  forms: string[];
  periods: Array<{
    form: string;
    fiscal_year: number | null;
    period: string | null;
    period_end: string | null;
  }>;
  latest: {
    form: string;
    fiscal_year: number | null;
    period: string | null;
    period_end: string | null;
  } | null;
}

export interface AuthConfig {
  google: boolean;
  apple: boolean;
  tradingview: boolean;
}

export interface AuthSession {
  provider: string | null;
  authenticated: boolean;
}

export interface RouteInfo {
  route: "KNOWN" | "COLD_START" | "AMBIGUOUS";
  ticker: string | null;
}

export interface AuthProvider {
  id: "google" | "apple" | "tradingview" | "dev";
  label: string;
  icon: React.ReactNode;
}