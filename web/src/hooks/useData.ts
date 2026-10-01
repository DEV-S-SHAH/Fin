"use client";

import useSWR from "swr";
import { api } from "@/lib/api";
import type { MarketQuote, Company } from "@/types";

const MARKETS_REFRESH_MS = 60_000;

export function useMarkets() {
  const { data, error, isLoading, mutate } = useSWR<{ markets: MarketQuote[] }>(
    "/api/markets",
    () => api.markets(),
    {
      refreshInterval: MARKETS_REFRESH_MS,
      revalidateOnFocus: false,
      dedupingInterval: MARKETS_REFRESH_MS,
      fallbackData: { markets: [] },
    }
  );

  return {
    markets: data?.markets || [],
    isLoading,
    isError: !!error,
    error: error?.message,
    mutate,
  };
}

export function useCompanies() {
  const { data, error, isLoading, mutate } = useSWR<{ companies: Company[] }>(
    "/api/companies",
    () => api.companies(),
    {
      revalidateOnFocus: false,
      dedupingInterval: 5 * 60_000, // 5 minutes
      fallbackData: { companies: [] },
    }
  );

  return {
    companies: data?.companies || [],
    isLoading,
    isError: !!error,
    error: error?.message,
    mutate,
  };
}