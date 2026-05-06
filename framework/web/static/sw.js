// Service worker: receives push + shows notification + handles click.
// Scope "/" — served by the backend at /sw.js.
self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (_) {
    data = { title: "Notification", body: event.data ? event.data.text() : "" };
  }
  const title = data.title || "Agents";
  const options = {
    body: data.body || "",
    icon: "/static/icon-192.png",
    badge: "/static/icon-192.png",
    tag: data.tag || "default",
    data: data.data || {},
    requireInteraction: !!data.requireInteraction,
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = event.notification.data?.url || "/";
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
      for (const c of list) {
        if (c.url.includes(url) && "focus" in c) return c.focus();
      }
      if (clients.openWindow) return clients.openWindow(url);
    }),
  );
});
