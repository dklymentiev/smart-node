<?php
/**
 * DOCI - Markdown Processing
 *
 * Functions for parsing and transforming markdown content.
 * Requires Parsedown and ParsedownExtended to be loaded.
 */

/**
 * Parse markdown content to HTML using ParsedownExtended
 *
 * @param string $markdown Markdown content
 * @return string HTML content
 */
function parse_markdown(string $markdown): string {
    // Mask code blocks and inline code first so {{embed: ...}} inside
    // backticks (used in docs to *describe* the shortcode) is not parsed
    // as a real embed. Restored after Parsedown runs.
    $codeStash = [];
    $markdown = preg_replace_callback(
        '/(^|\n)(```[^\n]*\n[\s\S]*?\n```)/m',
        function ($m) use (&$codeStash) {
            $token = "\x00DOCICODE" . count($codeStash) . "\x00";
            $codeStash[$token] = $m[2];
            return $m[1] . $token;
        },
        $markdown
    );
    $markdown = preg_replace_callback(
        '/`[^`\n]+`/',
        function ($m) use (&$codeStash) {
            $token = "\x00DOCICODE" . count($codeStash) . "\x00";
            $codeStash[$token] = $m[0];
            return $token;
        },
        $markdown
    );

    // Extract {{embed: path/to.html [| height=320]}} shortcodes BEFORE
    // Parsedown's safe-mode strips HTML. Replace each with a stable
    // placeholder; after Parsedown runs, expand the placeholders into
    // inline HTML (via render_html_inline). The `| height=NNN` hint is
    // accepted for backward compatibility and silently ignored.
    $embeds = [];
    $markdown = preg_replace_callback(
        '/\{\{embed:\s*([\w\-\.\/]+\.html)(?:\s*\|\s*height=(\d+))?\s*\}\}/',
        function ($m) use (&$embeds) {
            $token = 'DOCIEMBED' . count($embeds) . 'XX';
            $embeds[$token] = ['path' => $m[1], 'height' => isset($m[2]) ? (int)$m[2] : 320];
            return $token;
        },
        $markdown
    );

    // Restore code tokens before Parsedown so they parse as code normally.
    if (!empty($codeStash)) {
        foreach ($codeStash as $token => $original) {
            $markdown = str_replace($token, $original, $markdown);
        }
    }

    $parsedown = new ParsedownExtended();
    $parsedown->setSafeMode(true); // Escape HTML for security
    $html = $parsedown->text($markdown);

    if (!empty($embeds)) {
        foreach ($embeds as $token => $opts) {
            // Apply sanitize_path before any filesystem touch so a path
            // like '../etc/passwd' is rejected before realpath() has a
            // chance to follow it. realpath() check below stays as the
            // belt-and-suspenders second layer.
            require_once __DIR__ . '/validation.php';
            $safePath = sanitize_path($opts['path']);
            if ($safePath === '') {
                $html = str_replace('<p>' . $token . '</p>',
                    '<div class="doci-embed-error">Invalid embed path</div>', $html);
                $html = str_replace($token,
                    '<div class="doci-embed-error">Invalid embed path</div>', $html);
                continue;
            }
            $filePath = __DIR__ . '/../files/' . $safePath;
            if (file_exists($filePath) && is_file($filePath)) {
                $real = realpath($filePath);
                $filesRoot = realpath(__DIR__ . '/../files');
                if ($real !== false && strpos($real, $filesRoot) === 0) {
                    // Inline embed -- the embedded HTML's styles get scoped
                    // to .doci-html-inline by render_html_inline, and the
                    // body becomes part of the surrounding markdown page.
                    // The legacy `| height=NNN` hint is ignored (no iframe
                    // to size); the embed grows to fit its content.
                    $replacement = render_html_inline(file_get_contents($filePath));
                } else {
                    $replacement = '<div class="doci-embed-error">Embed path outside files/: ' . htmlspecialchars($opts['path']) . '</div>';
                }
            } else {
                $replacement = '<div class="doci-embed-error">Embed not found: ' . htmlspecialchars($opts['path']) . '</div>';
            }
            // Parsedown wraps lone tokens in <p>...</p>; handle both forms.
            $html = str_replace('<p>' . $token . '</p>', $replacement, $html);
            $html = str_replace($token, $replacement, $html);
        }
    }

    // Process footnotes [^N] and [^N]:
    $html = process_footnotes($html);

    // Allow <br> tags for line breaks in tables
    $html = str_replace('&lt;br&gt;', '<br>', $html);

    // Allow <mark> tags for highlighting (re-enable after safe mode escaping)
    $html = preg_replace('/&lt;mark&gt;(.+?)&lt;\/mark&gt;/s', '<mark>$1</mark>', $html);

    // Allow <span id="..."> for custom anchors
    $html = preg_replace('/&lt;span id="([a-z0-9\-]+)"&gt;&lt;\/span&gt;/', '<span id="$1"></span>', $html);

    // Replace AI pending placeholder with loading animation
    $html = preg_replace(
        '/&lt;!-- ai-pending:([a-z]+) --&gt;/',
        '<div class="ai-loading" data-model="$1">
            <div class="ai-loading-bar"></div>
            <div class="ai-loading-text">AI is thinking...</div>
        </div>',
        $html
    );

    // Add target="_blank" and rel="noopener noreferrer" to external links
    $html = preg_replace(
        '/<a href="(https?:\/\/[^"]+)"/',
        '<a href="$1" target="_blank" rel="noopener noreferrer"',
        $html
    );

    // Rewrite internal path links to stable GUID links: links survive
    // file renames and folder moves. Markdown authors keep writing
    // human-readable paths; the rendered href is /<guid>.
    $html = rewrite_links_to_guids($html);

    return $html;
}

