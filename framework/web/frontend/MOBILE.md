# Mobile UI — armadilhas conhecidas

> Lições compiladas de incidentes reais (D-93, D-94, mobile pass D-XX).
> Antes de mexer em layout/UI da PWA, leia. Custo de revisar < custo de
> caçar overflow horizontal num device físico.

## Breakpoints (Tailwind config)

```
xs  480px   — split icon-only / icon+text em botões compactos
sm  640px   — divisor mobile/desktop principal (a maior parte das media queries)
md  768px   — sidebar desktop aparece; bottom nav some
lg  1024px  — só usado em layouts amplos
```

**Default = mobile (<640px).** Sempre escreva os estilos pra mobile primeiro;
adicione `sm:`/`md:` pra alargar.

## Armadilhas de layout (mobile)

### 1. `flex-wrap` + `truncate` é incompatível

`truncate` aplica `white-space: nowrap` que conflita com wrap. Truncate **só
em filhos individuais**, nunca no container flex-wrap.

```svelte
<!-- ✗ errado -->
<div class="flex flex-wrap truncate">
  <strong>{title}</strong>
  <span>...</span>
</div>

<!-- ✓ certo -->
<div class="flex flex-wrap min-w-0">
  <strong class="min-w-0 truncate">{title}</strong>
  <span class="...">...</span>
</div>
```

### 2. Group de botões com `shrink-0 ml-auto` viola viewport

Padrão antigo "ações sempre alinhadas à direita com `ml-auto shrink-0`"
estoura em telas estreitas. Botões somam mais que viewport e o pai não
wrappa.

```svelte
<!-- ✗ errado -->
<div class="flex items-center gap-2">
  <span class="truncate">{slug}</span>
  <span class="ml-auto flex shrink-0 gap-1">
    <button>Run</button> <button>Pause</button> <button>Edit</button> ...
  </span>
</div>

<!-- ✓ certo: row pai wrappa, botões ficam em linha própria em mobile -->
<div class="flex flex-wrap items-center gap-2">
  <span class="min-w-0 flex-1 truncate sm:flex-none">{slug}</span>
  <span class="flex w-full flex-wrap gap-1 sm:ml-auto sm:w-auto sm:shrink-0">
    <button>Run</button> ...
  </span>
</div>
```

### 3. Texto de botão hidden em mobile (icon-only)

Botões com texto + ícone ocupam ~80-100px cada. 4 botões = 400px > viewport.
Esconda texto < `xs`, mantenha ícone + `aria-label`.

```svelte
<button title="Run" aria-label="Run">
  <Play class="h-3.5 w-3.5" />
  <span class="hidden xs:inline">Run</span>
</button>
```

### 4. Containers `overflow-y-auto` precisam de `overflow-x-hidden`

Sem isso, qualquer filho mais largo que viewport aciona scroll horizontal
indesejado (a tela inteira "anda" pro lado).

```svelte
<!-- ✗ -->
<div class="flex-1 overflow-y-auto">

<!-- ✓ -->
<div class="flex-1 overflow-y-auto overflow-x-hidden">
```

### 5. Wrapper de `<aside w-full>` precisa `w-full min-w-0` em mobile

Aside dentro de flex item sem `w-full` no pai resolve via auto-content
(cresce com o conteúdo, vaza viewport).

```svelte
<!-- +layout.svelte: pattern correto -->
<div class="flex h-full w-full min-w-0 md:w-auto md:flex">
  <ConvListPane />  <!-- aside w-full md:w-sidebar dentro -->
</div>
```

### 6. `PageHeader` actions é `shrink-0` por contrato

[`PageHeader.svelte`](src/lib/components/ui/PageHeader.svelte) usa
`shrink-0` no slot de actions. **Não meta 3+ selects ali** — não cabe em
mobile. Filtros/toolbars pesados vão pra row separada abaixo do header,
com `flex-wrap`.

```svelte
<PageHeader title="Telemetry">
  {#snippet actions()}
    <button>Refresh</button>  <!-- só 1-2 itens curtos no header -->
  {/snippet}
</PageHeader>

<!-- toolbar separada abaixo, podendo wrappar -->
<div class="flex flex-wrap items-center gap-2 border-b px-3 py-2">
  <select>...</select>
  <select>...</select>
  <select>...</select>
</div>
```

### 7. Tab row com N tabs > viewport

5 tabs de ~100px cada = 500px > 390px (mobile). Use scroll horizontal
no row e `shrink-0 whitespace-nowrap` em cada tab.

```svelte
<div class="-mx-4 flex gap-1 overflow-x-auto border-b px-4">
  <button class="shrink-0 whitespace-nowrap ...">Tab 1</button>
  <button class="shrink-0 whitespace-nowrap ...">Tab 2</button>
  ...
</div>
```

### 8. `pb-bottomNav` ≠ `pb-safe-nav`

O shell ([`+layout.svelte`](src/routes/+layout.svelte)) reserva espaço
pra `<AppNav variant="bottom">` (que é `position: fixed`). Use
**`pb-safe-nav`** (`var(--bottom-nav-h) + env(safe-area-inset-bottom)`),
**não** `pb-bottomNav` (só 56px). Em devices com gesture bar (Android
nav, iOS home indicator) o inset é > 0 e conteúdo fica cortado debaixo
da nav se você esqueceu o `safe`.

A `AppNav` em si tem `pb-safe`, então conteúdo **dentro** dela respeita o
gesture bar. O reservado pelo pai precisa casar.

## Cache em dev (D-96)

**Bundles do Vite/SvelteKit já são hash-versioned** (`/_app/0.D_HAVXFH.js`).
Hash novo = URL novo = sempre fresh.

O `index.html` (entry) **não é versionado** — sem `Cache-Control`, browser
cacheia via heurística (10% do age) e mostra bundles velhos pós-rebuild.

[`framework/web/app/main.py`](app/main.py) seta:
- `/`, `/sw.js`, `/manifest.webmanifest`, spa fallback → `Cache-Control: no-cache`
- `/_app/*` → `public, max-age=31536000, immutable` (via middleware)
- `/static/*` → `public, max-age=3600`

**Não retire esses headers.** Sem eles, qualquer rebuild fica invisível pro
user até hard-reload.

Em dev, se ainda assim ver versão velha:
```bash
# 1. Forçar reload sem cache (recomendado primeiro):
#    Ctrl+Shift+R (Chrome/Firefox), ou DevTools > Disable cache (com devtools aberto).
# 2. Limpar service worker + caches do browser:
#    DevTools > Application > Service Workers > Unregister + Clear storage.
# 3. URL com query string:
#    https://your-instance.example.com/?nocache=N
```

## Validação

Antes de fechar PR mobile-relacionado, rode em viewport real ou Playwright:

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
// doc > 0 = page tem scroll horizontal indesejado.
// aside.w > 390 = aside vazou viewport.
```

Confirme também que:
- bottom da página tem folga até `<AppNav>` (botões de form não cortados).
- header da página NÃO precisa scroll lateral pra ler título.
- tabs/toolbars que estouram **scrollam horizontalmente** (não wrappam
  pra 2ª linha torta nem somem).
