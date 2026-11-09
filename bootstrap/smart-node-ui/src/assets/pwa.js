/* pwa.js: registers the service worker, which makes the panel installable (Add to Home screen, Install app). One
   file for every page; the worker itself is /sw.js and keeps only an offline page. */
if ('serviceWorker' in navigator) {
  window.addEventListener('load', function () {
    navigator.serviceWorker.register('/sw.js').catch(function () { /* the panel works the same without it */ });
  });
}