/**
 * Process footnote markers and definitions
 * Converts [^N] to clickable superscript links
 * Converts [^N]: to anchored definitions (each on new line)
 *
 * @param string $html HTML content
 * @return string HTML with processed footnotes
 */
function process_footnotes(string $html): string {
    // Convert [^N] in text to clickable superscript (but not [^N]: definitions)
    $html = preg_replace(
        '/\[\^(\d+)\](?!:)/',
        '<sup class="footnote-ref"><a href="#fn-$1" id="fnref-$1" title="See footnote $1">$1</a></sup>',
        $html
    );

    // Convert [^N]: to anchored definition with line break before it
    $html = preg_replace(
        '/\[\^(\d+)\]:/',
        '<br><span class="footnote-def" id="fn-$1"><a href="#fnref-$1" title="Back to reference">^$1</a>:</span>',
        $html
    );

    // Remove the first <br> in footnotes section (after ## Sources or similar)
    $html = preg_replace(
        '/(<h2[^>]*>Sources<\/h2>\s*<p>)<br>/',
        '$1',
        $html
    );

    // Also handle case where <br> is right after <p>
    $html = preg_replace(
        '/<p><br>/',
        '<p>',
        $html
    );

    return $html;
}

/**
 * Detect whether document content is a full HTML page rather than Markdown.
 *
 * Sniffs the first non-whitespace bytes for an HTML5 doctype or <html> tag.
 * HTML documents are rendered in a sandboxed iframe instead of being escaped
 * through Parsedown SafeMode.
 *
 * @param string $content Raw document content
 * @return bool
 */
function doci_is_html_document(string $content): bool {
    // Drop a leading UTF-8 BOM and surrounding whitespace before sniffing.
    $head = preg_replace('/^\xEF\xBB\xBF/', '', ltrim($content));
    return (bool) preg_match('~^<!doctype\s+html|^<html[\s>]~i', $head);
}

