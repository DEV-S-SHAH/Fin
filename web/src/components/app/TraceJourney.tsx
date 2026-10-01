"use client";

import React, { useState, useEffect } from "react";
import { motion, AnimatePresence } from "motion/react";
import { cn } from "@/lib/utils";
import type { TraceStep, RouteType } from "@/lib/api";
import {
  CheckCircle,
  Loader2,
  AlertCircle,
  HelpCircle,
  Sparkles,
  Database,
  Search,
  FileText,
  Brain,
  ArrowRight,
  X,
} from "lucide-react";

const STAGE_CONFIG: Record<string, { icon: React.ReactNode; color: string; description: string }> = {
  routing: {
    icon: <HelpCircle className="w-5 h-5" />,
    color: "#8B95A7",
    description: "Determining how to answer your question",
  },
  entity_resolution: {
    icon: <Search className="w-5 h-5" />,
    color: "#FF6B35",
    description: "Identifying companies, metrics, and entities mentioned",
  },
  graph_retrieval: {
    icon: <Database className="w-5 h-5" />,
    color: "#FF3C00",
    description: "Traversing the knowledge graph for relevant connections",
  },
  filing_retrieval: {
    icon: <FileText className="w-5 h-5" />,
    color: "#E63600",
    description: "Fetching source documents and SEC filings",
  },
  evidence: {
    icon: <CheckCircle className="w-5 h-5" />,
    color: "#22C55E",
    description: "Grading evidence with provenance tags",
  },
  synthesis: {
    icon: <Brain className="w-5 h-5" />,
    color: "#FF3C00",
    description: "Synthesizing the final answer with citations",
  },
};

const STAGE_ORDER = [
  "routing",
  "entity_resolution",
  "graph_retrieval",
  "filing_retrieval",
  "evidence",
  "synthesis",
] as const;

const PROVENANCE_COLORS: Record<string, string> = {
  STATED: "bg-blue-500/20 text-blue-400 border-blue-500/30",
  DERIVED: "bg-purple-500/20 text-purple-400 border-purple-500/30",
  INFERRED: "bg-orange-500/20 text-orange-400 border-orange-500/30",
  EXTERNAL: "bg-cyan-500/20 text-cyan-400 border-cyan-500/30",
  GAP: "bg-red-500/20 text-red-400 border-red-500/30",
};

interface TraceData {
  route?: RouteType;
  ticker?: string | null;
  entities?: Array<{ name: string; type: string }>;
  nodes?: number;
  edges?: number;
  hops?: number;
  filings?: string[];
  provenance?: Record<string, number>;
  verdict?: string;
  citations?: number;
}

interface TraceJourneyProps {
  question: string;
  trace: TraceStep[];
  route: RouteType;
  ticker: string | null;
  answer: string;
  onClose: () => void;
  className?: string;
}

