"use client";

import React, { useState, useRef, useEffect, useCallback } from "react";
import { api, type RAGResponse, type Citation, type RouteType } from "@/lib/api";
import { cn } from "@/lib/utils";
import {
  Send,
  Loader2,
  ChevronDown,
  FileText,
  CheckCircle,
  HelpCircle,
  Sparkles,
  ArrowRight,
  Copy,
  ExternalLink,
} from "lucide-react";

const EXAMPLE_QUESTIONS = [
  "What was Apple's revenue in FY2025?",
  "How did Apple's revenue change year over year?",
  "What segments drove Apple's revenue change?",
  "Show the supporting filings for Apple's net sales",
  "Compare Microsoft and NVIDIA revenue growth",
  "Show me Tesla's cash flow trend",
];

const ROUTE_LABELS: Record<RouteType, { label: string; description: string; icon: React.ReactNode }> = {
  KNOWN: {
    label: "Known",
    description: "Answered from the knowledge graph",
    icon: <CheckCircle className="w-4 h-4" />,
  },
  COLD_START: {
    label: "Cold Start",
    description: "Live SEC fetch + synthesis",
    icon: <Sparkles className="w-4 h-4" />,
  },
  AMBIGUOUS: {
    label: "Ambiguous",
    description: "Multiple companies matched",
    icon: <HelpCircle className="w-4 h-4" />,
  },
};

const VERDICT_COLORS: Record<RAGResponse["verdict"], string> = {
  SUFFICIENT: "bg-green-500/20 text-green-400 border-green-500/30",
  PARTIAL: "bg-yellow-500/20 text-yellow-400 border-yellow-500/30",
  INSUFFICIENT: "bg-red-500/20 text-red-400 border-red-500/30",
  UNCERTAIN: "bg-gray-500/20 text-gray-400 border-gray-500/30",
};

const PROVENANCE_COLORS: Record<Citation["provenance"]["tag"], string> = {
  STATED: "bg-blue-500/20 text-blue-400 border-blue-500/30",
  DERIVED: "bg-purple-500/20 text-purple-400 border-purple-500/30",
  INFERRED: "bg-orange-500/20 text-orange-400 border-orange-500/30",
  EXTERNAL: "bg-cyan-500/20 text-cyan-400 border-cyan-500/30",
  GAP: "bg-red-500/20 text-red-400 border-red-500/30",
};

interface AskFinGraphProps {
  onAnswer?: (response: RAGResponse) => void;
  className?: string;
}

