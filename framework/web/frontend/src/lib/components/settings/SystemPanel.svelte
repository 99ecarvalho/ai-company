<script lang="ts">
  import { onMount } from 'svelte';
  import { RefreshCw, Save, Bell, BellOff, Loader2, AlertTriangle, Trash2 } from 'lucide-svelte';
  import {
    getWebSettings,
    updateWebSettings,
    generateVapid,
    clearVapid,
    listStreams,
    type WebSettingsGeneral,
    type StreamInfo
  } from '$lib/api';
  import { logEvent } from '$lib/stores/ui';

  let settings = $state<WebSettingsGeneral | null>(null);
  let streams = $state<StreamInfo[]>([]);
  let loading = $state(false);

  // Editable drafts
  let defaultStreamDraft = $state('');
  let contactEmailDraft = $state('');
  let savingGeneral = $state(false);

  // VAPID actions
  let generating = $state(false);
  let clearing = $state(false);
  let confirmRotate = $state(false);

  $effect(() => {
    if (settings) {
      defaultStreamDraft = settings.default_stream;
      contactEmailDraft = settings.vapid.contact_email;
    }
  });

  const dirty = $derived(
    settings !== null &&
      (defaultStreamDraft !== settings.default_stream ||
        contactEmailDraft !== settings.vapid.contact_email)
  );

  onMount(() => {
    void refresh();
  });

  async function refresh() {
    loading = true;
    try {
      const [s, sr] = await Promise.all([getWebSettings(), listStreams()]);
      settings = s;
      streams = sr.streams.filter((x) => x.is_active);
    } catch (e) {
      logEvent(`load web-settings: ${e}`, 'err');
    } finally {
      loading = false;
    }
  }

  async function saveGeneral() {
    if (!settings) return;
    savingGeneral = true;
    try {
      const patch: { default_stream?: string; vapid_contact_email?: string } = {};
      if (defaultStreamDraft !== settings.default_stream) {
        patch.default_stream = defaultStreamDraft;
      }
      if (contactEmailDraft !== settings.vapid.contact_email && contactEmailDraft) {
        patch.vapid_contact_email = contactEmailDraft;
      }
      settings = await updateWebSettings(patch);
      logEvent('Settings saved', 'ok');
    } catch (e) {
      logEvent(`save settings: ${e}`, 'err');
    } finally {
      savingGeneral = false;
    }
  }

  async function generate(force: boolean) {
    generating = true;
    try {
      const r = await generateVapid({
        force,
        contact_email: contactEmailDraft || undefined
      });
      if (r.subscriptions_invalidated > 0) {
        logEvent(
          `VAPID rotated · ${r.subscriptions_invalidated} subscription(s) invalidated`,
          'ok'
        );
      } else {
        logEvent('VAPID keys generated', 'ok');
      }
      await refresh();
      confirmRotate = false;
    } catch (e) {
      // Backend returns 409 when it already exists and force=false — UI turns it into a prompt
      const msg = String(e);
      if (msg.includes('409') && !force) {
        confirmRotate = true;
      } else {
        logEvent(`generate VAPID: ${e}`, 'err');
      }
    } finally {
      generating = false;
    }
  }

  async function clear() {
    if (!confirm('Disable push notifications and remove the keypair?\n\nAll existing subscriptions will be invalidated.')) {
      return;
    }
    clearing = true;
    try {
      const r = await clearVapid();
      logEvent(
        `VAPID disabled · ${r.subscriptions_invalidated} subscription(s) invalidated`,
        'ok'
      );
      await refresh();
    } catch (e) {
      logEvent(`clear VAPID: ${e}`, 'err');
    } finally {
      clearing = false;
    }
  }
</script>

