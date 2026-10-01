"use client";

import { useState, useEffect, useCallback } from "react";
import { useAuth } from "@/context/AuthContext";
import { useEntities, useGraph } from "@/hooks/useGraphRAG";
import { KnowledgeGraph } from "@/components/app/KnowledgeGraph";
import { EntityBrowser } from "@/components/app/EntityBrowser";
import { AskFinGraph } from "@/components/app/AskFinGraph";
import { TraceJourney } from "@/components/app/TraceJourney";
import { FinGraphLogo } from "@/lib/utils";
import { cn } from "@/lib/utils";
import { ChevronLeft, ChevronRight, Sparkles, BarChart3, X, Maximize2 } from "lucide-react";
import type { Entity, EntityType } from "@/lib/api";

interface RAGResponse {
  answer: string;
  chunks: any[];
  citations: any[];
  verdict: string;
  route: string;
  ticker: string | null;
  trace?: any[];
  timings?: Record<string, number>;
}

export function AppPage() {
  const { session, logout } = useAuth();
  const [selectedEntity, setSelectedEntity] = useState<Entity | null>(null);
  const [searchTerm, setSearchTerm] = useState("");
  const [filterType, setFilterType] = useState<"all" | EntityType>("all");
  const [response, setResponse] = useState<RAGResponse | null>(null);
  const [showTrace, setShowTrace] = useState(false);
  const [showGraphFullscreen, setShowGraphFullscreen] = useState(false);
  const [lastQuestion, setLastQuestion] = useState("");
  const [isMobile, setIsMobile] = useState(false);
  const [leftPanelOpen, setLeftPanelOpen] = useState(true);
  const [rightPanelOpen, setRightPanelOpen] = useState(true);

  const { entities } = useEntities({ q: searchTerm, limit: 200 });
  const { graph, isLoading: graphLoading } = useGraph({
    seed: selectedEntity?.id,
    hops: 2,
    limit: 250,
  });

  useEffect(() => {
    const handleResize = () => setIsMobile(window.innerWidth < 1024);
    handleResize();
    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, []);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "/" && document.activeElement?.tagName !== "INPUT" && document.activeElement?.tagName !== "TEXTAREA") {
        e.preventDefault();
        const searchInput = document.querySelector('#entity-search') as HTMLInputElement;
        searchInput?.focus();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);

  const handleAnswer = useCallback((ragResponse: RAGResponse) => {
    setResponse(ragResponse);
    setLastQuestion(ragResponse.answer);
    if (ragResponse.trace) {
      setShowTrace(true);
    }
  }, []);

  const citedNodeIds = new Set(response?.citations?.map((c: any) => c.entity_id) || []);
  const highlightedNodeIds = new Set(selectedEntity ? [selectedEntity.id] : []);

  return (
    <div className="app h-svh flex flex-col bg-[#030303]">
      <header className={cn(
        "fixed top-0 left-0 right-0 z-40 flex items-center justify-between px-4 py-3 bg-[#030303]/95 backdrop-blur-sm border-b border-white/5",
        isMobile && "px-3 py-2"
      )}>
        <div className="flex items-center gap-3">
          <button
            onClick={() => setLeftPanelOpen(!leftPanelOpen)}
            className={cn(
              "p-2 rounded-lg transition-colors",
              isMobile ? "text-white/60 hover:text-white hover:bg-white/10" : "hidden md:flex"
            )}
            aria-label="Toggle entity browser"
          >
            <ChevronLeft className="w-5 h-5" />
          </button>
          <FinGraphLogo size={28} className="text-[#FF3C00]" />
          <span className="hidden sm:block font-semibold text-white">FinGraph Studio</span>
        </div>

        <div className="flex items-center gap-3 flex-1 max-w-2xl mx-4">
          <div className="relative flex-1">
            <Sparkles className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-white/30" />
            <input
              id="entity-search"
              type="search"
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              placeholder="Search entities, metrics, filings… (press / to focus)"
              className="w-full px-10 py-2.5 pl-10 bg-white/5 border border-white/10 rounded-xl text-white placeholder-white/30 focus:border-[#FF3C00]/50 focus:outline-none focus:ring-1 focus:ring-[#FF3C00]/50 transition-all text-sm"
              aria-label="Search entities"
            />
            {searchTerm && (
              <button
                onClick={() => setSearchTerm("")}
                className="absolute right-3 top-1/2 -translate-y-1/2 p-1 text-white/30 hover:text-white transition-colors"
                aria-label="Clear search"
              >
                <X className="w-4 h-4" />
              </button>
            )}
          </div>
        </div>

        <div className="flex items-center gap-2">
          {session && (
            <>
              <span className="hidden sm:block px-2 py-1 rounded-full bg-white/5 text-white/60 text-xs font-medium border border-white/5">
                {session.provider}
              </span>
              <button
                onClick={() => logout()}
                className={cn(
                  "px-3 py-1.5 rounded-lg text-sm font-medium transition-all",
                  isMobile ? "text-white/60 hover:text-white hover:bg-white/10" : "bg-white/5 text-white/70 hover:bg-white/10 hover:text-white border border-white/10"
                )}
              >
                Sign out
              </button>
            </>
          )}
        </div>
      </header>

      {showTrace && response && (
        <TraceJourney
          question={lastQuestion}
          trace={response.trace || []}
          route={response.route as any}
          ticker={response.ticker}
          answer={response.answer}
          onClose={() => setShowTrace(false)}
        />
      )}

      <main className={cn(
        "flex-1 flex overflow-hidden pt-16 pb-4",
        isMobile && "pt-14"
      )}>
        {!isMobile && leftPanelOpen && (
          <aside className={cn(
            "w-80 flex-shrink-0 flex flex-col bg-[#0D0D0E]/60 border-r border-white/5 backdrop-blur-sm transition-all duration-300",
            showGraphFullscreen && "w-0 overflow-hidden"
          )}>
            <div className="p-3 border-b border-white/5 flex items-center justify-between">
              <h3 className="font-semibold text-white text-sm">Entities</h3>
              <span className="px-2 py-0.5 rounded-full bg-white/5 text-white/50 text-[10px] font-mono">
                {entities.length}
              </span>
            </div>
            <EntityBrowser
              selectedEntity={selectedEntity}
              onSelectEntity={setSelectedEntity}
              searchTerm={searchTerm}
              onSearchChange={setSearchTerm}
              filterType={filterType}
              onFilterChange={setFilterType}
              className="flex-1 min-h-0"
            />
          </aside>
        )}

        <section className={cn(
          "flex-1 flex flex-col min-w-0 relative",
          showGraphFullscreen ? "fixed inset-16 z-50 bg-[#030303]" : ""
        )}>
          <div className={cn(
            "flex-1 flex flex-col relative",
            showGraphFullscreen && "h-svh"
          )}>
            <div className="flex items-center justify-between px-4 py-3 border-b border-white/5 bg-[#0D0D0E]/60 backdrop-blur-sm">
              <h3 className="font-semibold text-white text-sm flex items-center gap-2">
                <BarChart3 className="w-4 h-4 text-[#FF3C00]" />
                Knowledge Graph
              </h3>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => setShowGraphFullscreen(!showGraphFullscreen)}
                  className="p-2 rounded-lg hover:bg-white/10 text-white/50 hover:text-white transition-colors"
                  aria-label={showGraphFullscreen ? "Exit fullscreen" : "Fullscreen"}
                >
                  <Maximize2 className="w-4 h-4" />
                </button>
              </div>
            </div>

            <div className="flex-1 min-h-0 relative">
              <KnowledgeGraph
                data={graph}
                citedNodes={citedNodeIds}
                selectedNode={selectedEntity?.id || null}
                highlightedNodes={highlightedNodeIds}
                onNodeClick={setSelectedEntity}
                onBackgroundClick={() => setSelectedEntity(null)}
                width={showGraphFullscreen ? window.innerWidth : undefined}
                height={showGraphFullscreen ? window.innerHeight - 80 : undefined}
                showLabels={true}
                className="h-full"
              />

              {graphLoading && (
                <div className="absolute inset-0 flex items-center justify-center bg-[#030303]/80 backdrop-blur-sm z-10">
                  <div className="flex flex-col items-center gap-3 text-white/50">
                    <div className="w-8 h-8 border-2 border-[#FF3C00] border-t-transparent rounded-full animate-spin" />
                    <p className="text-sm">Loading graph…</p>
                  </div>
                </div>
              )}

              {!graph.nodes.length && !graphLoading && (
                <div className="absolute inset-0 flex items-center justify-center text-white/30 p-8">
                  <div className="text-center">
                    <FinGraphLogo size={64} className="mx-auto mb-4 opacity-30" />
                    <p className="text-lg">No graph data</p>
                    <p className="text-sm mt-1">Select an entity from the browser to visualize its connections</p>
                  </div>
                </div>
              )}
            </div>
          </div>
        </section>

        <aside className={cn(
          "w-96 flex-shrink-0 flex flex-col bg-[#0D0D0E]/60 border-l border-white/5 backdrop-blur-sm transition-all duration-300",
          isMobile && !rightPanelOpen && "fixed right-0 top-16 bottom-0 z-40 shadow-xl",
          showGraphFullscreen && "hidden"
        )}>
          <div className="flex-1 min-h-0">
            <AskFinGraph onAnswer={handleAnswer} className="h-full" />
          </div>
        </aside>
      </main>

      {isMobile && (
        <>
          {leftPanelOpen && (
            <div className="fixed inset-0 z-30 bg-black/50" onClick={() => setLeftPanelOpen(false)} />
          )}
          <button
            onClick={() => setLeftPanelOpen(!leftPanelOpen)}
            className={cn(
              "fixed bottom-20 left-4 z-40 p-3 rounded-full bg-[#FF3C00] text-white shadow-lg transition-transform",
              leftPanelOpen && "translate-x-64"
            )}
            aria-label="Toggle entity browser"
          >
            <ChevronLeft className="w-5 h-5" />
          </button>
          <button
            onClick={() => setRightPanelOpen(!rightPanelOpen)}
            className={cn(
              "fixed bottom-20 right-4 z-40 p-3 rounded-full bg-[#FF3C00] text-white shadow-lg transition-transform",
              rightPanelOpen && "translate-x-64"
            )}
            aria-label="Toggle Q&A panel"
          >
            <ChevronRight className="w-5 h-5" />
          </button>
        </>
      )}
    </div>
  );
}