export function AskFinGraph({ onAnswer, className }: AskFinGraphProps) {
  const [question, setQuestion] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [response, setResponse] = useState<RAGResponse | null>(null);
  const [routeInfo, setRouteInfo] = useState<{ route: RouteType; ticker: string | null } | null>(null);
  const [showTrace, setShowTrace] = useState(false);
  const [expandedCitations, setExpandedCitations] = useState<Set<string>>(new Set());
  const [copied, setCopied] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const abortControllerRef = useRef<AbortController | null>(null);

  const handleRoute = useCallback(async (q: string) => {
    try {
      const route = await api.route(q);
      setRouteInfo(route);
    } catch {
      setRouteInfo({ route: "AMBIGUOUS", ticker: null });
    }
  }, []);

  useEffect(() => {
    const timer = setTimeout(() => {
      if (question.trim()) handleRoute(question.trim());
    }, 300);
    return () => clearTimeout(timer);
  }, [question, handleRoute]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!question.trim() || isSubmitting) return;

    setIsSubmitting(true);
    abortControllerRef.current = new AbortController();

    try {
      const result = await api.rag.ask(question.trim(), { stream: false });
      setResponse(result);
      onAnswer?.(result);
    } catch (error) {
      console.error("Ask failed:", error);
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleExampleClick = (q: string) => {
    setQuestion(q);
    if (textareaRef.current) {
      textareaRef.current.focus();
    }
  };

  const handleCopy = async () => {
    if (!response) return;
    await navigator.clipboard.writeText(response.answer);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const toggleCitation = (citationId: string) => {
    setExpandedCitations((prev) => {
      const next = new Set(prev);
      if (next.has(citationId)) next.delete(citationId);
      else next.add(citationId);
      return next;
    });
  };

  const followUpQuestions = response ? [
    { label: "Compare with previous year", action: () => handleExampleClick(`Compare ${response.ticker || "this company"}'s ${question.match(/revenue|sales|income/i)?.[0] || "revenue"} with previous year`) },
    { label: "Break down by segment", action: () => handleExampleClick(`Break down ${response.ticker || "this company"}'s ${question.match(/revenue|sales|income/i)?.[0] || "revenue"} by segment`) },
    { label: "Show related metrics", action: () => handleExampleClick(`Show related metrics for ${response.ticker || "this company"}`) },
    { label: "Show supporting evidence", action: () => setShowTrace(true) },
    { label: "Explore in graph", action: () => setShowTrace(true) },
  ] : [];

  return (
    <div className={cn("flex flex-col h-full bg-[#0D0D0E]/60 border border-white/5 rounded-2xl backdrop-blur-sm overflow-hidden", className)}>
      <div className="px-4 py-3 border-b border-white/5">
        <h2 className="text-lg font-semibold text-white flex items-center gap-2">
          <Sparkles className="w-5 h-5 text-[#FF3C00]" />
          Ask FinGraph
        </h2>
      </div>

      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {routeInfo && (
          <div className="flex items-center gap-2 px-3 py-2 bg-white/5 rounded-xl border border-white/5">
            <span className="flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-[#FF3C00]/20 text-[#FF3C00] text-xs font-medium border border-[#FF3C00]/30">
              {ROUTE_LABELS[routeInfo.route].icon}
              <span>{ROUTE_LABELS[routeInfo.route].label}</span>
            </span>
            <span className="text-white/50 text-sm">{ROUTE_LABELS[routeInfo.route].description}</span>
            {routeInfo.ticker && (
              <span className="ml-auto px-2 py-1 rounded-full bg-white/10 text-white/70 text-xs font-mono">
                {routeInfo.ticker}
              </span>
            )}
          </div>
        )}

        {!response && !isSubmitting && (
          <div className="space-y-3 animate-in fade-in duration-300">
            <p className="text-white/40 text-sm">Try one of these questions:</p>
            <div className="grid gap-2 sm:grid-cols-2">
              {EXAMPLE_QUESTIONS.map((q, i) => (
                <button
                  key={i}
                  onClick={() => handleExampleClick(q)}
                  className="text-left px-3 py-2.5 bg-white/5 hover:bg-white/10 border border-white/5 rounded-xl text-white/70 hover:text-white text-sm transition-all"
                >
                  {q}
                </button>
              ))}
            </div>
          </div>
        )}

        <form onSubmit={handleSubmit} className="space-y-3">
          <div className="relative">
            <textarea
              ref={textareaRef}
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              placeholder={isSubmitting ? "Thinking…" : "Ask a financial question…"}
              rows={3}
              className="w-full px-4 py-3 bg-white/5 border border-white/10 rounded-xl text-white placeholder-white/30 focus:border-[#FF3C00]/50 focus:outline-none focus:ring-1 focus:ring-[#FF3C00]/50 resize-none transition-all"
              disabled={isSubmitting}
              aria-label="Your financial question"
            />
            {isSubmitting && (
              <div className="absolute bottom-3 right-3 flex items-center gap-2 text-white/50 text-xs">
                <Loader2 className="w-4 h-4 animate-spin text-[#FF3C00]" />
                <span>Analyzing…</span>
              </div>
            )}
          </div>
          <button
            type="submit"
            disabled={!question.trim() || isSubmitting}
            className={cn(
              "w-full px-4 py-3 rounded-xl font-medium text-sm transition-all flex items-center justify-center gap-2",
              question.trim() && !isSubmitting
                ? "bg-[#FF3C00] text-white hover:bg-[#FF3C00]/90 shadow-[0_0_20px_rgba(255,60,0,0.3)]"
                : "bg-white/5 text-white/30 cursor-not-allowed"
            )}
          >
            {isSubmitting ? (
              <>
                <Loader2 className="w-4 h-4 animate-spin" />
                Analyzing…
              </>
            ) : (
              <>
                <Send className="w-4 h-4" />
                Ask
              </>
            )}
          </button>
        </form>

        {response && (
          <div className="space-y-4 animate-in slide-in-from-bottom-4 duration-300">
            <div className="flex items-start justify-between gap-4">
              <div className={cn(
                "px-3 py-1.5 rounded-full text-xs font-medium border",
                VERDICT_COLORS[response.verdict]
              )}>
                {response.verdict}
              </div>
              <div className="flex items-center gap-2">
                <button
                  onClick={handleCopy}
                  className="p-2 rounded-lg bg-white/5 hover:bg-white/10 text-white/50 hover:text-white transition-colors"
                  aria-label="Copy answer"
                >
                  {copied ? <CheckCircle className="w-4 h-4 text-green-400" /> : <Copy className="w-4 h-4" />}
                </button>
                <button
                  onClick={() => setShowTrace(!showTrace)}
                  className="p-2 rounded-lg bg-white/5 hover:bg-white/10 text-white/50 hover:text-white transition-colors"
                  aria-label="View trace"
                >
                  <ExternalLink className="w-4 h-4" />
                </button>
              </div>
            </div>

            <div className="prose prose-invert max-w-none text-white/90 leading-relaxed">
              {response.answer.split("\n").map((paragraph, i) => (
                <p key={i} className="mb-4 whitespace-pre-wrap">{paragraph}</p>
              ))}
            </div>

            {response.citations.length > 0 && (
              <div className="space-y-2 border-t border-white/5 pt-4">
                <h3 className="text-sm font-semibold text-white/70 flex items-center gap-2">
                  <FileText className="w-4 h-4" />
                  Evidence ({response.citations.length})
                </h3>
                <div className="space-y-2">
                  {response.citations.map((citation) => {
                    const isExpanded = expandedCitations.has(citation.id);
                    return (
                      <div
                        key={citation.id}
                        className={cn(
                          "rounded-xl border transition-all overflow-hidden",
                          isExpanded ? "border-white/10 bg-white/5" : "border-white/5 bg-white/3"
                        )}
                      >
                        <button
                          onClick={() => toggleCitation(citation.id)}
                          className="w-full px-4 py-3 flex items-center gap-3 text-left"
                        >
                          <span className={cn(
                            "px-2 py-1 rounded text-[10px] font-medium border flex-shrink-0",
                            PROVENANCE_COLORS[citation.provenance.tag]
                          )}>
                            {citation.provenance.tag}
                          </span>
                          <span className="flex-1 min-w-0 text-sm font-medium text-white/90 truncate">
                            {citation.entity_name}
                          </span>
                          {citation.filing_form && citation.fiscal_year && (
                            <span className="px-2 py-1 rounded text-[10px] font-mono text-white/50 bg-white/5">
                              {citation.filing_form} FY{citation.fiscal_year}
                            </span>
                          )}
                          <ChevronDown className={cn("w-4 h-4 text-white/40 transition-transform", isExpanded && "rotate-180")} />
                        </button>
                        {isExpanded && (
                          <div className="px-4 pb-4 border-t border-white/5 bg-white/3">
                            <p className="text-sm text-white/70 whitespace-pre-wrap">{citation.text}</p>
                            {citation.provenance.explanation && (
                              <p className="text-xs text-white/50 mt-2 italic">
                                {citation.provenance.explanation}
                              </p>
                            )}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

            {followUpQuestions.length > 0 && (
              <div className="space-y-2 border-t border-white/5 pt-4">
                <h3 className="text-sm font-semibold text-white/70 flex items-center gap-2">
                  <ArrowRight className="w-4 h-4" />
                  Follow-up
                </h3>
                <div className="flex flex-wrap gap-2">
                  {followUpQuestions.map((q, i) => (
                    <button
                      key={i}
                      onClick={q.action}
                      className="px-3 py-1.5 bg-white/5 hover:bg-white/10 border border-white/5 rounded-xl text-white/70 hover:text-white text-xs transition-all"
                    >
                      {q.label}
                    </button>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        {isSubmitting && !response && (
          <div className="flex flex-col items-center justify-center py-8 text-white/40">
            <Loader2 className="w-8 h-8 animate-spin text-[#FF3C00] mb-3" />
            <p className="text-sm">Retrieving evidence and synthesizing answer…</p>
          </div>
        )}
      </div>
    </div>
  );
}