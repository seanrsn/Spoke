// Service Worker for Brooklyn Bikery Admin Dashboard
// Handles push notifications for incoming messages

self.addEventListener('install', (event) => {
  console.log('Service Worker installed');
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  console.log('Service Worker activated');
  event.waitUntil(clients.claim());
});

// Handle push notifications
self.addEventListener('push', (event) => {
  console.log('Push notification received:', event);

  let data = {
    title: 'Brooklyn Bikery',
    body: 'New message received',
    icon: 'https://brooklynbikery.com/favicon.png',
    badge: 'https://brooklynbikery.com/favicon.png',
    data: {}
  };

  if (event.data) {
    try {
      const payload = event.data.json();
      data = { ...data, ...payload };
    } catch (e) {
      data.body = event.data.text();
    }
  }

  // Use unique tag per notification so iOS doesn't silently replace them
  const uniqueTag = 'msg-' + Date.now();

  event.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      icon: data.icon,
      badge: data.badge,
      tag: uniqueTag,
      data: data.data
    })
  );
});

// Handle notification click
self.addEventListener('notificationclick', (event) => {
  console.log('Notification clicked:', event.action);
  event.notification.close();

  const phone = event.notification.data && event.notification.data.phone;
  const name = event.notification.data && event.notification.data.name;
  const targetUrl = phone
    ? `/admin-dashboard.html?openThread=${encodeURIComponent(phone)}`
    : '/admin-dashboard.html';

  // Open or focus the dashboard
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
      // Check if dashboard is already open — focus it and send a message to open the thread
      for (const client of clientList) {
        if (client.url.includes('admin-dashboard') && 'focus' in client) {
          client.focus();
          if (phone) client.postMessage({ action: 'openThread', phone, name: name || null });
          return;
        }
      }
      // Open new window with phone param so dashboard opens the right thread
      if (clients.openWindow) {
        return clients.openWindow(targetUrl);
      }
    })
  );
});
