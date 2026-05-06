import { readable } from 'svelte/store';

/**
 * Clock reativo — tick a cada 1s, emite Date.now() em ms.
 *
 * Usado por componentes que renderizam "ha Xs" / "19s atras" sem refetch:
 * em vez de polling que re-baixava a lista so pra re-render, mantemos o
 * dado em memoria (SSE-triggered) e so re-avaliamos o formatter quando
 * `$now` muda.
 *
 * Pattern de uso:
 *   ```
 *   import { now } from '$lib/stores/clock';
 *   import { fmtAge } from '$lib/services/format';
 *   ...
 *   <span>{fmtAge(conv.last_activity, $now)}</span>
 *   ```
 *
 * O readable store so roda o setInterval quando tem subscriber ativo
 * (lazy) e limpa quando o ultimo desinscreve — zero custo em rotas sem
 * consumo.
 */
export const now = readable<number>(Date.now(), (set) => {
  set(Date.now());
  const id = setInterval(() => set(Date.now()), 1000);
  return () => clearInterval(id);
});
