// Smart Node Studio -- message formatter.
//
// Renders agent message content into safe HTML. Different models / specialists
// may format differently, so this is a registry: register a formatter under a
// key (specialist slug or model id) and format() picks it by ctx, falling back
// to the markdown default. Everything is escaped first, so registered
// formatters build on already-safe text and never inject raw user/model HTML.
//
//   snFormatter.register('some-specialist', (raw, ctx) => '<...>');
//   snFormatter.format(content, { specialist, model, role });
(function () {
  const _registry = new Map();

  const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  // Inline spans inside an already-escaped line: code, bold, italic, links.
  function inline(raw) {
    let s = esc(raw);
    s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>');
    s = s.replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>');
    s = s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/(^|[^*])\*(?!\s)([^*\n]+?)\*/g, '$1<em>$2</em>');
    // Links: [text](http...) only -- restrict scheme to avoid javascript: URIs.
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    // A page of this site, as the node's own messages link to it. One slash: two
    // would be another site.
    s = s.replace(/\[([^\]]+)\]\((\/[^\/)\s][^)\s]*)\)/g, '<a href="$2">$1</a>');
    // Bare URLs -> clickable. Skip ones already inside an href (preceded by " or >)
    // by only matching after start/whitespace/'('. Strip trailing sentence punctuation.
    s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, function (m, pre, url) {
      var tail = ''; var mt = url.match(/[.,;:!?]+$/);
      if (mt) { tail = mt[0]; url = url.slice(0, -tail.length); }
      return pre + '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + url + '</a>' + tail;
    });
    return s;
  }

  // Renderable HTML block: Preview (sandboxed iframe) / Source (escaped code).
  // `code` is the raw HTML. srcdoc carries it as an attribute; esc() escapes
  // &, <, >, " -- attribute decoding reverses all four, so the iframe receives
  // the original markup verbatim. sandbox="allow-same-origin" (NO allow-scripts)
  // lets the parent measure height on load without ever executing embedded JS.
  function htmlPreview(code) {
    const src = esc(code);
    // Sandboxed, scripts off; still an image or a stylesheet would load on render, with no
    // click, and carry whatever its address holds out of the node.
    const framed = esc(`<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:">${code}`);
    return '<div class="htmlprev" data-mode="preview">'
      + '<div class="htmlprev__bar">'
      + '<button type="button" class="htmlprev__tab is-active" data-mode="preview">Preview</button>'
      + '<button type="button" class="htmlprev__tab" data-mode="source">Source</button>'
      + '</div>'
      + `<iframe class="htmlprev__frame" sandbox="allow-same-origin" srcdoc="${framed}"></iframe>`
      + `<pre class="htmlprev__src"><code class="lang-html">${src}</code></pre>`
      + '</div>';
  }

  // Seed the sandboxed preview with the app's theme colors as DEFAULTS, so an
  // unstyled document (no background/color of its own) follows light/dark
  // instead of rendering on the UA's white canvas. Injected as the first rule
  // in the doc head -> any styling the embedded HTML carries overrides it.
  // Re-runnable: keyed by id so a theme switch just rewrites the same node.
  function themeFrame(frame) {
    const doc = frame && frame.contentDocument;
    if (!doc) return;
    const root = getComputedStyle(document.documentElement);
    const bg = (root.getPropertyValue('--sn-bg') || '').trim() || getComputedStyle(document.body).backgroundColor;
    const ink = (root.getPropertyValue('--sn-ink') || '').trim() || getComputedStyle(document.body).color;
    const scheme = document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
    const head = doc.head || doc.documentElement;
    let st = doc.getElementById('sn-theme-base');
    if (!st) { st = doc.createElement('style'); st.id = 'sn-theme-base'; head.insertBefore(st, head.firstChild); }
    st.textContent = `html{color-scheme:${scheme};background:${bg};color:${ink};}`;
  }

  // Re-apply theme colors to every already-rendered preview (call on theme switch).
  function themeHtmlPreviews(root) {
    (root || document).querySelectorAll('.htmlprev__frame').forEach((f) => {
      try { themeFrame(f); } catch (_) { /* cross-origin guard */ }
    });
  }

  // Wire Preview/Source toggles + auto-size iframes inside a rendered root.
  // Call after inserting markdown() output into the DOM. Idempotent per node.
  function bindHtmlPreviews(root) {
    if (!root) return;
    root.querySelectorAll('.htmlprev').forEach((box) => {
      if (box.dataset.bound) return;
      box.dataset.bound = '1';
      const frame = box.querySelector('.htmlprev__frame');
      box.querySelectorAll('.htmlprev__tab').forEach((tab) => {
        tab.addEventListener('click', () => {
          const mode = tab.getAttribute('data-mode');
          box.setAttribute('data-mode', mode); // CSS [data-mode] toggles which pane shows
          box.querySelectorAll('.htmlprev__tab').forEach((t) =>
            t.classList.toggle('is-active', t === tab));
        });
      });
      // Seed theme colors + auto-fit height (same-origin srcdoc, scripts off).
      if (frame) {
        frame.addEventListener('load', () => {
          try {
            themeFrame(frame);
            const h = frame.contentDocument.documentElement.scrollHeight;
            if (h) frame.style.height = Math.min(h + 8, 600) + 'px';
          } catch (_) { /* cross-origin guard -- keep CSS fallback height */ }
        });
      }
    });
  }

  // Block-level markdown: fenced code, headings, lists, blockquotes, paragraphs.
  // Deliberately small -- covers what the node's agents actually emit.
  function markdown(raw) {
    const src = String(raw ?? '');
    // Split out fenced code blocks first so their contents are never parsed.
    const parts = src.split(/(```[a-zA-Z0-9]*\n?[\s\S]*?```)/g);
    return parts.map((part, i) => {
      if (i % 2 === 1) {
        const m = part.match(/^```([a-zA-Z0-9]*)\n?([\s\S]*?)```$/);
        const lang = m ? m[1] : '';
        const code = m ? m[2].replace(/\n$/, '') : part.slice(3, -3);
        // An ```html``` block is a renderable document: show a Preview/Source
        // toggle. Preview goes into a sandboxed iframe (no allow-scripts -> the
        // embedded markup cannot run JS or reach the parent), so rendering
        // model/agent-authored HTML stays XSS-safe.
        if (lang === 'html' || lang === 'htm') return htmlPreview(code);
        return `<pre><code${lang ? ` class="lang-${lang}"` : ''}>${esc(code)}</code></pre>`;
      }
      const out = [];
      let listType = null; // 'ul' | 'ol' | null
      let listBlank = false; // a blank line came after the last item of the open list
      let para = [];       // lines of the paragraph being built
      const closeList = () => { listBlank = false; if (listType) { out.push(`</${listType}>`); listType = null; } };
      // A paragraph runs until a blank line or another block, which is what
      // markdown says. This loop used to emit one <p> per line, so a source that
      // is hard-wrapped -- anything written by a person rather than by an agent
      // -- came out as a column of one-line paragraphs with a margin between
      // each. Agents write a paragraph as one long line, which is why nobody
      // saw it. Lines are joined BEFORE inline parsing: **bold** can straddle a
      // wrap, and parsing line by line leaves the stars on the page.
      // ...with one exception: a line break the writer MEANT. An agent that puts
      //     Task link: https://example.com/tasks/42
      //     Design: https://example.com/docs/plan
      // on two lines wants two lines, and joining them produced one line with
      // two links run together.
      // A wrap and a meant break look different: a hard-wrapped line is full,
      // about 70 to 80 characters, and stops mid-sentence; a meant one is short,
      // or ends with a link. Those two keep their break, everything else is
      // still joined, so hand-wrapped prose stays one paragraph.
      const meantBreak = (line) => {
        const s = line.trim();
        return s.length < 60 || /https?:\/\/\S+$/.test(s);
      };
      const flushPara = () => {
        if (!para.length) return;
        let s = para[0];
        for (let k = 1; k < para.length; k++) s += (meantBreak(para[k - 1]) ? '\n' : ' ') + para[k];
        out.push(`<p>${inline(s).replace(/\n/g, '<br>')}</p>`);
        para = [];
      };
      // GFM table helpers. A table = header row + separator (|---|---|) + body rows.
      const isRow = (l) => l.indexOf('|') !== -1 && /\S/.test(l);
      const isSep = (l) => /^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)+\|?\s*$/.test(l);
      const splitRow = (l) => l.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim());
      const aligns = (sep) => splitRow(sep).map((c) => {
        const L = c.startsWith(':'), R = c.endsWith(':');
        return (L && R) ? 'center' : R ? 'right' : L ? 'left' : '';
      });
      const lines = part.split('\n');
      for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        if (isRow(line) && i + 1 < lines.length && isSep(lines[i + 1])) {
          flushPara();
          closeList();
          const head = splitRow(line);
          const al = aligns(lines[i + 1]);
          const sty = (k) => al[k] ? ` style="text-align:${al[k]}"` : '';
          const body = [];
          let j = i + 2;
          while (j < lines.length && isRow(lines[j]) && !isSep(lines[j])) { body.push(splitRow(lines[j])); j++; }
          let t = '<table><thead><tr>' + head.map((c, k) => `<th${sty(k)}>${inline(c)}</th>`).join('') + '</tr></thead>';
          if (body.length) {
            t += '<tbody>' + body.map((r) => '<tr>' + head.map((_, k) =>
              `<td${sty(k)}>${inline(r[k] != null ? r[k] : '')}</td>`).join('') + '</tr>').join('') + '</tbody>';
          }
          out.push(t + '</table>');
          i = j - 1;
          continue;
        }
        // A blank line does not end a list when the next line is another item of the
        // same kind (a loose list, items set apart by blank lines): it ended the list
        // there, so every item became a list of its own and a numbered one read
        // 1. 1. 1.. The list ends at the
        // first line that is not another item of it.
        if (listBlank && line.trim()) {
          listBlank = false;
          const same = listType === 'ul' ? /^\s*[-*+]\s+/.test(line) : /^\s*\d+[.)]\s+/.test(line);
          if (!same) closeList();
        }
        const h = line.match(/^(#{1,6})\s+(.*)$/);
        if (h) { flushPara(); closeList(); out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); continue; }
        const ul = line.match(/^\s*[-*+]\s+(.*)$/);
        if (ul) { flushPara(); if (listType !== 'ul') { closeList(); out.push('<ul>'); listType = 'ul'; } out.push(`<li>${inline(ul[1])}</li>`); continue; }
        const ol = line.match(/^\s*(\d+)[.)]\s+(.*)$/);
        if (ol) {
          flushPara();
          // A list that starts at 3 starts at 3.
          if (listType !== 'ol') { closeList(); out.push(Number(ol[1]) > 1 ? `<ol start="${Number(ol[1])}">` : '<ol>'); listType = 'ol'; }
          out.push(`<li>${inline(ol[2])}</li>`);
          continue;
        }
        const bq = line.match(/^\s*>\s?(.*)$/);
        if (bq) { flushPara(); closeList(); out.push(`<blockquote>${inline(bq[1])}</blockquote>`); continue; }
        // A wrapped list item. Continuation lines are indented and carry no
        // marker, so they used to close the list and land as a paragraph of
        // their own: one bullet became a bullet plus a stray line under it,
        // on every hand-wrapped document.
        if (listType && /^\s+\S/.test(line)) {
          const last = out.length - 1;
          if (out[last] && out[last].slice(0, 4) === '<li>') {
            out[last] = out[last].slice(0, -5) + ' ' + inline(line.trim()) + '</li>';
            continue;
          }
        }
        if (!line.trim()) { flushPara(); if (listType) listBlank = true; continue; }
        closeList();
        para.push(line);
      }
      flushPara();
      closeList();
      return out.join('');
    }).join('');
  }

  // Plain text: escape + keep line breaks. The pre-formatter fallback.
  function plain(raw) { return esc(raw).replace(/\n/g, '<br>'); }

  function register(key, fn) { _registry.set(String(key), fn); }

  function format(content, ctx = {}) {
    const key = (ctx.specialist != null ? String(ctx.specialist) : '')
      || (ctx.model != null ? String(ctx.model) : '');
    const fn = (key && _registry.get(key)) || _registry.get('default');
    try { return fn(content, ctx); }
    catch (e) { return plain(content); }
  }

  register('default', markdown);
  register('plain', plain);
  // System / status lines come through brin tagged specialist '_system'; keep
  // them as plain text so terse status replies are not markdown-mangled.
  register('_system', plain);

  window.snFormatter = { register, format, markdown, plain, esc, bindHtmlPreviews, themeHtmlPreviews };
})();