/**
 * Render a full HTML document INLINE (no iframe) into the surrounding DOCI page.
 *
 * Used by the .html file route and by the {{embed:}} markdown shortcode.
 * The whole document is unwrapped:
 *   - `<style>` blocks are pulled out; selectors that match the document root
 *     (`:root`, `html`, `body`) are rewritten to `.doci-html-inline` so the
 *     embedded CSS variables / page-level styles scope to a single wrapper
 *     div instead of leaking into DOCI's own theme.
 *   - `<body>` contents are taken verbatim.
 *   - Everything else (`<!doctype>`, `<html>`, `<head>`, `<meta>`, `<title>`,
 *     `<link>`) is stripped — DOCI already owns those slots.
 *
 * No sandbox. The embedded JS runs in the page's origin like any other DOCI
 * script; only first-party HTML files (under `files/`) are routed here.
 *
 * @param string $html Raw HTML document content
 * @return string HTML fragment to drop into the DOCI page body
 */
function render_html_inline(string $html): string {
    // 1. Pull every <style> block out, rewriting root-level selectors.
    $styles = '';
    $html = preg_replace_callback(
        '#<style[^>]*>([\s\S]*?)</style>#i',
        function ($m) use (&$styles) {
            $css = $m[1];
            // Scope root-level selectors to the inline wrapper so CSS
            // variables and html/body styles don't leak into DOCI's
            // surrounding chrome. Order matters -- the more specific
            // joint selector first.
            $css = preg_replace('/(^|\}|\s)\s*html\s*,\s*body\s*\{/m', '$1.doci-html-inline {', $css);
            $css = preg_replace('/(^|\}|\s)\s*:root\s*\{/m', '$1.doci-html-inline {', $css);
            $css = preg_replace('/(^|\}|\s)\s*body\s*\{/m', '$1.doci-html-inline {', $css);
            $css = preg_replace('/(^|\}|\s)\s*html\s*\{/m', '$1.doci-html-inline {', $css);
            $styles .= $css . "\n";
            return '';
        },
        $html
    );

    // 2. Extract <body>...</body>; fall back to stripping shell tags.
    if (preg_match('#<body[^>]*>([\s\S]*?)</body>#i', $html, $m)) {
        $body = $m[1];
    } else {
        $body = preg_replace('#<!doctype[^>]*>#i', '', $html);
        $body = preg_replace('#</?(?:html|head)\b[^>]*>#i', '', $body);
        $body = preg_replace('#<(?:meta|link)\b[^>]*/?>#i', '', $body);
        $body = preg_replace('#<title[^>]*>[\s\S]*?</title>#i', '', $body);
    }

    // 3. XSS sanitisation. Inline embed renders into the page's own
    //    DOM, so any <script>, on* attribute, or javascript:/data:
    //    URI from the embed source executes with full origin
    //    privileges. Strip them. The 4-line allowlist preserves the
    //    intended use case (SVG charts, prerendered HTML widgets) and
    //    closes the stored-XSS vector raised by the audit.
    $body = doci_sanitize_inline_html($body);

    return '<div class="doci-html-inline">'
        . ($styles !== '' ? '<style>' . $styles . '</style>' : '')
        . $body
        . '</div>';
}

/**
 * Strip the four script-bearing affordances from an HTML fragment:
 * <script> tags, on* event-handler attributes, javascript:/vbscript:/
 * data:text/html URIs in href/src, and inline <iframe>s. Everything
 * else (SVGs, tables, divs, styles already scoped above) passes.
 *
 * Not a full sanitiser -- callers must use it only on content that
 * has already been bounded by realpath() to files/ (i.e. content the
 * operator deliberately added to the document tree, not arbitrary
 * user input). Belt-and-suspenders for the {{embed:}} shortcode.
 */
