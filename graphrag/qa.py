"""GraphRAG retrieval and cited question answering.

Retrieval runs in three stages:

1. *Entity linking* - the model names entities in the question, and those names
   are resolved to graph nodes by slug, with a lexical fallback for names the
   model phrased differently from the stored node.
2. *Traversal* - the 1-hop and 2-hop neighbourhood of the linked nodes is
   collected, undirected, so inbound edges are considered too.
3. *Synthesis* - the subgraph is serialised with stable citation tags and the
   model answers using only that context.

Every answer reports which tags it actually cited, so an ungrounded answer is
visible rather than silently accepted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import GraphRAGConfig
from .extract import ANSWER_SYSTEM, identify_entities, normalise_text
from .llm import LLMClient
from .resolve import slugify
from .store import Entity, GraphStore, Subgraph, _QUESTION_WORDS

_CITATION = re.compile(r"\[(E\d+)\]")


@dataclass
class Answer:
    question: str
    text: str
    cited: list[str] = field(default_factory=list)
    used_tags: list[str] = field(default_factory=list)
    linked: list[str] = field(default_factory=list)
    context_entities: int = 0
    context_edges: int = 0
    grounded: bool = True
    note: str = ""
    # Citation tag -> entity id, so a caller can resolve "[E2]" to a node
    # without re-deriving the tag order. The UI needs this to highlight the
    # entities an answer actually leaned on.
    tag_map: dict[str, str] = field(default_factory=dict)
    # The subgraph that was given to the model, kept for inspection.
    graph: "Subgraph | None" = None

    def render(self) -> str:
        lines = [self.text, ""]
        lines.append(f"linked entities : {', '.join(self.linked) or '(none)'}")
        lines.append(
            f"subgraph         : {self.context_entities} entities, "
            f"{self.context_edges} relationships"
        )
        if self.used_tags:
            lines.append(f"cited            : {', '.join(self.used_tags)}")
        if not self.grounded:
            lines.append(f"warning          : {self.note}")
        flow = self.render_flow()
        if flow:
            lines.extend(["", flow])
        return "\n".join(lines)

    def render_flow(self, max_edges_per_seed: int = 6) -> str:
        """Show how the answer's nodes connect, as an indented trace.

        The answer text alone gives no indication of *why* it says what it says.
        This renders the retrieval path underneath it: the question, the seeds it
        linked to, the relationships radiating from those seeds, and which tags
        the answer actually leaned on. Without the graph the answer is not
        auditable, so this is the part a reader should check.
        """
        # An ungrounded answer is exactly when the reader most needs to see
        # where the chain broke, and that path never builds a subgraph.
        if not self.grounded:
            return "\n".join(
                [
                    "FLOW (how these nodes connect)",
                    f"  {self.question}",
                    f"    └─ stopped: {self.note or 'not grounded'}",
                ]
            )

        if not self.graph or self.graph.is_empty():
            return ""

        names = {
            tag: self.graph.nodes[entity_id].name
            for tag, entity_id in self.tag_map.items()
            if entity_id in self.graph.nodes
        }
        # tag lookup by entity id, for labelling an edge's far end
        by_id = {entity_id: tag for tag, entity_id in self.tag_map.items()}

        out = ["FLOW (how these nodes connect)"]

        out.append(f'  {self.question}')
        out.append(
            f"    │\n"
            f"    ├─ linked {len(self.graph.seeds)} seed"
            f"{'s' if len(self.graph.seeds) != 1 else ''} "
            f"from the graph"
        )

        for seed in self.graph.seeds:
            tag = by_id.get(seed.id, "")
            out.append(f"    │   [{tag}] {seed.name}  ({seed.entity_type})")
            spokes = [
                edge
                for edge in self.graph.edges
                if edge.from_id == seed.id or edge.to_id == seed.id
            ]
            for edge in spokes[:max_edges_per_seed]:
                outward = edge.from_id == seed.id
                other = edge.to_id if outward else edge.from_id
                other_tag = by_id.get(other, "?")
                other_name = names.get(other_tag, other)
                arrow = f"--{edge.rel_type}-->" if outward else f"<--{edge.rel_type}--"
                out.append(f"    │     {arrow} [{other_tag}] {other_name}")
            hidden = len(spokes) - max_edges_per_seed
            if hidden > 0:
                out.append(f"    │     ... and {hidden} more relationship(s)")

        out.append(
            f"    │\n"
            f"    ├─ retrieved {self.context_entities} nodes, "
            f"{self.context_edges} relationships\n"
            f"    └─ answered using "
            + (", ".join(f"[{t}]" for t in self.used_tags) if self.used_tags else "no citations")
        )
        return "\n".join(out)


def _lexical_seeds(
    store: GraphStore, question: str, per_token: int = 2, limit: int = 6
) -> list[Entity]:
    """Fall back to the question's own words when the model names nothing.

    The identification prompt asks for proper names and distinctive phrases,
    and tells the model that an empty list is correct when the question names
    nothing findable. A question written in ordinary words therefore comes back
    naming nothing even when the graph holds the answer: "which officers signed
    the certifications" resolves to nothing, while the graph contains both
    "Principal executive officer" and three certification documents.

    Matching the question's content words against node names recovers those
    cases without loosening the prompt, which is right for the questions that
    the model already handles.
    """
    seeds: list[Entity] = []
    seen: set[str] = set()
    # Question order is the relevance signal: earlier words are the subject.
    tokens = [
        t
        for t in re.split(r"[^a-z0-9]+", question.lower())
        if len(t) > 2 and t not in _QUESTION_WORDS
    ]
    for token in tokens:
        for entity in store.best_name_match(token, limit=per_token):
            if entity.id in seen:
                continue
            seen.add(entity.id)
            seeds.append(entity)
            if len(seeds) >= limit:
                return seeds
    return seeds


def link_entities(
    store: GraphStore, client: LLMClient, question: str
) -> list[Entity]:
    """Resolve the entities named in *question* to graph nodes.

    Exact slug matches win. A name the model produced that does not match any
    node is retried as a lexical search, which recovers cases where the model
    returned a longer or shorter surface form than the stored node.
    """
    names = identify_entities(client, question)
    if not names:
        # The model declined to name anything. Before reporting that the
        # question mentions no known entity, try its plain words.
        return _lexical_seeds(store, question)
    linked: list[Entity] = []
    seen: set[str] = set()

    for name in names:
        candidates: list[Entity] = []
        slug = slugify(name)
        if slug:
            candidates = store.find_entities([slug])
        if not candidates:
            # Lexical fallback: the model may phrase a name differently from the
            # stored node. Ranked so only the most specific matches are linked,
            # otherwise a genus name drags in every node containing it.
            candidates = store.best_name_match(name, limit=3)
        for entity in candidates:
            if entity.id in seen:
                continue
            seen.add(entity.id)
            linked.append(entity)
    return linked


def _describe_entity(tag: str, entity: Entity) -> str:
    bits = [f'[{tag}] "{entity.name}"']
    if entity.entity_type:
        bits.append(f"({entity.entity_type})")
    line = " ".join(bits)
    if entity.description:
        line += f": {entity.description}"
    return line


def render_context(graph: Subgraph) -> tuple[str, list[str], dict[str, str]]:
    """Serialise a subgraph into a prompt block with stable citation tags.

    Seed entities are tagged first so the model can reference the entities the
    question actually asked about, then the rest of the neighbourhood.

    Returns the block, the seed tags, and a ``tag -> entity id`` map so callers
    can resolve a citation like "[E2]" back to a node.
    """
    tags: dict[str, str] = {}
    lines: list[str] = ["ENTITIES:"]
    index = 1

    for entity in graph.seeds:
        tag = f"E{index}"
        tags[entity.id] = tag
        lines.append(_describe_entity(tag, entity))
        index += 1

    for entity in graph.node_list():
        if entity.id in tags:
            continue
        tag = f"E{index}"
        tags[entity.id] = tag
        lines.append(_describe_entity(tag, entity))
        index += 1

    lines.append("")
    lines.append("RELATIONSHIPS:")
    for edge in graph.edges:
        src = tags.get(edge.from_id)
        dst = tags.get(edge.to_id)
        if not src or not dst:
            continue
        line = f"[{src}] --{edge.rel_type}--> [{dst}]"
        if edge.description:
            line += f": {edge.description}"
        lines.append(line)

    return (
        "\n".join(lines),
        [tags[e.id] for e in graph.seeds if e.id in tags],
        # Inverted to tag -> entity id: that is the direction a caller needs
        # to resolve a citation in an answer back to a node.
        {tag: entity_id for entity_id, tag in tags.items()},
    )


def build_answer_prompt(question: str, context: str) -> str:
    return (
        f"CONTEXT (retrieved knowledge graph):\n{context}\n\n"
        f"QUESTION: {question}\n\n"
        "Answer using only the context above, citing tags like [E1]."
    )


def ask(
    question: str,
    store: GraphStore,
    client: LLMClient,
    config: GraphRAGConfig | None = None,
) -> Answer:
    """Answer *question* from the graph, with citations."""
    config = config or store.config
    question = normalise_text(question)
    if not question:
        return Answer(
            question=question,
            text="No question provided.",
            grounded=False,
            note="empty question",
        )

    try:
        linked = link_entities(store, client, question)
    except Exception as exc:  # noqa: BLE001 - surface provider failure to caller
        # Distinct from "no entity linked": the model was never successfully
        # asked, so reporting an empty result here would misstate the cause.
        return Answer(
            question=question,
            text=f"Entity resolution failed: {exc}",
            grounded=False,
            note=str(exc),
        )
    if not linked:
        return Answer(
            question=question,
            text=(
                "The question does not name any entity present in the graph, so "
                "there is nothing to retrieve."
            ),
            grounded=False,
            note="no entity could be linked to a graph node",
        )

    graph = store.neighborhood([e.id for e in linked], hops=config.hops)
    context, seed_tags, tag_map = render_context(graph)

    if graph.is_empty():
        return Answer(
            question=question,
            text=(
                "The linked entities exist in the graph but have no recorded "
                "relationships, so the question cannot be answered from context."
            ),
            linked=[e.name for e in linked],
            grounded=False,
            note="linked nodes have no edges",
            graph=graph,
        )

    prompt = build_answer_prompt(question, context)
    try:
        text = client.complete_text(ANSWER_SYSTEM, prompt)
    except Exception as exc:  # noqa: BLE001 - surface provider failure to caller
        return Answer(
            question=question,
            text=f"Answer generation failed: {exc}",
            linked=[e.name for e in linked],
            context_entities=len(graph.nodes),
            context_edges=len(graph.edges),
            grounded=False,
            note=str(exc),
            tag_map=tag_map,
            graph=graph,
        )

    valid = set(seed_tags) | {f"E{i}" for i in range(1, len(graph.nodes) + 1)}
    cited = _CITATION.findall(text or "")
    used = sorted({tag for tag in cited if tag in valid}, key=_tag_sort)
    hallucinated = sorted({tag for tag in cited if tag not in valid})

    answer = Answer(
        question=question,
        text=(text or "").strip(),
        cited=cited,
        used_tags=used,
        linked=[e.name for e in linked],
        context_entities=len(graph.nodes),
        context_edges=len(graph.edges),
        grounded=not hallucinated and bool(used),
        tag_map=tag_map,
        graph=graph,
    )
    if hallucinated:
        answer.note = (
            "cited tags not present in context: " + ", ".join(hallucinated)
        )
    elif not used:
        answer.note = "answer contains no citations"
    return answer


def _tag_sort(tag: str) -> int:
    match = re.match(r"E(\d+)$", tag)
    return int(match.group(1)) if match else 0
