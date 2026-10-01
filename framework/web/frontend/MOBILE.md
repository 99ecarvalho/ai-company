# Mobile UI — known pitfalls

> Lessons compiled from real incidents (D-93, D-94, mobile pass D-XX).
> Read this before touching PWA layout/UI. The cost of reviewing < the cost of
> hunting horizontal overflow on a physical device.

## Breakpoints (Tailwind config)

```
xs  480px   — split icon-only / icon+text on compact buttons
sm  640px   — main mobile/desktop divider (most media queries)
md  768px   — desktop sidebar appears; bottom nav disappears
lg  1024px  — only used in wide layouts
```

**Default = mobile (<640px).** Always write styles for mobile first;
add `sm:`/`md:` to widen.

## Layout pitfalls (mobile)

### 1. `flex-wrap` + `truncate` are incompatible

`truncate` applies `white-space: nowrap`, which conflicts with wrap. Truncate
**only individual children**, never the flex-wrap container.

```svelte
<!-- ✗ wrong -->
<div class="flex flex-wrap truncate">
  <strong>{title}</strong>
  <span>...</span>
</div>

<!-- ✓ right -->
<div class="flex flex-wrap min-w-0">
  <strong class="min-w-0 truncate">{title}</strong>
  <span class="...">...</span>
</div>
```

### 2. Button group with `shrink-0 ml-auto` breaks the viewport

The old pattern "actions always right-aligned with `ml-auto shrink-0`"
overflows on narrow screens. Buttons add up to more than the viewport and the
parent does not wrap.

```svelte
<!-- ✗ wrong -->
<div class="flex items-center gap-2">
  <span class="truncate">{slug}</span>
  <span class="ml-auto flex shrink-0 gap-1">
    <button>Run</button> <button>Pause</button> <button>Edit</button> ...
  </span>
</div>

<!-- ✓ right: parent row wraps, buttons get their own line on mobile -->
<div class="flex flex-wrap items-center gap-2">
  <span class="min-w-0 flex-1 truncate sm:flex-none">{slug}</span>
  <span class="flex w-full flex-wrap gap-1 sm:ml-auto sm:w-auto sm:shrink-0">
    <button>Run</button> ...
  </span>
</div>
```

### 3. Button text hidden on mobile (icon-only)

Buttons with text + icon take ~80-100px each. 4 buttons = 400px > viewport.
Hide the text below `xs`, keep the icon + `aria-label`.

```svelte
<button title="Run" aria-label="Run">
  <Play class="h-3.5 w-3.5" />
  <span class="hidden xs:inline">Run</span>
</button>
```

### 4. `overflow-y-auto` containers need `overflow-x-hidden`

Without it, any child wider than the viewport triggers unwanted horizontal
scroll (the whole screen "slides" sideways).

```svelte
<!-- ✗ -->
<div class="flex-1 overflow-y-auto">

<!-- ✓ -->
<div class="flex-1 overflow-y-auto overflow-x-hidden">
```

### 5. Wrapper of `<aside w-full>` needs `w-full min-w-0` on mobile

An aside inside a flex item without `w-full` on the parent resolves via
auto-content (grows with the content, leaks out of the viewport).

```svelte
<!-- +layout.svelte: correct pattern -->
<div class="flex h-full w-full min-w-0 md:w-auto md:flex">
  <ConvListPane />  <!-- aside w-full md:w-sidebar inside -->
</div>
```

### 6. `PageHeader` actions are `shrink-0` by contract

[`PageHeader.svelte`](src/lib/components/ui/PageHeader.svelte) uses
`shrink-0` on the actions slot. **Don't put 3+ selects there** — they don't fit
on mobile. Heavy filters/toolbars go in a separate row below the header,
with `flex-wrap`.

```svelte
<PageHeader title="Telemetry">
  {#snippet actions()}
    <button>Refresh</button>  <!-- only 1-2 short items in the header -->
  {/snippet}
</PageHeader>

<!-- separate toolbar below, allowed to wrap -->
<div class="flex flex-wrap items-center gap-2 border-b px-3 py-2">
  <select>...</select>
  <select>...</select>
  <select>...</select>
</div>
```

### 7. Tab row with N tabs > viewport

5 tabs of ~100px each = 500px > 390px (mobile). Use horizontal scroll
on the row and `shrink-0 whitespace-nowrap` on each tab.

```svelte
<div class="-mx-4 flex gap-1 overflow-x-auto border-b px-4">
  <button class="shrink-0 whitespace-nowrap ...">Tab 1</button>
  <button class="shrink-0 whitespace-nowrap ...">Tab 2</button>
  ...
</div>
```

### 8. `pb-bottomNav` ≠ `pb-safe-nav`

The shell ([`+layout.svelte`](src/routes/+layout.svelte)) reserves space
for `<AppNav variant="bottom">` (which is `position: fixed`). Use
**`pb-safe-nav`** (`var(--bottom-nav-h) + env(safe-area-inset-bottom)`),
**not** `pb-bottomNav` (only 56px). On devices with a gesture bar (Android
nav, iOS home indicator) the inset is > 0 and content gets cut off under
the nav if you forgot the `safe`.

`AppNav` itself has `pb-safe`, so content **inside** it respects the
gesture bar. The space reserved by the parent has to match.

## Cache in dev (D-96)

**Vite/SvelteKit bundles are already hash-versioned** (`/_app/0.D_HAVXFH.js`).
New hash = new URL = always fresh.

`index.html` (entry) **is not versioned** — without `Cache-Control`, the browser
caches it heuristically (10% of age) and shows old bundles after a rebuild.

[`framework/web/app/main.py`](app/main.py) sets:
- `/`, `/sw.js`, `/manifest.webmanifest`, spa fallback → `Cache-Control: no-cache`
- `/_app/*` → `public, max-age=31536000, immutable` (via middleware)
- `/static/*` → `public, max-age=3600`

**Don't remove these headers.** Without them, any rebuild stays invisible to the
user until a hard reload.

In dev, if you still see an old version:
```bash
# 1. Force reload without cache (recommended first):
#    Ctrl+Shift+R (Chrome/Firefox), or DevTools > Disable cache (with devtools open).
# 2. Clear the browser's service worker + caches:
#    DevTools > Application > Service Workers > Unregister + Clear storage.
# 3. URL with a query string:
#    https://your-instance.example.com/?nocache=N
```

## Validation

Before closing a mobile-related PR, run it on a real viewport or Playwright:

```js
await page.setViewportSize({ width: 390, height: 844 });  // iPhone 14
await page.goto(URL);
const overflow = await page.evaluate(() => ({
  doc: document.documentElement.scrollWidth - document.documentElement.clientWidth,
  asides: [...document.querySelectorAll('aside')].map(a => ({
    w: a.getBoundingClientRect().width,
    parent: a.parentElement?.className,
  })),
}));
// doc > 0 = page has unwanted horizontal scroll.
// aside.w > 390 = aside leaked out of the viewport.
```

Also confirm that:
- the bottom of the page has room before `<AppNav>` (form buttons not cut off).
- the page header does NOT need sideways scroll to read the title.
- tabs/toolbars that overflow **scroll horizontally** (they don't wrap
  onto a crooked 2nd line or disappear).