function doci_sanitize_inline_html(string $html): string {
    // Remove <script>...</script> and self-closing variants.
    $html = preg_replace('#<script\b[^>]*>[\s\S]*?</script\s*>#i', '', $html);
    $html = preg_replace('#<script\b[^>]*/?>#i', '', $html);
    // Strip on* event-handler attributes anywhere in any tag.
    $html = preg_replace('#\s+on[a-z]+\s*=\s*"[^"]*"#i', '', $html);
    $html = preg_replace("#\s+on[a-z]+\s*=\s*'[^']*'#i", '', $html);
    $html = preg_replace('#\s+on[a-z]+\s*=\s*[^\s>]+#i', '', $html);
    // Neutralise script-bearing URI schemes in href/src/xlink:href.
    $html = preg_replace(
        '#(href|src|xlink:href)\s*=\s*(["\']?)\s*(?:javascript|vbscript|data\s*:\s*text/html)\s*:[^"\'\s>]*\2#i',
        '$1=$2#blocked-uri$2',
        $html
    );
    // Drop iframes -- inline embeds should not nest navigation contexts.
    $html = preg_replace('#<iframe\b[^>]*>[\s\S]*?</iframe\s*>#i', '', $html);
    $html = preg_replace('#<iframe\b[^>]*/?>#i', '', $html);
    return $html;
}

/**
 * Rewrite internal path links inside markdown SOURCE to stable GUID links.
 *
 * Called on every save (UI, REST, CLI, MCP) so the file on disk only
 * ever contains GUID links. Authors and agents can write either form
 * (`[Inbox](/tour/03-inbox)` or `[Inbox](/<guid>)`); the controller
 * normalises to the second. After a rename or move, the GUID still
 * resolves -- the entire reason GUIDs exist as identifiers.
 *
 * Handles inline `[text](url)` and reference `[label]: url` syntaxes.
 * Leaves untouched: external URLs, anchors (#x), static paths
 * (/assets/, /api/), paths that don't resolve to a document.
 *
 * @param string $markdown
 * @return string
 */
function rewrite_md_links_to_guids(string $markdown): string {
    if (!function_exists('get_db')) {
        return $markdown;
    }

    // Pass 1: scan once to collect every URL that might need normalising.
    // Then one IN-clause query maps every resolvable path -> GUID in a
    // single round-trip. Pass 2 substitutes using the cached map and
    // falls through to per-link path_to_guid_url() only for the rare
    // edge cases the map misses.
    $urls = [];
    if (preg_match_all('/\[([^\]\n]+)\]\(\s*([^)\s]+)(\s+"[^"]*")?\s*\)/', $markdown, $im)) {
        foreach ($im[2] as $u) { $urls[$u] = true; }
    }
    if (preg_match_all('/^(\s{0,3}\[[^\]\n]+\]:\s+)(\S+)/m', $markdown, $rm)) {
        foreach ($rm[2] as $u) { $urls[$u] = true; }
    }
    $cache = $urls ? path_to_guid_url_map(array_keys($urls)) : [];

    // Inline: [text](url) or [text](url "title")
    $markdown = preg_replace_callback(
        '/\[([^\]\n]+)\]\(\s*([^)\s]+)(\s+"[^"]*")?\s*\)/',
        function ($m) use ($cache) {
            // First try to repair if it's a dead GUID URL (post-reseed safety).
            // This is rare and still per-link -- the common path is the
            // batched map below.
            $url = repair_dead_guid_link($m[2], $m[1]);
            $newUrl = $cache[$url] ?? ($url === $m[2] ? ($cache[$url] ?? path_to_guid_url($url)) : path_to_guid_url($url));
            return '[' . $m[1] . '](' . $newUrl . ($m[3] ?? '') . ')';
        },
        $markdown
    );

    // Reference: [label]: url  (line-anchored). Reference links have no
    // adjacent display text we can recover from; only forward-normalize.
    $markdown = preg_replace_callback(
        '/^(\s{0,3}\[[^\]\n]+\]:\s+)(\S+)/m',
        function ($m) use ($cache) {
            return $m[1] . ($cache[$m[2]] ?? path_to_guid_url($m[2]));
        },
        $markdown
    );

    return $markdown;
}