<div class="space-y-6">
  <div class="flex items-center justify-between gap-2">
    <div>
      <h2 class="text-base font-semibold text-fg">System settings</h2>
      <p class="text-xs text-muted">
        Per-instance config persisted in <code class="text-fg">web.app_settings</code>. Falls back to env
        vars when not set.
      </p>
    </div>
    <button
      type="button"
      class="inline-flex min-h-tap items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-xs font-medium text-muted hover:bg-elevated"
      onclick={refresh}
      disabled={loading}
    >
      <RefreshCw class="h-3.5 w-3.5 {loading ? 'animate-spin' : ''}" /> Refresh
    </button>
  </div>

  {#if loading && !settings}
    <div class="flex items-center gap-2 text-sm text-muted">
      <Loader2 class="h-4 w-4 animate-spin" /> Loading…
    </div>
  {:else if settings}
    <!-- Default stream -->
    <section class="space-y-2 rounded-lg border border-border bg-elevated p-4">
      <h3 class="text-sm font-semibold text-fg">Default stream</h3>
      <p class="text-xs text-muted">
        Stream pre-selected when creating new conversations or promoting tasks.
      </p>
      <div class="flex flex-col gap-2 sm:flex-row sm:items-center">
        <select
          class="min-h-tap rounded-md border border-border bg-bg px-3 py-1.5 text-sm text-fg"
          bind:value={defaultStreamDraft}
        >
          <option value="">— None —</option>
          {#each streams as s}
            <option value={s.name}>#{s.name}</option>
          {/each}
        </select>
        {#if settings.default_stream && !streams.some((s) => s.name === settings!.default_stream)}
          <span class="inline-flex items-center gap-1 text-xs text-warning">
            <AlertTriangle class="h-3.5 w-3.5" />
            Current value <code>#{settings.default_stream}</code> doesn't match any active stream
          </span>
        {/if}
      </div>
    </section>

    <!-- VAPID / Push -->
    <section class="space-y-3 rounded-lg border border-border bg-elevated p-4">
      <div class="flex items-center justify-between gap-2">
        <h3 class="text-sm font-semibold text-fg">Web push (VAPID)</h3>
        {#if settings.vapid.configured}
          <span class="inline-flex items-center gap-1 rounded-full bg-success/10 px-2 py-0.5 text-xs text-success">
            <Bell class="h-3 w-3" /> Enabled
          </span>
        {:else}
          <span class="inline-flex items-center gap-1 rounded-full bg-muted/10 px-2 py-0.5 text-xs text-muted">
            <BellOff class="h-3 w-3" /> Disabled
          </span>
        {/if}
      </div>

      {#if settings.vapid.configured}
        <div class="space-y-1 text-xs text-muted">
          <div>
            Public key:
            <code class="block break-all rounded bg-bg px-2 py-1 text-fg">{settings.vapid.public_key}</code>
          </div>
        </div>
      {:else}
        <p class="text-xs text-muted">
          No keypair configured. Generate one to enable push notifications when an agent calls
          <code>ask_human</code>.
        </p>
      {/if}

      <label class="block text-xs font-medium text-muted">
        Contact email (VAPID JWT <code>sub</code> claim)
        <input
          type="email"
          class="mt-1 block w-full rounded-md border border-border bg-bg px-3 py-1.5 text-sm text-fg"
          bind:value={contactEmailDraft}
          placeholder="admin@example.com"
        />
      </label>

      <div class="flex flex-wrap gap-2">
        {#if !settings.vapid.configured}
          <button
            type="button"
            class="inline-flex min-h-tap items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-on-accent hover:opacity-90 disabled:opacity-50"
            onclick={() => generate(false)}
            disabled={generating}
          >
            {#if generating}<Loader2 class="h-3.5 w-3.5 animate-spin" />{:else}<Bell class="h-3.5 w-3.5" />{/if}
            Generate VAPID keypair
          </button>
        {:else if !confirmRotate}
          <button
            type="button"
            class="inline-flex min-h-tap items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm font-medium text-fg hover:bg-bg disabled:opacity-50"
            onclick={() => (confirmRotate = true)}
            disabled={generating || clearing}
          >
            <RefreshCw class="h-3.5 w-3.5" /> Rotate keypair…
          </button>
          <button
            type="button"
            class="inline-flex min-h-tap items-center gap-1.5 rounded-md border border-danger/40 px-3 py-1.5 text-sm font-medium text-danger hover:bg-danger/10 disabled:opacity-50"
            onclick={clear}
            disabled={generating || clearing}
          >
            {#if clearing}<Loader2 class="h-3.5 w-3.5 animate-spin" />{:else}<Trash2 class="h-3.5 w-3.5" />{/if}
            Disable push
          </button>
        {:else}
          <div class="flex w-full flex-col gap-2 rounded-md border border-warning/40 bg-warning/5 p-3 text-xs">
            <div class="flex items-start gap-2 text-warning">
              <AlertTriangle class="h-4 w-4 shrink-0" />
              <span>
                Rotating invalidates ALL existing push subscriptions — every browser/device will
                need to re-subscribe.
              </span>
            </div>
            <div class="flex gap-2">
              <button
                type="button"
                class="inline-flex min-h-tap items-center gap-1.5 rounded-md bg-warning px-3 py-1.5 text-xs font-medium text-on-warning hover:opacity-90 disabled:opacity-50"
                onclick={() => generate(true)}
                disabled={generating}
              >
                {#if generating}<Loader2 class="h-3.5 w-3.5 animate-spin" />{:else}<RefreshCw class="h-3.5 w-3.5" />{/if}
                Confirm rotate
              </button>
              <button
                type="button"
                class="inline-flex min-h-tap items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-xs font-medium text-muted hover:bg-bg"
                onclick={() => (confirmRotate = false)}
                disabled={generating}
              >
                Cancel
              </button>
            </div>
          </div>
        {/if}
      </div>
    </section>

    <!-- Save button -->
    <div class="flex justify-end">
      <button
        type="button"
        class="inline-flex min-h-tap items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-on-accent hover:opacity-90 disabled:opacity-50"
        onclick={saveGeneral}
        disabled={!dirty || savingGeneral}
      >
        {#if savingGeneral}<Loader2 class="h-3.5 w-3.5 animate-spin" />{:else}<Save class="h-3.5 w-3.5" />{/if}
        Save
      </button>
    </div>
  {/if}
</div>
