// Service worker for push notifications only -- this app has no offline/
// caching story (voicemail data is always fetched live), so this
// deliberately never intercepts fetch.

self.addEventListener("push", (event) => {
  let data = { title: "New voicemail", body: "" };
  try {
    data = event.data.json();
  } catch {
    // no payload, or not JSON -- fall back to the generic message above
  }
  event.waitUntil(
    self.registration.showNotification(data.title || "New voicemail", {
      body: data.body || "",
      data: { messageId: data.messageId },
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((windowClients) => {
      for (const client of windowClients) {
        if ("focus" in client) return client.focus();
      }
      if (clients.openWindow) return clients.openWindow(self.registration.scope);
    })
  );
});