/**
 * Batched path -> GUID resolver. Given a list of URL strings, return
 * a map ['original-url' => '/guid<tail>', ...] for those that resolve
 * to a live document; URLs that don't resolve are absent from the map
 * (callers should fall through to leaving them unchanged).
 *
 * Internals mirror path_to_guid_url() but issue a single IN-clause
 * query for every candidate, matching the render-path batching
 * pattern in rewrite_links_to_guids().
 *
 * @param string[] $urls
 * @return array<string, string>
 */
function path_to_guid_url_map(array $urls): array {
    if (!function_exists('get_db')) {
        return [];
    }

    // Normalise each URL into (cleanPath, tail) and the two candidate
    // documents.path keys (key.md and key/index.md). Carry both
    // candidates back to the URL so we can pick the right one when
    // the IN-query returns matches.
    $candidates = [];
    $perUrl = [];
    foreach ($urls as $url) {
        if ($url === '' || $url[0] !== '/') continue;
        if (preg_match('#^/(assets|api|files/\.data)/#', $url)) continue;
        if (preg_match('#^/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(/|$|\?|\#)#', $url)) continue;

        $path = $url; $tail = '';
        if (($pos = strpos($path, '#')) !== false) { $tail = substr($path, $pos) . $tail; $path = substr($path, 0, $pos); }
        if (($pos = strpos($path, '?')) !== false) { $tail = substr($path, $pos) . $tail; $path = substr($path, 0, $pos); }
        $clean = preg_replace('/\.html$/', '', rtrim($path, '/'));
        $key = ltrim($clean, '/');
        if ($key === '' || $key === 'index') continue;

        $perUrl[$url] = ['key' => $key, 'tail' => $tail];
        $candidates[] = $key . '.md';
        $candidates[] = $key . '/index.md';
    }

    if (!$candidates) {
        return [];
    }
    $candidates = array_values(array_unique($candidates));

    try {
        $pdo = get_db();
        $ph = implode(',', array_fill(0, count($candidates), '?'));
        $stmt = $pdo->prepare("SELECT path, guid FROM documents WHERE path IN ($ph) AND deleted_at IS NULL");
        $stmt->execute($candidates);
        $pathMap = [];
        while ($row = $stmt->fetch(PDO::FETCH_ASSOC)) {
            $pathMap[$row['path']] = $row['guid'];
        }
    } catch (Throwable $e) {
        return [];
    }

    $out = [];
    foreach ($perUrl as $url => $meta) {
        // Prefer exact .md match over folder/index.md, matching
        // path_to_guid_url() semantics.
        foreach ([$meta['key'] . '.md', $meta['key'] . '/index.md'] as $cand) {
            if (isset($pathMap[$cand])) {
                $out[$url] = '/' . $pathMap[$cand] . $meta['tail'];
                break;
            }
        }
    }
    return $out;
}

/**
 * If $url is a /<guid> link whose GUID no longer exists in the documents
 * table (post-reseed, post-rename, post-restore), try to recover the
 * intended target from the visible link text and rewrite the URL to the
 * path form. A subsequent path_to_guid_url() call will then forward-
 * normalize it to the current live GUID.
 *
 * Heuristic: the link text in our docs usually contains the path hint
 * (e.g. `[/farm](...)`, `[`docs/05-api-reference`](...)`, `[CHANGELOG](...)`)
 * because that's what the writer typed before normalize-links rewrote
 * the URL. We strip backticks/slashes/extensions and look the hint up
 * against the documents table.
 *
 * If recovery fails, the dead URL is left intact and a warning is logged
 * so an operator notices.
 *
 * @param string $url       The URL portion of the markdown link.
 * @param string $linkText  The bracketed display text of the link.
 * @return string           Either the original $url, or a recovered /path form.
 */
