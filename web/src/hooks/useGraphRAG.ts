"use client";

import useSWR from "swr";
import { api, type Entity, type GraphPayload, type Company, type RouteResponse, type Report } from "@/lib/api";

export function useEntities(params: { q?: string; limit?: number } = {}) {
  const key = params.q ? `/api/entities?q=${encodeURIComponent(params.q)}&limit=${params.limit || 50}` : null;
  const { data, error, isLoading, mutate } = useSWR<{ entities: Entity[] }>(
    key,
    () => api.entities(params),
    {
      revalidateOnFocus: false,
      dedupingInterval: 30_000,
      fallbackData: { entities: [] },
    }
  );

  return {
    entities: data?.entities || [],
    isLoading,
    isError: !!error,
    error: error?.message,
    mutate,
  };
}

export function useGraph(params: { seed?: string; hops?: number; limit?: number } = {}) {
  const hasSeed = !!params.seed;
  const key = hasSeed && params.seed ? `/api/graph?seed=${encodeURIComponent(params.seed)}&hops=${params.hops || 2}&limit=${params.limit || 250}` : null;
  const { data, error, isLoading, mutate } = useSWR<GraphPayload>(
    key,
    () => api.graph(params),
    {
      revalidateOnFocus: false,
      dedupingInterval: 30_000,
      fallbackData: { nodes: [], edges: [], seeds: [] },
    }
  );

  return {
    graph: data || { nodes: [], edges: [], seeds: [] },
    isLoading,
    isError: !!error,
    error: error?.message,
    mutate,
  };
}

export function useRoute(question: string) {
  const key = question ? `/api/route?q=${encodeURIComponent(question)}` : null;
  const { data, error, isLoading } = useSWR<RouteResponse>(
    key,
    () => api.route(question),
    {
      revalidateOnFocus: false,
      dedupingInterval: 60_000,
      fallbackData: { route: "AMBIGUOUS" as const, ticker: null },
    }
  );

  return {
    route: data?.route || "AMBIGUOUS",
    ticker: data?.ticker || null,
    isLoading,
    isError: !!error,
  };
}

export function useCompanies() {
  const { data, error, isLoading, mutate } = useSWR<{ companies: Company[] }>(
    "/api/companies",
    () => api.companies(),
    {
      revalidateOnFocus: false,
      dedupingInterval: 5 * 60_000,
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

export function useReports() {
  const { data, error, isLoading, mutate } = useSWR<{ reports: Report[] }>(
    "/api/reports",
    () => api.reports(),
    {
      revalidateOnFocus: false,
      dedupingInterval: 5 * 60_000,
      fallbackData: { reports: [] },
    }
  );

  return {
    reports: data?.reports || [],
    isLoading,
    isError: !!error,
    error: error?.message,
    mutate,
  };
}

export function useReport(id: string) {
  const key = id ? `/api/reports/${encodeURIComponent(id)}` : null;
  const { data, error, isLoading } = useSWR<Report>(
    key,
    () => api.report(id),
    {
      revalidateOnFocus: false,
    }
  );

  return {
    report: data,
    isLoading,
    isError: !!error,
    error: error?.message,
  };
}