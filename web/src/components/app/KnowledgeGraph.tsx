"use client";

import React, { useEffect, useRef, useState, useCallback } from "react";
import * as d3 from "d3";
import { cn } from "@/lib/utils";
import type { GraphPayload, GraphNode, GraphEdge, EntityType } from "@/lib/api";

interface KnowledgeGraphProps {
  data: GraphPayload;
  citedNodes?: Set<string>;
  selectedNode?: string | null;
  highlightedNodes?: Set<string>;
  onNodeClick: (node: GraphNode) => void;
  onBackgroundClick: () => void;
  width?: number;
  height?: number;
  showLabels?: boolean;
  className?: string;
}

const TYPE_COLORS: Record<EntityType, string> = {
  Company: "#FF3C00",
  Filing: "#FF6B35",
  FinancialMetric: "#E63600",
  Segment: "#FF8A50",
  DisclosureEvent: "#B84A2E",
  Unspecified: "#8B95A7",
};

interface D3Node extends GraphNode {
  x?: number;
  y?: number;
  vx?: number;
  vy?: number;
  fx?: number | null;
  fy?: number | null;
  r?: number;
}

interface D3Link extends GraphEdge {
  source: D3Node;
  target: D3Node;
}

export function KnowledgeGraph({
  data,
  citedNodes = new Set(),
  selectedNode = null,
  highlightedNodes = new Set(),
  onNodeClick,
  onBackgroundClick,
  width,
  height,
  showLabels = true,
  className,
}: KnowledgeGraphProps) {
  const svgRef = useRef<SVGSVGElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [transform, setTransform] = useState({ x: 0, y: 0, k: 1 });
  const simulationRef = useRef<any>(null);
  const nodesRef = useRef<D3Node[]>([]);
  const linksRef = useRef<D3Link[]>([]);
  const linkSelRef = useRef<any>(null);
  const nodeSelRef = useRef<any>(null);
  const labelSelRef = useRef<any>(null);

  const color = (type: EntityType) => TYPE_COLORS[type] || TYPE_COLORS.Unspecified;

  const initializeSimulation = useCallback((nodes: D3Node[], links: D3Link[]) => {
    if (simulationRef.current) {
      simulationRef.current.stop();
    }

    const { width: w, height: h } = containerRef.current?.getBoundingClientRect() || { width: width || 800, height: height || 600 };

    const simulation = d3.forceSimulation<D3Node, D3Link>(nodes)
      .force("link", d3.forceLink<D3Node, D3Link>(links).id((d: D3Node) => d.id).distance(80).strength(0.08))
      .force("charge", d3.forceManyBody<D3Node>().strength((d) => -150 - 6 * ((d as D3Node).r || 8)))
      .force("x", d3.forceX(w / 2).strength(0.04))
      .force("y", d3.forceY(h / 2).strength(0.04))
      .force("center", d3.forceCenter(w / 2, h / 2))
      .force("collide", d3.forceCollide<D3Node>((d) => (d.r || 8) + 6).iterations(2))
      .alpha(0.8)
      .on("tick", () => {
        linkSelRef.current?.attr("d", (link: D3Link) => {
          const source = link.source;
          const target = link.target;
          if (!source.x || !source.y || !target.x || !target.y) return "";
          const dx = target.x - source.x;
          const dy = target.y - source.y;
          const dr = Math.sqrt(dx * dx + dy * dy) * (link.curve || 1) * 0.15;
          return `M${source.x},${source.y}Q${source.x + dx / 2 - dy * dr},${source.y + dy / 2 + dx * dr} ${target.x},${target.y}`;
        });

        nodeSelRef.current?.attr("transform", (d: D3Node) => `translate(${d.x || 0},${d.y || 0})`);
        labelSelRef.current?.attr("transform", (d: D3Node) => `translate(${d.x || 0},${d.y || 0})`);
      });

    simulationRef.current = simulation;
  }, [width, height]);

  useEffect(() => {
    if (!svgRef.current || !containerRef.current) return;

    const svg = d3.select(svgRef.current);
    const container = containerRef.current;
    const { width: w, height: h } = container.getBoundingClientRect();

    svg.attr("width", w).attr("height", h);

    const defs = svg.select("defs").empty() ? svg.append("defs") : svg.select("defs");

    if (defs.select("#arrowhead").empty()) {
      defs.append("marker")
        .attr("id", "arrowhead")
        .attr("viewBox", "0 0 10 10")
        .attr("refX", 8)
        .attr("refY", 5)
        .attr("markerWidth", 6)
        .attr("markerHeight", 6)
        .attr("orient", "auto-start-reverse")
        .append("path")
        .attr("d", "M 0 0 L 10 5 L 0 10 z")
        .attr("fill", "#8B95A7");
    }

    if (defs.select("#arrowhead-cited").empty()) {
      defs.append("marker")
        .attr("id", "arrowhead-cited")
        .attr("viewBox", "0 0 10 10")
        .attr("refX", 8)
        .attr("refY", 5)
        .attr("markerWidth", 6)
        .attr("markerHeight", 6)
        .attr("orient", "auto-start-reverse")
        .append("path")
        .attr("d", "M 0 0 L 10 5 L 0 10 z")
        .attr("fill", "#FF3C00");
    }

    if (defs.select("#arrowhead-highlight").empty()) {
      defs.append("marker")
        .attr("id", "arrowhead-highlight")
        .attr("viewBox", "0 0 10 10")
        .attr("refX", 8)
        .attr("refY", 5)
        .attr("markerWidth", 6)
        .attr("markerHeight", 6)
        .attr("orient", "auto-start-reverse")
        .append("path")
        .attr("d", "M 0 0 L 10 5 L 0 10 z")
        .attr("fill", "#FF6B35");
    }

    if (defs.select("#glow").empty()) {
      const filter = defs.append("filter").attr("id", "glow").attr("x", "-50%").attr("y", "-50%").attr("width", "200%").attr("height", "200%");
      filter.append("feGaussianBlur").attr("stdDeviation", "3").attr("result", "blur");
      filter.append("feMerge").append("feMergeNode").attr("in", "blur");
    }

    const g = svg.select("g.graph-layer").empty() ? svg.append("g").attr("class", "graph-layer") : svg.select("g.graph-layer");

    g.attr("transform", `translate(${transform.x},${transform.y}) scale(${transform.k})`);

    const linkGroup = g.select("g.links").empty() ? g.append("g").attr("class", "links") : g.select("g.links");
    const nodeGroup = g.select("g.nodes").empty() ? g.append("g").attr("class", "nodes") : g.select("g.nodes");
    const labelGroup = g.select("g.labels").empty() ? g.append("g").attr("class", "labels") : g.select("g.labels");

    const byId = new Map(data.nodes.map((n) => [n.id, { ...n }]));
    const links = data.edges.map((e) => ({
      ...e,
      source: typeof e.source === "string" ? byId.get(e.source)! : e.source,
      target: typeof e.target === "string" ? byId.get(e.target)! : e.target,
    })).filter((e) => e.source && e.target) as D3Link[];

    const nodes = data.nodes.map((n) => ({
      ...n,
      r: 5 + Math.min(8, (n.degree || 1) * 2),
    })) as D3Node[];

    nodesRef.current = nodes;
    linksRef.current = links;

    // Links
    const linkData = (linkGroup as any).selectAll("path.link")
      .data(links, (d: D3Link) => `${d.source.id}|${d.target.id}|${d.relation}`);

    linkData.exit().remove();

    const linkEnter = linkData.enter()
      .append("path");
    linkEnter
      .attr("class", "link")
      .attr("fill", "none")
      .attr("stroke-width", 1.2)
      .attr("stroke-linecap", "round")
      .attr("marker-end", (d: D3Link) => {
        const isCited = citedNodes.has(d.source.id) && citedNodes.has(d.target.id);
        const isHighlighted = highlightedNodes.has(d.source.id) || highlightedNodes.has(d.target.id);
        if (isCited) return "url(#arrowhead-cited)";
        if (isHighlighted) return "url(#arrowhead-highlight)";
        return "url(#arrowhead)";
      })
      .style("stroke", (d: D3Link) => {
        const isCited = citedNodes.has(d.source.id) && citedNodes.has(d.target.id);
        const isHighlighted = highlightedNodes.has(d.source.id) || highlightedNodes.has(d.target.id);
        if (isCited) return "#FF3C00";
        if (isHighlighted) return "#FF6B35";
        return "rgba(255,255,255,0.15)";
      })
      .style("stroke-opacity", (d: D3Link) => {
        const isCited = citedNodes.has(d.source.id) && citedNodes.has(d.target.id);
        const isHighlighted = highlightedNodes.has(d.source.id) || highlightedNodes.has(d.target.id);
        return isCited || isHighlighted ? 0.9 : 0.4;
      })
      .style("filter", (d: D3Link) => {
        const isCited = citedNodes.has(d.source.id) && citedNodes.has(d.target.id);
        return isCited ? "url(#glow)" : "none";
      });

    linkSelRef.current = linkData.merge(linkEnter);

    // Nodes
    const nodeData = (nodeGroup as any).selectAll("g.node")
      .data(nodes, (d: D3Node) => d.id);

    nodeData.exit().remove();

    const nodeEnter = nodeData.enter()
      .append("g");
    nodeEnter
      .attr("class", "node")
      .call(
        d3.drag<SVGGElement, D3Node>()
          .on("start", (event: any, d: D3Node) => {
            if (!event.active) simulationRef.current?.alphaTarget(0.3).restart();
            d.fx = d.x;
            d.fy = d.y;
          })
          .on("drag", (event: any, d: D3Node) => {
            d.fx = event.x;
            d.fy = event.y;
          })
          .on("end", (event: any, d: D3Node) => {
            if (!event.active) simulationRef.current?.alphaTarget(0);
            d.fx = null;
            d.fy = null;
          })
      )
      .on("click", (event: any, d: D3Node) => {
        event.stopPropagation();
        onNodeClick(d);
      });

    nodeEnter.append("circle")
      .attr("class", "node-core")
      .attr("r", (d: D3Node) => d.r || 8)
      .attr("fill", (d: D3Node) => color(d.entity_type))
      .attr("stroke", (d: D3Node) => {
        const isCited = citedNodes.has(d.id);
        const isSelected = selectedNode === d.id;
        const isHighlighted = highlightedNodes.has(d.id);
        if (isSelected) return "#FFFFFF";
        if (isCited) return "#FF3C00";
        if (isHighlighted) return "#FF6B35";
        return "rgba(255,255,255,0.2)";
      })
      .attr("stroke-width", (d: D3Node) => {
        const isSelected = selectedNode === d.id;
        const isCited = citedNodes.has(d.id);
        return isSelected ? 2.5 : isCited ? 2 : 1;
      })
      .style("filter", (d: D3Node) => citedNodes.has(d.id) ? "url(#glow)" : "none");

    nodeEnter.append("circle")
      .attr("class", "node-halo")
      .attr("r", (d: D3Node) => (d.r || 8) + 4)
      .attr("fill", "none")
      .attr("stroke", (d: D3Node) => citedNodes.has(d.id) ? "#FF3C00" : "transparent")
      .attr("stroke-width", 1.5)
      .attr("stroke-opacity", 0.6)
      .style("filter", (d: D3Node) => citedNodes.has(d.id) ? "url(#glow)" : "none");

    // Labels
    const labelData = (labelGroup as any).selectAll("text.label")
      .data(nodes, (d: D3Node) => d.id);

    labelData.exit().remove();

    const labelEnter = labelData.enter()
      .append("text");
    labelEnter
      .attr("class", "label")
      .attr("text-anchor", "middle")
      .attr("dy", (d: D3Node) => (d.r || 8) + 14)
      .attr("font-size", "11px")
      .attr("fill", "#E8E8EC")
      .attr("font-weight", 500)
      .style("pointer-events", "none")
      .style("display", (d: D3Node) => {
        if (!showLabels) return "none";
        const isCited = citedNodes.has(d.id);
        const isSelected = selectedNode === d.id;
        const isHighlighted = highlightedNodes.has(d.id);
        return (showLabels && (transform.k > 0.5 || isCited || isSelected || isHighlighted)) ? null : "none";
      })
      .text((d: D3Node) => d.name.length > 28 ? `${d.name.slice(0, 27)}…` : d.name);

    labelSelRef.current = labelData.merge(labelEnter);
    nodeSelRef.current = nodeData.merge(nodeEnter);

    initializeSimulation(nodes, links);

    return () => {
      simulationRef.current?.stop();
    };
  }, [data, citedNodes, selectedNode, highlightedNodes, transform, showLabels, initializeSimulation, width, height, onNodeClick]);

  const zoom = useCallback(
    d3.zoom<SVGSVGElement, unknown>()
      .scaleExtent([0.1, 4])
      .on("zoom", (event: any) => {
        setTransform({ x: event.transform.x, y: event.transform.y, k: event.transform.k });
      }),
    []
  );

  useEffect(() => {
    if (svgRef.current) {
      d3.select(svgRef.current).call(zoom).on("dblclick.zoom", null);
    }
  }, [zoom]);

  const handleBackgroundClick = (event: React.MouseEvent) => {
    if (event.target === event.currentTarget) {
      onBackgroundClick();
    }
  };

  const fit = useCallback(() => {
    if (!simulationRef.current || nodesRef.current.length === 0) return;
    const bounds = containerRef.current?.getBoundingClientRect();
    if (!bounds) return;

    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (const node of nodesRef.current) {
      const r = (node.r || 8) + 20;
      minX = Math.min(minX, (node.x || 0) - r);
      maxX = Math.max(maxX, (node.x || 0) + r);
      minY = Math.min(minY, (node.y || 0) - r);
      maxY = Math.max(maxY, (node.y || 0) + r);
    }

    const k = Math.max(0.1, Math.min(2, 0.85 * Math.min(
      bounds.width / Math.max(1, maxX - minX),
      bounds.height / Math.max(1, maxY - minY),
    )));

    const x = bounds.width / 2 - ((minX + maxX) / 2) * k;
    const y = bounds.height / 2 - ((minY + maxY) / 2) * k;

    setTransform({ x, y, k });
    if (svgRef.current) {
      d3.select(svgRef.current).transition().duration(600).call(
        zoom.transform,
        d3.zoomIdentity.translate(x, y).scale(k)
      );
    }
  }, [zoom]);

  useEffect(() => {
    fit();
  }, [data.nodes.length, fit]);

  return (
    <div
      ref={containerRef}
      className={cn("relative w-full h-full bg-[radial-gradient(ellipse_at_center,_rgba(255,60,0,0.03),_transparent_70%)]", className)}
      onClick={handleBackgroundClick}
      style={{ width, height }}
    >
      <svg ref={svgRef} className="w-full h-full" style={{ background: "transparent" }}>
        <defs />
        <rect
          width="100%"
          height="100%"
          fill="url(#graph-grid)"
          opacity={0.15}
        />
        <defs>
          <pattern id="graph-grid" width="40" height="40" patternUnits="userSpaceOnUse">
            <path d="M 40 0 L 0 0 0 40" fill="none" stroke="#FF3C00" stroke-width="0.5" stroke-opacity="0.3" />
          </pattern>
        </defs>
      </svg>
    </div>
  );
}