/**
 * Locate a rendered-text selection inside its markdown source.
 *
 * Browser selections come back as plain text (no `**`, no backticks, with
 * `\n` joins where the source had `\n\n`). Straight strpos fails whenever
 * the span crosses a marker. This walks the source token-by-token so any
 * sequence of `[\W_]*?` between tokens is consumed -- bold/italic/code
 * markers, list bullets, blockquote prefixes, and whitespace collapse.
 *
 * Returns ['start' => int, 'length' => int, 'raw' => string] on hit, or
 * null when no plausible match exists.
 *
 * `$occurrenceIndex` (0-based) selects which match when the same text
 * appears multiple times -- the client tracks this by counting DOM
 * occurrences before the selection.
 */
/**
 * Decide whether the given match span can safely host a thread wrap.
 *
 * Three categories of selection are refused because the inline HTML-comment
 * wrap can't survive them:
 *   - inside a fenced code block (``` ... ```): comments would render as text
 *   - crossing a table row (a `\n` adjacent to a `|`-formatted line)
 *   - crossing block boundaries without a clean paragraph break (single `\n`
 *     between e.g. list items, header -> body, etc.) -- the rendered span
 *     would straddle <li>/<p> tags and produce invalid HTML.
 *
 * Returns ['ok' => true] or ['ok' => false, 'reason' => '...'].
 */
function check_thread_wrap_target(string $source, int $start, string $raw): array {
    // 1) fenced code block: count ``` before the start position; odd = inside.
    $before = substr($source, 0, $start);
    $fenceCount = preg_match_all('/^```/m', $before);
    if ($fenceCount % 2 === 1) {
        return ['ok' => false, 'reason' => 'Selection is inside a code block.'];
    }

    // 2) multi-line raw with a pipe -> probably a markdown table.
    if (strpos($raw, "\n") !== false && strpos($raw, '|') !== false) {
        return ['ok' => false, 'reason' => 'Selection crosses a table boundary.'];
    }

    // 3) raw has a single `\n` but no paragraph break -> would straddle a block
    //    element (list item, header, etc.). Either keep it inside one line or
    //    pick a clean multi-paragraph span.
    if (strpos($raw, "\n") !== false && preg_match('/\n\s*\n/', $raw) !== 1) {
        return ['ok' => false, 'reason' => 'Selection crosses a block boundary without a clear paragraph break. Try a shorter span inside one block, or extend it to the end of the paragraph.'];
    }

    return ['ok' => true];
}

function find_thread_quote_in_source(string $source, string $selectedText, int $occurrenceIndex = 0): ?array {
    $tokens = preg_split('/\s+/u', trim($selectedText), -1, PREG_SPLIT_NO_EMPTY);
    if (!$tokens) {
        return null;
    }

    $parts = [];
    foreach ($tokens as $tok) {
        $parts[] = preg_quote($tok, '/');
    }
    $pattern = '/' . implode('[\W_]*?', $parts) . '/su';

    if (!preg_match_all($pattern, $source, $matches, PREG_OFFSET_CAPTURE)) {
        return null;
    }

    $hits = $matches[0];
    $idx = $occurrenceIndex >= 0 && $occurrenceIndex < count($hits) ? $occurrenceIndex : 0;
    [$raw, $start] = $hits[$idx];

    return [
        'start'  => $start,
        'length' => strlen($raw),
        'raw'    => $raw,
    ];
}

