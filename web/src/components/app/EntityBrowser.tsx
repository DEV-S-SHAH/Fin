"use client";

import React, { useState, useMemo, useEffect } from "react";
import { useEntities } from "@/hooks/useGraphRAG";
import { cn } from "@/lib/utils";
import { type Entity, type EntityType } from "@/lib/api";
import { Search, X, ChevronRight, Building2, FileText, BarChart3, Layers, AlertTriangle } from "lucide-react";

const TYPE_ICONS: Record<EntityType, React.ReactElement<any>> = {
  Company: <Building2 className="w-4 h-4" />,
  Filing: <FileText className="w-4 h-4" />,
  FinancialMetric: <BarChart3 className="w-4 h-4" />,
  Segment: <Layers className="w-4 h-4" />,
  DisclosureEvent: <AlertTriangle className="w-4 h-4" />,
  Unspecified: <Building2 className="w-4 h-4" />,
};

const TYPE_COLORS: Record<EntityType, string> = {
  Company: "text-[#FF3C00]",
  Filing: "text-[#FF6B35]",
  FinancialMetric: "text-[#E63600]",
  Segment: "text-[#FF8A50]",
  DisclosureEvent: "text-[#B84A2E]",
  Unspecified: "text-[#8B95A7]",
};

const TYPE_BG: Record<EntityType, string> = {
  Company: "bg-[#FF3C00]/20",
  Filing: "bg-[#FF6B35]/20",
  FinancialMetric: "bg-[#E63600]/20",
  Segment: "bg-[#FF8A50]/20",
  DisclosureEvent: "bg-[#B84A2E]/20",
  Unspecified: "bg-[#8B95A7]/20",
};

const CATEGORY_ORDER: EntityType[] = ["Company", "Filing", "FinancialMetric", "Segment", "DisclosureEvent"];
const FILTER_OPTIONS: ("all" | EntityType)[] = ["all", "Company", "Filing", "FinancialMetric", "Segment", "DisclosureEvent"];

interface EntityBrowserProps {
  selectedEntity?: Entity | null;
  onSelectEntity: (entity: Entity) => void;
  searchTerm?: string;
  onSearchChange: (term: string) => void;
  filterType?: EntityType | "all";
  onFilterChange: (type: EntityType | "all") => void;
  className?: string;
}

