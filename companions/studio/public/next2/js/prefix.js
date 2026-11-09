// /next2 is the production page, copied to grow into the new workspace
//. Its scripts read and set the address as if they lived at
// the root ("/chats/<id>"); this keeps them under /next2 without touching their
// logic: what they set gets the prefix, and snPath() is the address without it.
(function () {
  // The shell is served at the root now; the prefix is kept only
  // when it was opened through an old /next2 link, so its moves stay under it.
  const P = /^\/next2(\/|$)/.test(location.pathname) ? '/next2' : '';
  if (!P) { window.snPath = () => location.pathname || '/'; return; }
  const wrap = (fn) => function (state, title, url) {
    if (typeof url === 'string' && url.startsWith('/') && url !== P && !url.startsWith(`${P}/`)) url = P + url;
    return fn.call(this, state, title, url);
  };
  history.pushState = wrap(history.pushState);
  history.replaceState = wrap(history.replaceState);
  window.snPath = () => location.pathname.replace(/^\/next2(?=\/|$)/, '') || '/';
})();
// The kit's toast (ui/components.css .sn-toast): the word after an action where
// nothing on screen changes, or what went wrong. One element, the last word wins.
window.snToast = (t) => {
  let el = document.querySelector('.sn-toast');
  if (!el) { el = document.createElement('div'); el.className = 'sn-toast'; el.setAttribute('role', 'status'); document.body.appendChild(el); }
  el.textContent = t; el.classList.add('is-on');
  clearTimeout(window.snToast.t); window.snToast.t = setTimeout(() => el.classList.remove('is-on'), 2400);
};
// A section a module brings shows only when that module is installed on the
// node (data-needs="<component>", as /next does): a node without the planner
// showed Planner, Tasks and Projects that led nowhere.
// window.snComponents resolves once the list is known, for a view that has to
// wait for it rather than draw first (Settings opened by a link drew too early)
window.snComponents = fetch('/api/node/components', { headers: { Accept: 'application/json' } })
  .then((r) => (r.ok ? r.json() : null))
  .then((j) => {
    if (!j) return;
    const on = new Set(j.installed || []);
    window.snInstalled = on;   // read by the agent card too (chat-shell.js)
    // also for what a view draws later (the Dashboard's task cards): it calls this
    window.snApplyNeeds = (root) => (root || document).querySelectorAll('[data-needs]').forEach((el) => el.classList.toggle('is-installed', on.has(el.dataset.needs)));
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => window.snApplyNeeds()); else window.snApplyNeeds();
  }).catch(() => {});