function repair_dead_guid_link(string $url, string $linkText): string {
    if (!preg_match('~^/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})([/?\#].*)?$~', $url, $m)) {
        return $url; // not a GUID URL, nothing to repair
    }
    $guid = $m[1];
    $tail = $m[2] ?? '';

    try {
        $pdo = get_db();
        $stmt = $pdo->prepare("SELECT 1 FROM documents WHERE guid = ? AND deleted_at IS NULL");
        $stmt->execute([$guid]);
        if ($stmt->fetchColumn()) {
            return $url; // GUID is alive, leave the link alone
        }

        // Dead GUID. Try recovery from link text.
        $hint = trim($linkText, " \t`/");
        if ($hint === '') {
            error_log("DOCI: dead GUID link with no recovery hint: $url");
            return $url;
        }
        $hint = preg_replace('/\.(md|html)$/', '', $hint);

        $stmt = $pdo->prepare(
            "SELECT path FROM documents
             WHERE (path = ? OR path = ? OR path = ? OR path LIKE ?)
               AND deleted_at IS NULL
             ORDER BY length(path) ASC
             LIMIT 1"
        );
        $stmt->execute([
            $hint . '.md',
            $hint . '/index.md',
            $hint,
            '%/' . $hint . '.md',
        ]);
        $row = $stmt->fetch(PDO::FETCH_ASSOC);

        if ($row) {
            // Rewrite to path form WITHOUT the .md extension; the path_to_guid_url
            // pass downstream will turn it into the current live GUID.
            $pathClean = preg_replace('/\/index\.md$|\.md$/', '', $row['path']);
            $recovered = '/' . $pathClean . $tail;
            error_log("DOCI: repaired dead GUID link via text='$linkText': $url -> $recovered");
            return $recovered;
        }

        error_log("DOCI: dead GUID link, recovery failed: $url (text='$linkText')");
        return $url;
    } catch (Exception $e) {
        error_log("DOCI: repair_dead_guid_link error: " . $e->getMessage());
        return $url;
    }
}

/**
 * Resolve a single URL string from `/some/path` to `/<guid>` if it
 * matches a known document; otherwise return it unchanged.
 *
 * @param string $url
 * @return string
 */
function path_to_guid_url(string $url): string {
    if ($url === '' || $url[0] !== '/') return $url;
    if (preg_match('#^/(assets|api|files/\.data)/#', $url)) return $url;
    // Already a GUID URL? leave alone.
    if (preg_match('#^/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(/|$|\?|\#)#', $url)) return $url;

    $path = $url; $tail = '';
    if (($pos = strpos($path, '#')) !== false) { $tail = substr($path, $pos) . $tail; $path = substr($path, 0, $pos); }
    if (($pos = strpos($path, '?')) !== false) { $tail = substr($path, $pos) . $tail; $path = substr($path, 0, $pos); }
    $clean = preg_replace('/\.html$/', '', rtrim($path, '/'));
    $key = ltrim($clean, '/');
    if ($key === '' || $key === 'index') return $url;

    try {
        $pdo = get_db();
        $stmt = $pdo->prepare("SELECT path, guid FROM documents WHERE path IN (?, ?) AND deleted_at IS NULL LIMIT 2");
        $stmt->execute([$key . '.md', $key . '/index.md']);
        $found = $stmt->fetchAll(PDO::FETCH_ASSOC);
        // Prefer exact .md match over folder/index.md
        foreach ([$key . '.md', $key . '/index.md'] as $candidate) {
            foreach ($found as $row) {
                if ($row['path'] === $candidate) {
                    return '/' . $row['guid'] . $tail;
                }
            }
        }
    } catch (Throwable $e) {
        // DB unavailable -- keep original
    }
    return $url;
}

/**
 * Resolve internal path links in a rendered HTML fragment to GUID links.
 *
 * Markdown like `[Inbox](/tour/03-inbox)` renders to
 * `<a href="/tour/03-inbox">Inbox</a>`. After this rewrite, the href
 * becomes `/<guid>` -- so when the underlying file is moved or
 * renamed, the link keeps resolving. The link TEXT is untouched, only
 * the destination URL changes.
 *
 * One DB query per render (batched IN-clause), then string-replace.
 *
 * Skipped:
 *  - external URLs (http://, https://, mailto:)
 *  - in-page anchors (#section)
 *  - static asset paths (/assets/, /api/)
 *  - paths that do not resolve to a document in the table
 *
 * @param string $html
 * @return string
 */