export function EntityBrowser({
  selectedEntity,
  onSelectEntity,
  searchTerm = "",
  onSearchChange,
  filterType = "all",
  onFilterChange,
  className,
}: EntityBrowserProps) {
  const [localSearch, setLocalSearch] = useState(searchTerm);
  const [localFilter, setLocalFilter] = useState<EntityType | "all">(filterType);
  const [expandedCategories, setExpandedCategories] = useState<Record<EntityType, boolean>>({
    Company: true,
    Filing: true,
    FinancialMetric: true,
    Segment: true,
    DisclosureEvent: true,
    Unspecified: true,
  });

  const { entities, isLoading } = useEntities({ q: localSearch, limit: 200 });

  useEffect(() => {
    setLocalSearch(searchTerm);
  }, [searchTerm]);

  useEffect(() => {
    setLocalFilter(filterType);
  }, [filterType]);

  const groupedEntities = useMemo(() => {
    const groups: Record<EntityType, Entity[]> = {
      Company: [],
      Filing: [],
      FinancialMetric: [],
      Segment: [],
      DisclosureEvent: [],
      Unspecified: [],
    };

    for (const entity of entities) {
      const type = entity.entity_type as EntityType;
      if (localFilter !== "all" && type !== localFilter) continue;
      groups[type].push(entity);
    }

    return groups;
  }, [entities, localFilter]);

  const handleSearchChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const value = e.target.value;
    setLocalSearch(value);
    onSearchChange(value);
  };

  const toggleCategory = (type: EntityType) => {
    setExpandedCategories((prev) => ({ ...prev, [type]: !prev[type] }));
  };

  if (isLoading) {
    return (
      <div className={cn("space-y-3", className)}>
        <div className="flex items-center gap-2 px-3 py-2 bg-[#0D0D0E]/80 border border-white/5 rounded-xl backdrop-blur-sm">
          <Search className="w-4 h-4 text-white/30" />
          <input
            type="search"
            placeholder="Search entities..."
            className="flex-1 bg-transparent border-none outline-none text-white placeholder-white/30 text-sm"
            disabled
          />
        </div>
        <div className="space-y-2" role="status" aria-label="Loading entities">
          {[...Array(6)].map((_, i) => (
            <div key={i} className="flex items-center gap-3 px-3 py-2 animate-pulse">
              <div className="w-8 h-8 rounded-lg bg-white/5" />
              <div className="flex-1">
                <div className="h-4 w-3/4 bg-white/5 rounded" />
                <div className="h-3 w-1/2 bg-white/5 rounded mt-1" />
              </div>
            </div>
          ))}
        </div>
      </div>
    );
  }

  const totalEntities = Object.values(groupedEntities).flat().length;

  return (
    <div className={cn("flex flex-col h-full", className)}>
      <div className="flex items-center gap-2 px-3 py-2 bg-[#0D0D0E]/80 border border-white/5 rounded-xl backdrop-blur-sm">
        <Search className="w-4 h-4 text-white/30" />
        <input
          type="search"
          value={localSearch}
          onChange={handleSearchChange}
          placeholder="Search entities… (press / to focus)"
          className="flex-1 bg-transparent border-none outline-none text-white placeholder-white/30 text-sm"
          aria-label="Search entities"
        />
        {localSearch && (
          <button
            onClick={() => {
              setLocalSearch("");
              onSearchChange("");
            }}
            className="p-1 text-white/30 hover:text-white transition-colors"
            aria-label="Clear search"
          >
            <X className="w-4 h-4" />
          </button>
        )}
      </div>

      <div className="flex items-center gap-2 px-2 py-1 overflow-x-auto scrollbar-hide" role="tablist" aria-label="Entity type filters">
        {FILTER_OPTIONS.map((type) => (
          <button
            key={type}
            onClick={() => {
              setLocalFilter(type);
              onFilterChange(type);
            }}
            className={cn(
              "flex items-center gap-1 px-3 py-1.5 rounded-full text-xs font-medium whitespace-nowrap transition-all",
              localFilter === type
                ? "bg-[#FF3C00] text-white shadow-[0_0_12px_rgba(255,60,0,0.4)]"
                : "bg-white/5 text-white/60 hover:bg-white/10 hover:text-white"
            )}
            role="tab"
            aria-selected={localFilter === type}
          >
            {type !== "all" && TYPE_ICONS[type]}
            <span>{type === "all" ? "All" : type}</span>
            {type !== "all" && groupedEntities[type].length > 0 && (
              <span className={cn("px-1.5 py-0.5 rounded-full text-[10px]", localFilter === type ? "bg-white/30" : "bg-white/10")}>
                {groupedEntities[type].length}
              </span>
            )}
          </button>
        ))}
      </div>

      <div className="flex-1 overflow-y-auto pr-1 space-y-3" role="listbox" aria-label="Entities">
        {totalEntities === 0 ? (
          <div className="flex flex-col items-center justify-center h-full text-white/30 px-4">
            <Search className="w-10 h-10 mb-2 opacity-30" />
            <p className="text-sm">No matching entities</p>
            <p className="text-xs mt-1">Try a different search term or filter</p>
          </div>
        ) : (
          CATEGORY_ORDER.map((type) => {
            const items = groupedEntities[type];
            if (!items.length) return null;
            const isExpanded = expandedCategories[type];

            return (
              <div key={type} className="space-y-1">
                <button
                  onClick={() => toggleCategory(type)}
                  className="flex items-center gap-2 w-full px-2 py-1.5 text-left text-xs font-semibold text-white/50 hover:text-white transition-colors"
                  aria-expanded={isExpanded}
                >
                  <span className={cn("w-4 h-4 transition-transform", isExpanded ? "rotate-90" : "")}>
                    <ChevronRight className="w-4 h-4" />
                  </span>
                  {React.cloneElement(TYPE_ICONS[type], { className: cn(TYPE_COLORS[type], "w-4 h-4") })}
                  <span className={cn("capitalize", TYPE_COLORS[type])}>{type}</span>
                  <span className="ml-auto px-2 py-0.5 rounded-full bg-white/10 text-[10px] text-white/50">
                    {items.length}
                  </span>
                </button>

                {isExpanded && (
                  <div className="space-y-1 pl-6 animate-in fade-in-80 duration-200" role="group">
                    {items.slice(0, 50).map((entity) => (
                      <button
                        key={entity.id}
                        onClick={() => onSelectEntity(entity)}
                        className={cn(
                          "flex items-center gap-2 w-full px-3 py-2 rounded-lg text-left text-sm transition-all",
                          selectedEntity?.id === entity.id
                            ? "bg-[#FF3C00]/20 text-white border border-[#FF3C00]/30"
                            : "text-white/70 hover:bg-white/5 hover:text-white"
                        )}
                        role="option"
                        aria-selected={selectedEntity?.id === entity.id}
                      >
                        <span className={cn("w-2 h-2 rounded-full flex-shrink-0", TYPE_BG[entity.entity_type as EntityType])} />
                        <span className="flex-1 min-w-0 truncate font-medium">{entity.name}</span>
                        {entity.label_hint && (
                          <span className="px-2 py-0.5 rounded text-[10px] font-mono text-white/40 bg-white/5">
                            {entity.label_hint}
                          </span>
                        )}
                        {entity.fiscal_year && (
                          <span className="px-2 py-0.5 rounded text-[10px] font-mono text-white/40 bg-white/5">
                            FY{entity.fiscal_year}
                          </span>
                        )}
                      </button>
                    ))}
                    {items.length > 50 && (
                      <button className="w-full px-3 py-2 text-center text-xs text-white/40 hover:text-white/60 transition-colors">
                        + {items.length - 50} more results
                      </button>
                    )}
                  </div>
                )}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}