export function TraceJourney({
  question,
  trace,
  route,
  ticker,
  answer,
  onClose,
  className,
}: TraceJourneyProps) {
  const [activeStage, setActiveStage] = useState(0);
  const [isAnimating, setIsAnimating] = useState(false);

  const getStageStatus = (stageId: string) => {
    const step = trace.find((s) => s.stage === stageId);
    if (!step) return "pending";
    return step.status;
  };

  const getStageData = (stageId: string): TraceData | undefined => {
    return trace.find((s) => s.stage === stageId)?.data as TraceData | undefined;
  };

  const animateThroughStages = async () => {
    setIsAnimating(true);
    for (let i = 0; i < STAGE_ORDER.length; i++) {
      setActiveStage(i);
      await new Promise((resolve) => setTimeout(resolve, 800));
    }
    setIsAnimating(false);
  };

  useEffect(() => {
    if (trace.length > 0) {
      animateThroughStages();
    }
  }, [trace.length]);

  return (
    <div className={cn("fixed inset-0 z-50 flex flex-col bg-[#030303] overflow-hidden", className)}>
      <div className="flex items-center justify-between px-4 py-3 border-b border-white/5">
        <h2 className="text-lg font-semibold text-white flex items-center gap-2">
          <Sparkles className="w-5 h-5 text-[#FF3C00]" />
          How FinGraph Thinks
        </h2>
        <div className="flex items-center gap-2">
          {ticker && (
            <span className="px-2 py-1 rounded-full bg-[#FF3C00]/20 text-[#FF3C00] text-xs font-mono font-medium border border-[#FF3C00]/30">
              {ticker}
            </span>
          )}
          <span className={cn(
            "px-2 py-1 rounded-full text-xs font-medium border",
            route === "KNOWN" && "bg-green-500/20 text-green-400 border-green-500/30",
            route === "COLD_START" && "bg-[#FF3C00]/20 text-[#FF3C00] border-[#FF3C00]/30",
            route === "AMBIGUOUS" && "bg-yellow-500/20 text-yellow-400 border-yellow-500/30",
          )}>
            {route}
          </span>
          <button onClick={onClose} className="p-2 rounded-lg hover:bg-white/5 text-white/50 transition-colors">
            <X className="w-5 h-5" />
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto p-4 md:p-6 space-y-6">
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          className="max-w-3xl mx-auto space-y-4"
        >
          <div className="bg-white/3 border border-white/5 rounded-2xl p-4">
            <p className="text-white/40 text-sm mb-1">Your Question</p>
            <p className="text-white font-medium">{question}</p>
          </div>
        </motion.div>

        <div className="max-w-3xl mx-auto">
          <AnimatePresence mode="wait">
            {STAGE_ORDER.map((stageId, index) => {
              const config = STAGE_CONFIG[stageId];
              const status = getStageStatus(stageId);
              const data = getStageData(stageId);
              const isActive = isAnimating && index === activeStage;
              const isCompleted = index < activeStage || (index === activeStage && status === "completed");

              return (
                <motion.div
                  key={stageId}
                  initial={{ opacity: 0, x: -20 }}
                  animate={{ opacity: 1, x: 0 }}
                  exit={{ opacity: 0, x: 20 }}
                  transition={{ duration: 0.3, delay: index * 0.1 }}
                  className="relative"
                >
                  {index < STAGE_ORDER.length - 1 && (
                    <motion.div
                      className="absolute left-5 top-12 bottom-0 w-0.5"
                      style={{ background: isCompleted ? config.color : "rgba(255,255,255,0.05)" }}
                      initial={{ height: 0 }}
                      animate={{ height: isCompleted ? "100%" : 0 }}
                      transition={{ duration: 0.5, delay: index * 0.1 + 0.4 }}
                    />
                  )}

                  <div className={cn("flex gap-4 relative", isActive && "animate-pulse")}>
                    <div
                      className={cn(
                        "flex-shrink-0 w-10 h-10 rounded-xl flex items-center justify-center border-2 transition-all",
                        isCompleted
                          ? `bg-${config.color.replace("#", "")}20 border-${config.color} text-${config.color}`
                          : isActive
                          ? `bg-${config.color}20 border-${config.color} text-${config.color} shadow-[0_0_20px_${config.color}40]`
                          : "bg-white/5 border-white/10 text-white/40"
                      )}
                    >
                      {status === "completed" ? (
                        <CheckCircle className="w-5 h-5" />
                      ) : status === "error" ? (
                        <AlertCircle className="w-5 h-5" />
                      ) : status === "active" ? (
                        <Loader2 className="w-5 h-5 animate-spin" />
                      ) : (
                        config.icon
                      )}
                    </div>

                    <motion.div
                      initial={{ opacity: 0, x: 20 }}
                      animate={{ opacity: 1, x: 0 }}
                      className="flex-1 min-w-0 bg-white/3 border border-white/5 rounded-2xl p-4 transition-all"
                      style={{
                        borderColor: isActive ? config.color + "80" : "rgba(255,255,255,0.05)",
                        boxShadow: isActive ? `0 0 20px ${config.color}20` : "none",
                      }}
                    >
                      <div className="flex items-start gap-3">
                        <div className="flex-shrink-0">
                          <h3 className="font-semibold text-white">{stageId.replace("_", " ").replace(/\b\w/g, (c) => c.toUpperCase())}</h3>
                          <p className="text-white/50 text-sm mt-0.5">{config.description}</p>
                        </div>
                        <div className="ml-auto flex items-center gap-2">
                          {status === "active" && <Loader2 className="w-4 h-4 animate-spin text-[#FF3C00]" />}
                          {status === "completed" && <CheckCircle className="w-4 h-4 text-green-400" />}
                          {status === "error" && <AlertCircle className="w-4 h-4 text-red-400" />}
                        </div>
                      </div>

                      {data && (
                        <AnimatePresence mode="wait">
                          <motion.div
                            initial={{ opacity: 0, height: 0 }}
                            animate={{ opacity: 1, height: "auto" }}
                            exit={{ opacity: 0, height: 0 }}
                            className="mt-4 pt-4 border-t border-white/5 space-y-3"
                          >
                            {stageId === "routing" && data.route && (
                              <div className="grid gap-2 sm:grid-cols-2 text-sm">
                                <div className="bg-white/5 rounded-xl p-3">
                                  <p className="text-white/40 text-xs">Route</p>
                                  <p className="text-white font-mono capitalize">{data.route.toLowerCase()}</p>
                                </div>
                                <div className="bg-white/5 rounded-xl p-3">
                                  <p className="text-white/40 text-xs">Ticker</p>
                                  <p className="text-white font-mono">{data.ticker || "—"}</p>
                                </div>
                              </div>
                            )}
                            {stageId === "entity_resolution" && data.entities && (
                              <div className="flex flex-wrap gap-2">
                                {data.entities.slice(0, 8).map((e, i) => (
                                  <span key={i} className="px-2 py-1 rounded bg-white/5 text-white/70 text-xs font-medium">
                                    {e.name} ({e.type})
                                  </span>
                                ))}
                              </div>
                            )}
                            {stageId === "graph_retrieval" && (
                              <div className="grid gap-2 sm:grid-cols-3 text-sm">
                                <div className="bg-white/5 rounded-xl p-3">
                                  <p className="text-white/40 text-xs">Nodes Retrieved</p>
                                  <p className="text-white font-mono text-xl">{data.nodes || 0}</p>
                                </div>
                                <div className="bg-white/5 rounded-xl p-3">
                                  <p className="text-white/40 text-xs">Edges</p>
                                  <p className="text-white font-mono text-xl">{data.edges || 0}</p>
                                </div>
                                <div className="bg-white/5 rounded-xl p-3">
                                  <p className="text-white/40 text-xs">Hops</p>
                                  <p className="text-white font-mono text-xl">{data.hops || 0}</p>
                                </div>
                              </div>
                            )}
                            {stageId === "filing_retrieval" && data.filings && (
                              <div className="flex flex-wrap gap-2">
                                {data.filings.slice(0, 6).map((f, i) => (
                                  <span key={i} className="px-2 py-1 rounded bg-white/5 text-white/70 text-xs font-mono">
                                    {f}
                                  </span>
                                ))}
                              </div>
                            )}
                            {stageId === "evidence" && data.provenance && (
                              <div className="flex flex-wrap gap-2">
                                {(Object.entries(data.provenance) as [string, number][]).map(([tag, count]) => (
                                  <span key={tag} className={cn(
                                    "px-2 py-1 rounded text-[10px] font-medium border",
                                    PROVENANCE_COLORS[tag] || "bg-white/10 text-white/50 border-white/10"
                                  )}>
                                    {tag}: {count}
                                  </span>
                                ))}
                              </div>
                            )}
                            {stageId === "synthesis" && (
                              <div className="grid gap-2 sm:grid-cols-2 text-sm">
                                <div className="bg-white/5 rounded-xl p-3">
                                  <p className="text-white/40 text-xs">Verdict</p>
                                  <p className="text-white font-medium">{data.verdict}</p>
                                </div>
                                <div className="bg-white/5 rounded-xl p-3">
                                  <p className="text-white/40 text-xs">Citations</p>
                                  <p className="text-white font-mono">{data.citations || 0}</p>
                                </div>
                              </div>
                            )}
                          </motion.div>
                        </AnimatePresence>
                      )}

                      {trace.find((s) => s.stage === stageId)?.duration_ms && (
                        <div className="mt-3 text-xs text-white/40 flex items-center gap-1">
                          <span>⏱</span>
                          <span>{trace.find((s) => s.stage === stageId)!.duration_ms}ms</span>
                        </div>
                      )}
                    </motion.div>
                  </div>
                </motion.div>
              );
            })}
          </AnimatePresence>

          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: STAGE_ORDER.length * 0.1 + 0.3 }}
            className="max-w-3xl mx-auto mt-6 bg-white/3 border border-white/5 rounded-2xl p-4"
          >
            <div className="flex items-center justify-between mb-3">
              <h3 className="font-semibold text-white flex items-center gap-2">
                <ArrowRight className="w-5 h-5 text-[#FF3C00]" />
                Final Answer
              </h3>
            </div>
            <div className="prose prose-invert max-w-none text-white/90 leading-relaxed">
              {answer.split("\n").map((paragraph, i) => (
                <p key={i} className="mb-4 whitespace-pre-wrap">{paragraph}</p>
              ))}
            </div>
          </motion.div>
        </div>
      </div>
    </div>
  );
}