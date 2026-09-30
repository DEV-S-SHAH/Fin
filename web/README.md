# FinGraph web (shadcn/ui)

A self-contained [Vite](https://vite.dev) + React + TypeScript + [Tailwind CSS v4](https://tailwindcss.com) + [shadcn/ui](https://ui.shadcn.com) project.

The rest of this repository is a Python standard-library HTTP server (`sandbox_engine/ui_next`) that serves a vanilla-landing and the GraphRAG studio. It has **no React/Tailwind/TS/npm build step**, so this folder carries the shadcn/ui stack and the footer component.

## Stack

- React 18 + TypeScript (strict)
- Tailwind CSS v4 via the `@tailwindcss/vite` plugin (no `tailwind.config.js` — tokens live in `src/index.css`)
- shadcn/ui convention: components in `src/components/ui`, utility in `src/lib/utils.ts`
- Path alias `@/*` → `./src/*` (vite + tsconfig)
- Dependencies: `motion` (scroll/blur reveal), `lucide-react@0.440.0` (icons), `class-variance-authority`, `clsx`, `tailwind-merge`, `tw-animate-css`

> **Why `lucide-react@0.440.0`?** Current lucide releases removed brand logos (Facebook, Instagram, YouTube, LinkedIn). The footer links render those four social icons, so this project pins the last version that ships them. If you don't need the brand icons, you can upgrade freely.

## Getting started

```bash
cd web
npm install        # already run — installs rely on this lockfile/state
npm run dev        # http://localhost:5173
npm run build      # type-checks (tsc -b) + production build to dist/
npm run preview    # serve the production build
```

`src/main.tsx` mounts `src/demo.tsx`, which renders the `Footer` from `src/components/ui/footer-section.tsx`. Scroll down to see the blur/fade reveal.

## Setting up a fresh project via shadcn CLI

If you want to recreate this stack from scratch (get the CLI to wire Tailwind v4, path aliases, and CSS variables for you):

```bash
npm create vite@latest my-app -- --template react-ts
cd my-app
npx shadcn@latest init    # choose "New York" style, "Slate" base color, CSS variables on
npm i motion lucide-react
npx shadcn@latest add button
```

The CLI generates `components.json`, the theme tokens in `src/index.css`, the `@/*` alias, and `src/lib/utils.ts`.

### Why `/components/ui`?

`components.json` maps the `ui` alias to `src/components/ui`. shadcn and its ecosystem (registry copy-paste, the `shadcn add` CLI, and most community components like this footer) emit components into `src/components/ui`. Keeping them in that one folder:

- stays compatible with `npx shadcn add` (new components land alongside existing ones without manual moves or alias changes);
- keeps wrapper components separate from your app-specific code (`src/App.tsx`, `src/demo.tsx`);
- preserves the documented import contract `@/components/ui/<name>` that the registry and docs assume.

If a project puts UI primitives elsewhere, registry components silently break on their next `@/components/ui/...` import — that's why the folder must exist (or be aliased) before copy-pasting.

## Files

| Path | Purpose |
| --- | --- |
| `src/components/ui/footer-section.tsx` | The footer component (copied from the spec) |
| `src/demo.tsx` | Demo page mounting the footer |
| `src/main.tsx`, `src/index.css`, `vite.config.ts`, `tsconfig*.json`, `components.json` | Stack/theme configuration |
| `src/lib/utils.ts` | `cn()` helper used by shadcn components |

## Integration with the Python server

The existing server (`sandbox_engine/ui_next`) serves the vanilla landing at `/` and the studio at `/app`. To use this footer in the vanilla landing instead of a separate app, either:

1. add a build step — run `npm run build` here and serve `dist/` (replace the vanilla footer section), or
2. keep both stacks — mount this app under a new path (e.g. a Vite `base` and a server route).