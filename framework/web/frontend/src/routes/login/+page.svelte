<script lang="ts">
  import { authLogin, ApiError } from '$lib/api';

  let email = $state('');
  let password = $state('');
  let busy = $state(false);
  let error = $state<string | null>(null);

  async function submit(e: Event) {
    e.preventDefault();
    if (!email.trim() || !password) return;
    busy = true;
    error = null;
    try {
      await authLogin(email.trim(), password);
      // Full reload (not goto/SPA): `+layout.svelte` only calls
      // startConversationsStream/startTasksStream in onMount, and onMount
      // doesn't re-run on an SPA navigation. Without a reload, the streams would
      // be dead in the first post-login session — the sidebar would show
      // "No conversations yet" until the user refreshed manually.
      window.location.assign('/');
    } catch (e) {
      error = e instanceof ApiError ? e.detail : String(e);
      busy = false;
    }
  }
</script>

<svelte:head>
  <title>Login · Agents</title>
</svelte:head>

<main class="flex min-h-dvh items-center justify-center bg-bg p-4">
  <form
    onsubmit={submit}
    class="flex w-full max-w-sm flex-col gap-4 rounded-lg border border-border bg-panel p-6 shadow-2xl"
  >
    <h1 class="text-lg font-semibold">Sign in</h1>
    <label class="grid gap-1 text-xs">
      <span class="text-muted">Email</span>
      <input
        type="email"
        bind:value={email}
        autocomplete="username"
        class="min-h-tap rounded-md border border-border bg-panel2 px-3 py-2 text-base focus:border-accent focus:outline-none"
      />
    </label>
    <label class="grid gap-1 text-xs">
      <span class="text-muted">Password</span>
      <input
        type="password"
        bind:value={password}
        autocomplete="current-password"
        class="min-h-tap rounded-md border border-border bg-panel2 px-3 py-2 text-base focus:border-accent focus:outline-none"
      />
    </label>
    {#if error}
      <p class="text-xs text-accent2">{error}</p>
    {/if}
    <button
      type="submit"
      disabled={busy || !email.trim() || !password}
      class="min-h-tap rounded-md bg-accent px-4 py-2.5 text-sm font-semibold text-on-accent hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-50"
    >
      {busy ? 'Signing in…' : 'Sign in'}
    </button>
  </form>
</main>
