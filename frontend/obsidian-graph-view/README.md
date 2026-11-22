# Obsidian Graph View

An interactive, force-directed knowledge graph visualization in the style of
[Obsidian's Graph View](https://obsidian.md/): nodes = notes/entities, edges = links,
live force-directed layout, draggable nodes, zoom/pan, hover neighborhood highlighting,
context menus, filters, groups, and a chronological time-lapse animation.

**100% free, open-source, client-side.** One runtime dependency: `d3-force` (BSD-3).
No API keys, no SaaS, no network calls at runtime — works fully offline once the
dependency is vendored.

## Deliverables (this folder)

| File | Purpose |
|------|---------|
| `graph.js` | Framework-agnostic graph module (ES module, vanilla JS + HTML5 Canvas) |
| `demo.html` | Single self-contained HTML demo (loads `d3-force` from CDN via import map) |
| `README.md` | This file |

## Features

- **Force layout** via `d3-force` (BSD-3) rendered on HTML5 Canvas — 60 fps up to ~2,000 nodes / 5,000 edges on a mid-range laptop
- **Node size scales with popularity** — radius grows with incoming-link count (Obsidian's rule, log scale)
- **Unresolved nodes** — links to missing notes render as dimmed ghost nodes; right-click → Create
- **Hover** highlights the hovered node + its direct connections; everything else fades to low opacity
- **Click** → `onNodeClick(id)` callback (you handle navigation; never hard-coded)
- **Right-click** → context menu (open, copy link, create-if-unresolved, center)
- **Drag** nodes; simulation adapts while dragging and re-settles on release
- **Zoom**: wheel/pinch, `+`/`-` keys, centered on cursor
- **Pan**: drag empty canvas, arrow keys, `Shift` = faster
- **Animate** time-lapse — nodes/links pop in in creation-time order
- **Settings popover** (cog icon) with tabs: Filters, Groups, Display, Forces
- **Filters**: search (substring / `#tag`), tag nodes, attachments, existing files only, orphans, exclusion patterns
- **Groups**: search query + user-chosen color, with optional colored halo ring
- **Display**: arrows, text fade threshold, node size, link thickness, animate toggle
- **Forces**: center, repel, link force, link distance — live re-settle, plus "Restore default settings"
- **Local graph mode**: centered on an active node with depth slider (1–4); updates when the active node changes; settings are shared per instance

## Dependencies

| Package | License | Role |
|---------|---------|------|
| d3-force | BSD-3-Clause | Force-directed simulation (center, charge/repel, link, collide) |

That's it. Everything else (canvas rendering, input handling, settings UI) is vanilla JavaScript.

## Quick start

```html
<!-- import map (use a CDN for demos; vendor d3-force for offline/self-hosted use) -->
<script type="importmap">
  { "imports": { "d3-force": "https://esm.sh/d3-force@3" } }
</script>
<script type="module">
  import { ObsidianGraph } from './graph.js'

  const graph = new ObsidianGraph(document.getElementById('graph'), {
    createUI: true, // built-in cog settings popover
    callbacks: {
      onNodeClick: (id) => { /* open your note/entity */ },
      onNodeContextMenu: (id, event, { actions, x, y }) => { /* show your own menu */ },
      onSettingsChange: (patch) => { /* persist settings */ },
    },
  })

  graph.load({ nodes: [...], edges: [...] })
</script>
```

Install the runtime dependency:

```bash
npm install d3-force   # BSD-3
```

For fully offline use, vendor `d3-force` (e.g. copy `node_modules/d3-force/dist/d3-force.*.js`
into your repo) and point the import map / bundler at the local copy instead of a CDN.

## API

### `new ObsidianGraph(container, opts)`

`container` — DOM element that will hold the canvas (it is made `position: relative` if needed).

`opts`:
- `settings` — partial settings object, merged with defaults (see Settings below)
- `createUI` — `true` (default) builds the cog + settings popover inside `container`; pass `false` to supply your own UI and drive the graph via `setSettings()`
- `callbacks`:
  - `onNodeClick(id, event)` — user clicked a node (open the note/entity)
  - `onNodeContextMenu(id, event, { actions, x, y })` — right-click on a node; `actions` is a list of `{ label, action }` suggestions
  - `onSettingsChange(patch)` — fired whenever settings change (persist or mirror to another instance)
  - `onRender()` — fired after each render frame

### `graph.load({ nodes, edges })`

Data model:

```js
{
  nodes: [{
    id: string,              // required
    label: string,           // displayed text
    created?: number,        // epoch ms — drives the time-lapse animation
    group?: string,          // folder / path / type (used by search + grouping)
    tags?: string[],
    isAttachment?: boolean,
    exists?: boolean,        // false → unresolved "ghost" node, rendered dimmed
    ...
  }],
  edges: [{
    source: string,
    target: string,
    resolved?: boolean,      // false → link to a missing note
    ...
  }]
}
```

Edge endpoints missing from `nodes` are auto-created as dimmed ghost nodes.

### `graph.setSettings(patch)`

Merge settings and apply live. Force sliders (`centerForce`, `repelForce`, `linkForce`, `linkDistance`) re-settle the simulation without restarting; anything else rebuilds the filtered layout.

### `graph.focus(nodeId, depth)` / `graph.unfocus()`

Local graph mode: keep only the node's neighborhood up to `depth` (1–4). Recompute automatically when the active node or depth changes. `unfocus()` returns to the global graph.

### `graph.resetView()` / `graph.fitToView()` / `graph.resize()` / `graph.destroy()`

- `resetView()` — re-center on the graph origin at 1:1 zoom
- `fitToView()` — zoom/pan so every visible node fits
- `resize()` — call when the container size changes (also bound to `window.resize` automatically)
- `destroy()` — stop simulation, remove listeners/canvas

## Settings

| Setting | Range | Default | Meaning |
|---------|-------|---------|---------|
| `centerForce` | 0–1 | 0.05 | Pull toward center (higher = more compact) |
| `repelForce` | 0–600 | 180 | Node-node repulsion (higher = more spread out) |
| `linkForce` | 0–1 | 0.6 | Link tightness (rubber band) |
| `linkDistance` | 20–400 px | 100 | Resting link length |
| `showArrows` | bool | false | Arrowheads source → target |
| `textFadeThreshold` | 0–2 | 0.5 | Zoom at which labels fade in |
| `nodeSizeScale` | 1–40 px | 13 | Pixels per log1p(incoming links) |
| `nodeBaseSize` | 3–20 px | 7 | Minimum node radius |
| `linkThickness` | 0.2–4 px | 1.1 | Edge stroke width |
| `animate` | bool | false | Chronological time-lapse |
| `darkTheme` | bool | true | Dark / light canvas |
| `search` | string | '' | Substring match on id/label/group/tags; `#tag` matches tags |
| `showTags` / `showAttachments` / `showOrphans` | bool | true / true / true | Toggle node classes |
| `existingOnly` | bool | false | Hide unresolved ghost nodes |
| `exclusions` | string[] | [] | Regex or substring patterns matched against id/label/group/tags |
| `groups` | [{id, query, color, halo}] | [] | Matching nodes take the color (+ optional halo ring) |
| `localMode` / `focusNode` / `localDepth` | — | false / null / 2 | Local graph around a node, depth 1–4 |

Node size = `nodeBaseSize + nodeSizeScale × log1p(incoming links)`, matching Obsidian's "more links = bigger node" rule. Unresolved nodes are always drawn dimmer than resolved ones.

## Data model notes for your project

- **Markdown vaults**: parse `[[wikilinks]]`, `[text](path.md)` links and `#tags` from your files into the `{nodes, edges}` shape above. A link whose target file doesn't exist becomes `resolved: false`, and its target appears as a dimmed ghost node.
- **Non-note projects**: map your entities to `nodes` and relationships to `edges`; the same component works for any graph-shaped data.
- `created` (epoch ms) powers the time-lapse; omit it and nodes reveal immediately.

## Demo

Open `demo.html` in a browser. It seeds ~25 notes across folders/tags, 4 unresolved links (dimmed ghosts), 1 orphan note, and timestamps spanning Jan–Nov 2026 so the time-lapse animation is demonstrable. The demo loads `d3-force` from `esm.sh` via an import map.

## Licenses

- `graph.js`, `demo.html`, `README.md`: MIT (part of this project)
- `d3-force`: BSD-3-Clause (https://github.com/d3/d3-force)

No other dependencies are used.