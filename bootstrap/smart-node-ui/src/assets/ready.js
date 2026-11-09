/* ready.js: a page shows its content once, when its first data has arrived.
   Cards were drawn empty with a spinner and grew as their data came in, so the
   page jumped on every load. Loaded first in <head>, it
   counts the requests the page makes while it loads; when none has been open
   for 80 ms (the answer read and drawn), <html> gets is-ready and shell.css fades
   the content in. A page that takes longer is shown after one second anyway. */
/* It also puts the modules seen on the last visit on <html> (data-mods, kept by
   shell.js), so the menu draws its module entries at once instead of growing
   when the services list arrives. */
(function () {
  try { var m = localStorage.getItem('sn-panel-mods'); if (m != null) document.documentElement.setAttribute('data-mods', m); } catch (e) {}
})();
(function () {
  'use strict';
  var open = 0, timer = 0, done = false, fetch0 = window.fetch;
  function ready() {
    if (done) return;
    done = true;
    window.fetch = fetch0;
    document.documentElement.classList.add('is-ready');
  }
  function settle() { clearTimeout(timer); if (open === 0) timer = setTimeout(ready, 80); }
  window.fetch = function () {
    open++;
    clearTimeout(timer);
    return fetch0.apply(this, arguments).finally(function () { open--; settle(); });
  };
  document.addEventListener('DOMContentLoaded', settle);
  setTimeout(ready, 1000);
})();