function rewrite_links_to_guids(string $html): string {
    if (!function_exists('get_db')) {
        return $html;
    }

    // Helper: peel off query + fragment and trailing .html, return the
    // candidate documents.path keys ("foo/bar.md" and "foo/bar/index.md").
    $candidates = [];
    if (!preg_match_all('#<a\b[^>]*?href="([^"]+)"#', $html, $matches)) {
        return $html;
    }
    foreach ($matches[1] as $href) {
        if ($href[0] !== '/') continue;
        if (preg_match('#^/(assets|api|files/\.data)/#', $href)) continue;
        $pathPart = $href;
        if (($pos = strpos($pathPart, '#')) !== false) $pathPart = substr($pathPart, 0, $pos);
        if (($pos = strpos($pathPart, '?')) !== false) $pathPart = substr($pathPart, 0, $pos);
        $pathPart = preg_replace('/\.html$/', '', rtrim($pathPart, '/'));
        $pathPart = ltrim($pathPart, '/');
        if ($pathPart === '' || $pathPart === 'index') continue;
        $candidates[] = $pathPart . '.md';
        $candidates[] = $pathPart . '/index.md';   // folder-link fallback
    }
    if (empty($candidates)) return $html;
    $candidates = array_values(array_unique($candidates));

    try {
        $pdo = get_db();
        $ph = implode(',', array_fill(0, count($candidates), '?'));
        $stmt = $pdo->prepare("SELECT path, guid FROM documents WHERE path IN ($ph) AND deleted_at IS NULL");
        $stmt->execute($candidates);
        $map = [];
        while ($row = $stmt->fetch(PDO::FETCH_ASSOC)) {
            $map[$row['path']] = $row['guid'];
        }
    } catch (Throwable $e) {
        return $html;
    }
    if (empty($map)) return $html;

    return preg_replace_callback(
        '#(<a\b[^>]*?href=")([^"]+)(")#',
        function ($m) use ($map) {
            $href = $m[2];
            if ($href[0] !== '/') return $m[0];
            if (preg_match('#^/(assets|api|files/\.data)/#', $href)) return $m[0];
            // Split path | query | fragment
            $path = $href; $tail = '';
            if (($pos = strpos($path, '#')) !== false) { $tail = substr($path, $pos) . $tail; $path = substr($path, 0, $pos); }
            if (($pos = strpos($path, '?')) !== false) { $tail = substr($path, $pos) . $tail; $path = substr($path, 0, $pos); }
            $clean = preg_replace('/\.html$/', '', rtrim($path, '/'));
            $key = ltrim($clean, '/');
            if ($key === '' || $key === 'index') return $m[0];
            $candidate = $key . '.md';
            $folderCandidate = $key . '/index.md';
            if (isset($map[$candidate])) {
                return $m[1] . '/' . $map[$candidate] . $tail . $m[3];
            }
            if (isset($map[$folderCandidate])) {
                return $m[1] . '/' . $map[$folderCandidate] . $tail . $m[3];
            }
            return $m[0];
        },
        $html
    );
}

/**
 * Extract page title from markdown content (first H1) or use filename
 *
 * @param string $markdown Markdown content
 * @param string $fallback Fallback title
 * @return string Page title
 */
function extract_title(string $markdown, string $fallback): string {
    // Try to find first H1 heading
    if (preg_match('/^#\s+(.+)$/m', $markdown, $matches)) {
        return trim($matches[1]);
    }

    // HTML documents: fall back to the <title> tag
    if (preg_match('~<title[^>]*>(.*?)</title>~is', $markdown, $matches)) {
        $htmlTitle = trim(html_entity_decode($matches[1], ENT_QUOTES, 'UTF-8'));
        if ($htmlTitle !== '') {
            return $htmlTitle;
        }
    }

    // Use fallback (filename)
    return ucwords(str_replace(['-', '_'], ' ', $fallback));
}
