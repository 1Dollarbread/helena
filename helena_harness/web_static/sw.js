// Minimal service worker for HELENA's web HUD — just enough to receive a
// Web Push event while the tab isn't focused and show a notification for
// it. No caching, no offline support; this isn't a PWA, just a delivery
// point for pushes the FastAPI server sends via helena_harness/push.py.

self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('push', (event) => {
  let data = { title: 'H.E.L.E.N.A', body: 'A turn finished.', tag: 'helena-turn' };
  if (event.data) {
    try { data = { ...data, ...event.data.json() }; } catch (e) { /* fall back to defaults */ }
  }
  event.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      tag: data.tag,
      icon: undefined,
      renotify: true,
    })
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
      for (const client of clientList) {
        if ('focus' in client) return client.focus();
      }
      if (self.clients.openWindow) return self.clients.openWindow('/');
    })
  );
});
