<?php
/**
 * DOCI - Render Functions
 *
 * HTML rendering functions for pages, navigation, directory listings.
 * Requires config.php to be loaded (for DOCI_ROOT, AUTH_URL, APP_URL, etc.)
 */

/**
 * Generate breadcrumb navigation from path
 *
 * @param string $path Current path
 * @return string HTML breadcrumbs
 */
function render_breadcrumbs(string $path): string {
    if (empty($path) || $path === 'index') {
        return '<a href="/">Home</a>';
    }

    $segments = explode('/', $path);
    $breadcrumbs = ['<a href="/">Home</a>'];
    $currentPath = '';

    foreach ($segments as $i => $segment) {
        $currentPath .= ($currentPath ? '/' : '') . $segment;

        // Last segment is current page (not a link)
        if ($i === count($segments) - 1) {
            $breadcrumbs[] = '<span class="current">' . htmlspecialchars(ucwords(str_replace(['-', '_'], ' ', $segment))) . '</span>';
        } else {
            $label = htmlspecialchars(ucwords(str_replace(['-', '_'], ' ', $segment)));
            $breadcrumbs[] = '<a href="/' . htmlspecialchars($currentPath) . '.html">' . $label . '</a>';
        }
    }

    return implode(' <span class="separator">/</span> ', $breadcrumbs);
}

/**
 * Render full HTML page with header and footer
 *
 * @param string $title Page title
 * @param string $content HTML content
 * @param string $path Current path for breadcrumbs
 */
/**
 * Build the depth-chain bar -- one chip per level from root document
 * to the current page. Each chip = one nestable layer of discussion.
 *
 * Layers are: root document, every thread ancestor in the current
 * chain. Version snapshots are infrastructure -- they're filtered out
 * because the user doesn't think in terms of "version of thread of
 * version of document". They think in terms of "thread -> sub-thread".
 *
 * Labels:
 *   Root  -- the original document at the top of the chain
 *   T-N   -- N-th thread when walking root -> current
 *
 * Click a chip = jump to that layer. Active chip = current page.
 * Hover shows the layer's title + author + timestamp.
 *
 * Sibling threads (multiple threads anchored on the same document)
 * are NOT shown here -- they live as purple pills in the body text
 * and that's the canonical entry point for branching sideways.
 */
function build_version_bar_html(?array $documentRecord): string {
    if (!$documentRecord) return '';

    $chain = get_document_hierarchy($documentRecord['guid']);
    if (empty($chain)) return '';

    // chain is depth DESC -> root first, current last. Keep only the
    // discussion layers (document + thread). Drop version rows (snapshot
    // bookkeeping) and folder rows (filesystem path -- belongs to the
    // breadcrumb, not to the depth bar).
    $levels = array_values(array_filter(
        $chain,
        function ($h) {
            $t = $h['doc_type'] ?? '';
            return $t === 'document' || $t === 'thread';
        }
    ));
    if (empty($levels)) return '';

    // Single-layer pages (a live root with no threads in this chain
    // yet) get just a "Root" chip -- still useful so the bar layout
    // is consistent across page types.
    $currentGuid = $documentRecord['guid'];
    $threadIndex = 0;

    ob_start();
    ?>
            <div class="version-bar">
                <span class="version-bar-label">Depth:</span>
                <?php foreach ($levels as $entry):
                    $isDoc = (($entry['doc_type'] ?? '') === 'document');
                    if ($isDoc) {
                        $label = 'Root';
                    } else {
                        $threadIndex++;
                        $label = 'T-' . $threadIndex;
                    }
                    $isActive = ($entry['guid'] === $currentGuid);
                    $tooltip  = trim(($entry['title'] ?? '') . "\n" . ($entry['quote'] ?? ''));
                ?>
                <a href="/<?= htmlspecialchars($entry['guid']) ?>"
                   class="version-btn <?= $isActive ? 'active' : '' ?>"
                   title="<?= htmlspecialchars($tooltip) ?>">
                    <span class="version-author"><?= $label ?></span>
                </a>
                <?php endforeach; ?>
            </div>
    <?php
    return ob_get_clean();
}

function render_page(string $title, string $content, string $path, bool $showRecentSidebar = true, ?string $rawContent = null, array $hierarchy = [], ?array $documentRecord = null): void {
    $documentGuid = $documentRecord['guid'] ?? null;
    $username = get_current_username();
    $logoutUrl = get_logout_url();
    $baseUrl = get_base_url();

    // Get versions for document or determine original if this is a version
    $versions = [];
    $originalDoc = null;
    $isVersion = false;
    $isThread = false;
    $currentVersionGuid = null;

    if ($documentRecord) {
        $docType = $documentRecord['doc_type'] ?? 'document';
        $isThread = $docType === 'thread';

        if ($docType === 'document') {
            // This is an original document - get its versions
            $versions = get_document_versions($documentGuid);
            $originalDoc = $documentRecord;
        } elseif ($docType === 'version') {
            // This is a version - get the original and all versions
            $isVersion = true;
            $currentVersionGuid = $documentGuid;
            $originalDoc = get_original_document($documentGuid);
            if ($originalDoc) {
                $versions = get_document_versions($originalDoc['guid']);
            }
        } elseif ($docType === 'thread') {
            // Thread is anchored to a version of some root doc/thread.
            // Walk the full hierarchy (migration 003 follows both
            // parent_guid and original_guid) and surface the same
            // version-bar the root document would show, with the active
            // chip marking the version this thread sits on.
            $chain = get_document_hierarchy($documentGuid);
            foreach ($chain as $h) {
                if (($h['doc_type'] ?? '') === 'document') {
                    $originalDoc = get_document_by_guid($h['guid']);
                    break;
                }
            }
            if ($originalDoc) {
                $versions = get_document_versions($originalDoc['guid']);
                // The active version is the root-level version in the
                // chain (the snapshot of the root doc that this thread
                // line descends from). Find the version row whose
                // original_guid matches the root we just resolved.
                foreach ($chain as $h) {
                    if (($h['doc_type'] ?? '') === 'version') {
                        $ver = get_document_by_guid($h['guid']);
                        if ($ver && ($ver['original_guid'] ?? null) === $originalDoc['guid']) {
                            $currentVersionGuid = $ver['guid'];
                            break;
                        }
                    }
                }
            }
        }
    }

    // Use hierarchy-based breadcrumbs if available, otherwise path-based
    if (!empty($hierarchy)) {
        $breadcrumbs = render_hierarchy_breadcrumbs($hierarchy);
    } else {
        $breadcrumbs = render_breadcrumbs($path);
    }

    $fileTree = render_file_tree(DOCI_ROOT . '/files', '', $path);
    $recentSidebar = $showRecentSidebar ? render_recent_files(50) : '';

    // For edit mode - check if this is an editable file
    $isEditable = false;
    $editPath = '';
    if ($rawContent !== null && !empty($path) && $path !== 'index') {
        $mdFilePath = DOCI_ROOT . '/files/' . $path . '.md';
        error_log("Edit check: path=$path, mdFilePath=$mdFilePath, exists=" . (file_exists($mdFilePath) ? 'yes' : 'no'));
        if (file_exists($mdFilePath)) {
            $isEditable = true;
            $editPath = $path;
        }
    }

    ?><!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="csrf-token" content="<?= htmlspecialchars(get_csrf_token()) ?>">
    <title><?= htmlspecialchars($title) ?> - DOCI</title>
    <link rel="stylesheet" href="/assets/doci-theme.css?v=<?= @filemtime(__DIR__ . '/../assets/doci-theme.css') ?: time() ?>">
    <link rel="stylesheet" href="/assets/doci.css?v=<?= @filemtime(__DIR__ . '/../assets/doci.css') ?: time() ?>">
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
    <script>!function(){var s=localStorage.getItem('doci_theme');var t=s==='light'?'light':'dark';document.documentElement.dataset.theme=t}()</script>
</head>
<body>
    <!-- Context menu for text selection -->
    <div id="context-menu" class="context-menu">
        <div class="context-menu-item" data-action="start-thread">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
            </svg>
            Start Thread
        </div>
        <div class="context-menu-item" data-action="copy">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>
                <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
            </svg>
            Copy
        </div>
    </div>

    <!-- Context menu for no selection (document-level) -->
    <div id="context-menu-doc" class="context-menu">
        <div class="context-menu-item" data-action="discuss-document">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
            </svg>
            Discuss Document
        </div>
    </div>

    <!-- Thread creation modal -->
    <div id="thread-modal" class="modal-overlay">
        <div class="modal">
            <div class="modal-header">
                <span>Start Thread</span>
                <button class="modal-close" data-action="close">&times;</button>
            </div>
            <div class="modal-body">
                <div class="quote-block" id="thread-quote"></div>
                <details class="debug-context">
                    <summary>Debug: Show context</summary>
                    <div style="margin-top: 8px; font-family: monospace; white-space: pre-wrap; word-break: break-all;">
                        <div style="color: #888;">Before (50 chars):</div>
                        <div id="debug-context-before"></div>
                        <div style="color: #888;">Selected (HTML):</div>
                        <div id="debug-context-selected"></div>
                        <div style="color: #888;">After (50 chars):</div>
                        <div id="debug-context-after"></div>
                        <div style="color: #888; margin-top: 8px;">Markdown fragment (from API):</div>
                        <div id="debug-markdown-fragment"></div>
                        <div style="color: #888; margin-top: 8px;">Full search pattern:</div>
                        <div id="debug-context-full"></div>
                    </div>
                </details>
                <label class="form-label">Your comment or question</label>
                <textarea class="form-textarea" id="thread-comment" placeholder="Write something..."></textarea>
                <div class="ai-toggle-section">
                    <label class="ai-toggle-label">
                        <input type="checkbox" id="thread-ai-toggle">
                        <div class="ai-toggle-content">
                            <span class="ai-toggle-text">Ask AI to respond</span>
                            <span class="ai-toggle-hint">AI will receive document context and your question</span>
                        </div>
                    </label>
                    <div class="ai-model-selector" id="ai-model-selector">
                        <div class="ai-model-label">Model:</div>
                        <div class="ai-model-chips">
                            <div class="ai-model-chip" data-model="haiku">Haiku<span class="ai-model-chip-hint">(fast)</span></div>
                            <div class="ai-model-chip selected" data-model="sonnet">Sonnet<span class="ai-model-chip-hint">(balanced)</span></div>
                            <div class="ai-model-chip" data-model="opus">Opus<span class="ai-model-chip-hint">(best)</span></div>
                        </div>
                    </div>
                </div>
            </div>
            <div class="modal-footer">
                <button class="btn btn-secondary" data-action="close">Cancel</button>
                <button class="btn btn-primary" data-action="create">Create Thread</button>
            </div>
        </div>
    </div>

    <button class="sidebar-toggle" id="sidebar-toggle" aria-label="Menu">&#9776;</button>
    <div class="sidebar-overlay" id="sidebar-overlay"></div>

    <div class="layout">
        <aside class="sidebar" id="sidebar">
            <div class="logo-row"><div class="sidebar-logo"><a href="/">DOCI</a></div><div class="logo-row-toggles"><label class="key-switch canonical-filter-switch" title="Show only canonical documents in the tree"><input type="checkbox" id="canonical-filter-toggle" onchange="toggleCanonicalOnly(this.checked)"><span class="key-switch-slider"></span><span class="key-switch-label">Canonical</span></label><button class="theme-toggle" onclick="toggleTheme()" title="Switch theme"><span class="theme-toggle-label">Theme</span></button></div></div>
            <div class="sidebar-section">Navigation</div>
            <nav class="sidebar-nav">
                <?= $fileTree ?>
            </nav>
            <?php if (defined('AUTH_URL') && AUTH_URL !== ''): ?>
            <div class="sidebar-section" style="margin-top:auto;padding-top:15px;border-top:1px solid var(--border);">
                <a href="<?= AUTH_URL ?>/?page=settings" class="nav-item"><span class="nav-icon">USR</span><?= htmlspecialchars($username) ?></a>
                <a href="<?= htmlspecialchars($logoutUrl) ?>" class="nav-item"><span class="nav-icon">OUT</span>Logout</a>
            </div>
            <?php endif; ?>
        </aside>

        <div class="content">
            <div class="breadcrumbs">
                <div class="breadcrumbs-links"><?= $breadcrumbs ?></div>
                <form method="GET" action="/search.html" class="search-bar-form" onsubmit="return true;">
                    <input type="text" name="q" placeholder="Search..." class="search-bar-input" value="<?= htmlspecialchars($_GET['q'] ?? '') ?>">
                </form>
                <div class="edit-controls" data-edit-path="<?= htmlspecialchars($isEditable ? $editPath : '') ?>" data-doc-guid="<?= htmlspecialchars($documentGuid ?? '') ?>"<?= $isEditable ? '' : ' style="display:none"' ?>>
                    <button id="edit-btn" class="edit-btn" title="Edit this page">Edit</button>
                    <button id="save-btn" class="edit-btn save-btn" style="display:none">Save</button>
                    <button id="cancel-btn" class="edit-btn cancel-btn" style="display:none">Cancel</button>
                    <button id="delete-btn" class="edit-btn delete-btn" title="Delete this document">Delete</button>
                    <span id="edit-status" class="edit-status"></span>
                </div>
            </div>

            <div class="document-meta">
                <?php if ($isThread): ?>
                <span class="meta-item"><span class="meta-label">Thread by</span> <?= htmlspecialchars($documentRecord['created_by'] ?? 'unknown') ?></span>
                <span class="meta-sep">|</span>
                <?php endif; ?>
                <span class="meta-item"><span class="meta-label">Path:</span> <?= htmlspecialchars($path . '.md') ?></span>
                <?php if ($documentGuid): ?>
                <span class="meta-sep">|</span>
                <span class="meta-item"><span class="meta-label">GUID:</span> <span class="document-guid" id="copy-guid" data-url="<?= APP_URL ?>/<?= htmlspecialchars($documentGuid) ?>"><?= htmlspecialchars($documentGuid) ?></span></span>
                <?php
                    require_once __DIR__ . '/key-documents.php';
                    $isKey = is_key_document($documentGuid);
                ?>
                <span class="meta-sep">|</span>
                <label class="key-switch" title="<?= $isKey ? 'Marked as canonical' : 'Mark as canonical' ?>">
                    <input type="checkbox" id="key-doc-toggle" data-guid="<?= htmlspecialchars($documentGuid) ?>"<?= $isKey ? ' checked' : '' ?>>
                    <span class="key-switch-slider"></span>
                    <span class="key-switch-label">Canonical</span>
                </label>
                <?php endif; ?>
                <?php
                $commits = git_get_file_history($path . '.md', 3);
                $commitUrlBase = git_get_commit_url_base();
                if (!empty($commits)):
                ?>
                <span class="meta-sep">|</span>
                <span class="meta-item"><span class="meta-label">History:</span>
                <?php
                $commitLinks = array_map(function($c) use ($commitUrlBase) {
                    $title = $c['message'] . ' -- ' . $c['author'] . ' (' . $c['date'] . ')';
                    $title = htmlspecialchars($title);
                    $hash = htmlspecialchars($c['hash']);
                    if ($commitUrlBase) {
                        return '<a href="' . htmlspecialchars($commitUrlBase . '/' . $c['hash']) . '" target="_blank" title="' . $title . '">' . $hash . '</a>';
                    }
                    // No remote configured -- show the hash as a tooltipped span
                    // instead of dropping the whole History block.
                    return '<span class="commit-hash" title="' . $title . '">' . $hash . '</span>';
                }, $commits);
                echo implode(', ', $commitLinks);
                ?>
                </span>
                <?php endif; ?>
            </div>

            <?= build_version_bar_html($documentRecord) ?>

            <article class="markdown-content markdown-body" id="view-mode">
                <?= $content ?>
            </article>

            <div id="edit-mode" class="edit-mode" style="display:none">
                <textarea id="editor" class="markdown-editor"><?= htmlspecialchars($isEditable ? ($rawContent ?? '') : '') ?></textarea>
            </div>

            <?php if ($documentGuid): ?>
            <?php
                $parentDoc = null;
                if ($isThread && !empty($documentRecord['parent_guid'])) {
                    $parentDoc = get_document_by_guid($documentRecord['parent_guid']);
                }
                // Check if thread has AI response (to show reply section)
                $hasAiResponse = $isThread && strpos($rawContent ?? '', '**AI Response**') !== false;
            ?>

            <?php if ($hasAiResponse): ?>
            <div class="thread-reply-section" id="thread-reply-section">
                <h4>Continue conversation</h4>
                <div class="thread-reply-form">
                    <textarea class="thread-reply-input" id="thread-reply-input" placeholder="Ask a follow-up question..." rows="1"></textarea>
                    <button class="thread-reply-btn" id="thread-reply-btn">Send</button>
                </div>
            </div>
            <?php endif; ?>
            <?php endif; ?>
        </div><!-- .content -->
        <?= $recentSidebar ?>
    </div><!-- .layout -->

    <!-- Search preview panel (slide-out) -->
    <div id="search-preview-overlay" class="search-preview-overlay"></div>
    <div id="search-preview-panel" class="search-preview-panel">
        <div class="search-preview-header">
            <div class="search-preview-header-left">
                <span id="search-preview-title" class="search-preview-title"></span>
                <span id="search-preview-path" class="search-preview-path"></span>
            </div>
            <div class="search-preview-header-right">
                <a id="search-preview-open" href="#" class="search-preview-open-btn">Open</a>
                <button onclick="closeSearchPreview()" class="search-preview-close-btn">[X]</button>
            </div>
        </div>
        <div id="search-preview-body" class="search-preview-body markdown-body"></div>
    </div>

    <button id="back-to-top" title="Back to top" style="
        display: none;
        position: fixed;
        bottom: 30px;
        right: 30px;
        width: 44px;
        height: 44px;
        border-radius: 50%;
        background: #333;
        color: #fff;
        border: none;
        cursor: pointer;
        font-size: 20px;
        box-shadow: 0 2px 10px rgba(0,0,0,0.3);
        z-index: 1000;
        transition: opacity 0.3s;
    ">^</button>

    <script>
    <?php
        // Surface AI provider availability to the client. JS uses this
        // to hide AI controls (the thread modal's "Use AI" checkbox and
        // its model chips) when the operator has not configured a
        // provider, instead of letting users tick a checkbox that fails.
        require_once __DIR__ . '/../src/ai.php';
        $aiEnabled = ai_is_configured();
        $aiModels = [];
        foreach (['haiku', 'sonnet', 'opus'] as $alias) {
            if (ai_get_model_id($alias) !== null) $aiModels[] = $alias;
        }
    ?>
    window.__DOCI = {
        rawMarkdown: <?= json_encode($rawContent ?? '', JSON_UNESCAPED_UNICODE) ?>,
        currentPath: <?= json_encode($path, JSON_UNESCAPED_UNICODE) ?>,
        editPath: <?= json_encode($editPath, JSON_UNESCAPED_UNICODE) ?>,
        documentGuid: <?= json_encode($documentGuid ?? '', JSON_UNESCAPED_UNICODE) ?>,
        ai: { enabled: <?= $aiEnabled ? 'true' : 'false' ?>, models: <?= json_encode($aiModels) ?> }
    };
    </script>
    <script src="/assets/app.js?v=<?= filemtime('/var/www/html/assets/app.js') ?>"></script>
    <?php if (EXTERNAL_SCRIPTS_URL): ?>
    <script src="<?= htmlspecialchars(EXTERNAL_SCRIPTS_URL) ?>/external-links.js"></script>
    <?php endif; ?>
<script>
var THEMES=['dark','light'],THEME_NAMES={'dark':'Dark','light':'Light'};
function getTheme(){var s=localStorage.getItem('doci_theme');return s==='light'?'light':'dark'}
function applyTheme(t){document.documentElement.dataset.theme=t;var l=document.querySelector('.theme-toggle-label');if(l)l.textContent=THEME_NAMES[t]||'Dark'}
function toggleTheme(){var c=getTheme(),n=THEMES[(THEMES.indexOf(c)+1)%THEMES.length];localStorage.setItem('doci_theme',n);applyTheme(n)}
applyTheme(getTheme());
function getCanonicalOnly(){return localStorage.getItem('doci_canonical_only')==='1'}
function applyCanonicalOnly(on){document.body.classList.toggle('canonical-only',on);var c=document.getElementById('canonical-filter-toggle');if(c)c.checked=on}
function toggleCanonicalOnly(on){if(typeof on!=='boolean')on=!getCanonicalOnly();localStorage.setItem('doci_canonical_only',on?'1':'0');applyCanonicalOnly(on)}
document.addEventListener('DOMContentLoaded',function(){applyCanonicalOnly(getCanonicalOnly())});
</script>
<script>
(function(){
    var btn=document.getElementById('sidebar-toggle');
    var sb=document.getElementById('sidebar');
    var ov=document.getElementById('sidebar-overlay');
    if(!btn||!sb||!ov)return;
    function open(){sb.classList.add('open');ov.classList.add('visible');btn.innerHTML='&times;'}
    function close(){sb.classList.remove('open');ov.classList.remove('visible');btn.innerHTML='&#9776;'}
    btn.addEventListener('click',function(){sb.classList.contains('open')?close():open()});
    ov.addEventListener('click',close);
    sb.addEventListener('click',function(e){if(e.target.closest('a'))close()});
})();
</script>
</body>
</html><?php
}

/**
 * Simple file tree for sidebar
 */
function render_file_tree(string $basePath, string $urlPath = '', string $currentPath = '', int $depth = 0): string {
    $filesDir = realpath(DOCI_ROOT . '/files');
    $realBasePath = realpath($basePath);
    if (!$realBasePath || strpos($realBasePath, $filesDir) !== 0) return '';

    $items = @scandir($realBasePath);
    if (!$items) return '';

    $pad = $depth * 12;
    $html = '';

    // Collect folders and files
    $folders = [];
    $files = [];

    foreach ($items as $item) {
        if ($item[0] === '.' || $item === 'assets') continue;
        // Hide *every* index.md / index.html from the tree -- they are
        // the implicit landing of their level (root = /, sub-folder
        // accessed by clicking the folder name), so listing them as a
        // separate child duplicates the parent click target.
        if ($item === 'index.md' || $item === 'index.html') continue;
        $fullPath = $realBasePath . '/' . $item;
        $itemUrl = $urlPath ? $urlPath . '/' . $item : $item;

        if (is_dir($fullPath)) {
            $folders[$item] = ['path' => $fullPath, 'url' => $itemUrl];
        } elseif (preg_match('/\.(md|html)$/', $item)) {
            $name = preg_replace('/\.(md|html)$/', '', $item);
            $fileUrl = $urlPath ? $urlPath . '/' . $name : $name;
            $files[$name] = ['url' => $fileUrl, 'active' => ($fileUrl === $currentPath), 'mdPath' => $itemUrl];
        }
    }

    ksort($folders);
    ksort($files);

    // Render folders -- two-column row: icon cell (fixed) + title cell (depth-padded).
    foreach ($folders as $name => $data) {
        $children = render_file_tree($data['path'], $data['url'], $currentPath, $depth + 1);
        if (!$children) continue;

        $hasKeyDescendant = strpos($children, 'is-key') !== false;
        $isCurrentPath = $currentPath && ($currentPath === $data['url'] || strpos($currentPath, $data['url'] . '/') === 0);
        $isExpanded = $isCurrentPath || $hasKeyDescendant;
        $openClass = $isExpanded ? ' open' : '';
        $keyClass = $hasKeyDescendant ? ' has-key' : '';
        $toggleIcon = $isExpanded ? '[-]' : '[+]';
        $iconCell = '<span class="nav-icon-cell"></span>';

        $title = ucwords(str_replace(['-', '_'], ' ', $name));
        $html .= '<div class="nav-folder' . $openClass . $keyClass . '">';
        $html .= $iconCell;
        $html .= '<span class="nav-title-cell" style="padding-left:' . $pad . 'px">';
        $html .= '<span class="nav-folder-toggle" onclick="this.parentElement.parentElement.classList.toggle(\'open\'); this.textContent = this.textContent === \'[+]\' ? \'[-]\' : \'[+]\'">' . $toggleIcon . '</span> ';
        $html .= '<a href="/' . htmlspecialchars($data['url']) . '.html" class="nav-folder-link">' . htmlspecialchars($title) . '</a>';
        $html .= '</span>';
        $html .= '<div class="nav-folder-children">' . $children . '</div>';
        $html .= '</div>';
    }

    // Render files
    require_once __DIR__ . '/key-documents.php';
    static $pathInfo = null;
    if ($pathInfo === null) {
        $pathInfo = [];
        try {
            $pdo = get_db();
            $stmt = $pdo->query("SELECT path, guid, title FROM documents WHERE deleted_at IS NULL AND doc_type IN ('document','version')");
            if ($stmt) {
                while ($r = $stmt->fetch(PDO::FETCH_ASSOC)) {
                    $pathInfo[$r['path']] = ['guid' => $r['guid'], 'title' => $r['title']];
                }
            }
        } catch (Throwable $e) {}
    }
    foreach ($files as $name => $data) {
        $mdPath = $data['mdPath'];
        // Prefer the document's own title; fall back to the raw filename
        // (NOT humanized — humanization mangles dates like 2026-05-12).
        $dbTitle = $pathInfo[$mdPath]['title'] ?? null;
        $title = ($dbTitle !== null && $dbTitle !== '') ? $dbTitle : $name;
        $activeClass = $data['active'] ? ' nav-active' : '';
        $guid = $pathInfo[$mdPath]['guid'] ?? null;
        $isKey = $guid && is_key_document($guid);
        $keyClass = $isKey ? ' is-key' : '';
        $iconCell = '<span class="nav-icon-cell"></span>';
        $html .= '<div class="nav-file' . $activeClass . $keyClass . '">';
        $html .= $iconCell;
        $html .= '<span class="nav-title-cell" style="padding-left:' . $pad . 'px">';
        $html .= '<a href="/' . htmlspecialchars($data['url']) . '.html">' . htmlspecialchars($title) . '</a>';
        $html .= '</span>';
        $html .= '</div>';
    }

    return $html;
}

/**
 * Generate folder cards HTML for root directories
 *
 * @param string $basePath Base path to scan
 * @param bool $wrapInContainer Whether to wrap cards in folder-cards div
 * @return string HTML content with folder cards
 */
function render_folder_cards(string $basePath, bool $wrapInContainer = true, string $urlPrefix = ''): string {
    $filesDir = realpath(DOCI_ROOT . '/files');
    $realBasePath = realpath($basePath);

    if ($realBasePath === false || strpos($realBasePath, $filesDir) !== 0) {
        return '';
    }

    $items = scandir($realBasePath);
    if ($items === false) {
        return '';
    }

    $folders = [];
    foreach ($items as $item) {
        if ($item[0] === '.') {
            continue;
        }

        $fullPath = $realBasePath . '/' . $item;
        if (is_dir($fullPath)) {
            // Count markdown files in folder
            $mdFiles = glob($fullPath . '/*.md');
            $fileCount = $mdFiles ? count($mdFiles) : 0;

            // Get folder description from README.md if exists
            $description = '';
            $readmePath = $fullPath . '/README.md';
            if (file_exists($readmePath)) {
                $content = file_get_contents($readmePath);
                // Extract first paragraph after title
                if (preg_match('/^#[^\n]+\n+([^\n#]+)/m', $content, $matches)) {
                    $description = trim($matches[1]);
                    if (strlen($description) > 120) {
                        $description = substr($description, 0, 117) . '...';
                    }
                }
            }

            // Get last modified time
            $lastModified = filemtime($fullPath);

            $folders[] = [
                'name' => $item,
                'title' => trim(ucwords(str_replace(['-', '_'], ' ', $item))),
                'path' => $urlPrefix ? $urlPrefix . '/' . $item : $item,
                'file_count' => $fileCount,
                'description' => $description,
                'modified' => $lastModified,
            ];
        }
    }

    if (empty($folders)) {
        return '';
    }

    // Sort folders by modified date (newest first)
    usort($folders, fn($a, $b) => $b['modified'] - $a['modified']);

    if ($wrapInContainer) {
        $html = '<div class="folder-cards">' . "\n";
    } else {
        $html = '';
    }

    foreach ($folders as $folder) {
        $html .= '<a href="/' . htmlspecialchars($folder['path']) . '.html" class="folder-card">' . "\n";
        $html .= '  <div class="folder-icon">&#128193;</div>' . "\n";
        $html .= '  <div class="folder-info">' . "\n";
        $html .= '    <h3>' . htmlspecialchars($folder['title']) . '</h3>' . "\n";
        if ($folder['description']) {
            $html .= '    <p class="folder-desc">' . htmlspecialchars($folder['description']) . '</p>' . "\n";
        }
        $html .= '    <div class="folder-footer">' . "\n";
        $html .= '      <div class="folder-filename">' . htmlspecialchars($folder['name']) . '/ <span class="file-count">(' . $folder['file_count'] . ')</span></div>' . "\n";
        $html .= '      <div class="folder-updated">' . date('M j, Y', $folder['modified']) . '</div>' . "\n";
        $html .= '    </div>' . "\n";
        $html .= '  </div>' . "\n";
        $html .= '</a>' . "\n";
    }

    if ($wrapInContainer) {
        $html .= '</div>' . "\n";
    }

    return $html;
}

/**
 * Generate file cards HTML for markdown files in root directory
 *
 * @param string $basePath Base path to scan
 * @param bool $wrapInContainer Whether to wrap cards in folder-cards div
 * @return string HTML content with file cards
 */
function render_file_cards(string $basePath, bool $wrapInContainer = true, string $urlPrefix = ''): string {
    $filesDir = realpath(DOCI_ROOT . '/files');
    $realBasePath = realpath($basePath);

    if ($realBasePath === false || strpos($realBasePath, $filesDir) !== 0) {
        return '';
    }

    $items = scandir($realBasePath);
    if ($items === false) {
        return '';
    }

    $files = [];
    foreach ($items as $item) {
        if ($item[0] === '.' || $item === 'README.md') {
            continue;
        }

        $fullPath = $realBasePath . '/' . $item;

        // Include .md and .html files, skip directories
        $isMd = substr($item, -3) === '.md';
        $isHtml = substr($item, -5) === '.html';

        if (is_file($fullPath) && ($isMd || $isHtml)) {
            $content = file_get_contents($fullPath);

            // Extract title
            if ($isMd) {
                $title = ucwords(str_replace(['-', '_', '.md'], ' ', $item));
                if (preg_match('/^#\s+(.+)$/m', $content, $matches)) {
                    $title = trim($matches[1]);
                }
            } else {
                $title = ucwords(str_replace(['-', '_', '.html'], ' ', $item));
                if (preg_match('/<title>([^<]+)<\/title>/i', $content, $matches)) {
                    $title = trim($matches[1]);
                }
            }

            // Extract description (first paragraph after title for md, meta description for html)
            $description = '';
            if ($isMd) {
                if (preg_match('/^#[^\n]+\n+([^\n#]+)/m', $content, $matches)) {
                    $description = trim($matches[1]);
                    if (strlen($description) > 120) {
                        $description = substr($description, 0, 117) . '...';
                    }
                }
            } else {
                if (preg_match('/<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']/i', $content, $matches)) {
                    $description = trim($matches[1]);
                    if (strlen($description) > 120) {
                        $description = substr($description, 0, 117) . '...';
                    }
                }
            }

            // Get last modified time
            $lastModified = filemtime($fullPath);

            $fileBaseName = $isMd ? substr($item, 0, -3) : substr($item, 0, -5);
            $files[] = [
                'name' => $item,
                'title' => $title,
                'path' => $urlPrefix ? $urlPrefix . '/' . $fileBaseName : $fileBaseName,
                'description' => $description,
                'modified' => $lastModified,
                'type' => $isMd ? 'md' : 'html',
            ];
        }
    }

    if (empty($files)) {
        return '';
    }

    // Sort files by name descending (newest dated files first for YYYY-MM-DD naming)
    usort($files, fn($a, $b) => strcasecmp($b['name'], $a['name']));

    if ($wrapInContainer) {
        $html = '<div class="folder-cards">' . "\n";
    } else {
        $html = '';
    }

    foreach ($files as $file) {
        $fileType = $file['type'];
        $fileExt = $fileType === 'html' ? '.html' : '.md';

        $html .= '<a href="/' . htmlspecialchars($file['path']) . '.html" class="folder-card file-card file-card-no-icon">' . "\n";
        $html .= '  <div class="folder-info">' . "\n";
        $html .= '    <h3>' . htmlspecialchars($file['title']) . '</h3>' . "\n";
        if ($file['description']) {
            $html .= '    <p class="folder-desc">' . htmlspecialchars($file['description']) . '</p>' . "\n";
        }
        $html .= '    <div class="folder-footer">' . "\n";
        $html .= '      <div class="folder-filename">' . htmlspecialchars($file['name']) . '</div>' . "\n";
        $html .= '      <div class="folder-updated">' . date('M j, Y', $file['modified']) . '</div>' . "\n";
        $html .= '    </div>' . "\n";
        $html .= '  </div>' . "\n";
        $html .= '</a>' . "\n";
    }

    if ($wrapInContainer) {
        $html .= '</div>' . "\n";
    }

    return $html;
}

/**
 * Get recently modified files from the entire repository
 *
 * @param string $basePath Base path to scan
 * @param int $limit Maximum number of files to return
 * @return array Array of file info sorted by modification time (newest first)
 */
function get_recent_files(string $basePath, int $limit = 20): array {
    $filesDir = realpath($basePath);
    if ($filesDir === false) {
        return [];
    }

    $allFiles = [];
    $iterator = new RecursiveIteratorIterator(
        new RecursiveDirectoryIterator($filesDir, RecursiveDirectoryIterator::SKIP_DOTS),
        RecursiveIteratorIterator::LEAVES_ONLY
    );

    foreach ($iterator as $file) {
        if (!$file->isFile()) {
            continue;
        }

        $filename = $file->getFilename();
        $extension = strtolower(pathinfo($filename, PATHINFO_EXTENSION));

        // Skip non-content files
        if (!in_array($extension, ['md', 'html']) || $filename === 'README.md' || $filename === '.htaccess') {
            continue;
        }

        $fullPath = $file->getPathname();
        $relativePath = substr($fullPath, strlen($filesDir) + 1);

        // Skip files under dot-prefixed folders (e.g. .showcase/, .data/).
        // These are implementation details, not user-facing documents.
        $relParts = explode('/', $relativePath);
        $hidden = false;
        foreach ($relParts as $part) {
            if ($part !== '' && $part[0] === '.') { $hidden = true; break; }
        }
        if ($hidden) continue;

        // Skip auto-generated report directories that flood recent list
        $skipPrefixes = ['top-monitor/reports/', 'top-monitor/archive/'];
        $skip = false;
        foreach ($skipPrefixes as $prefix) {
            if (strncmp($relativePath, $prefix, strlen($prefix)) === 0) {
                $skip = true;
                break;
            }
        }
        if ($skip) {
            continue;
        }

        $urlPath = preg_replace('/\.(md|html)$/', '', $relativePath);

        // Extract title
        $content = file_get_contents($fullPath);
        if ($extension === 'md') {
            $title = ucwords(str_replace(['-', '_'], ' ', pathinfo($filename, PATHINFO_FILENAME)));
            // Strip fenced code blocks before scanning -- otherwise shell
            // comments like '# install foo' inside ```bash``` blocks get
            // mistaken for a markdown H1.
            $contentForTitle = preg_replace('/```.*?```/s', '', $content);
            if (preg_match('/^#\s+(.+)$/m', $contentForTitle, $matches)) {
                $title = trim($matches[1]);
            }
        } else {
            $title = ucwords(str_replace(['-', '_'], ' ', pathinfo($filename, PATHINFO_FILENAME)));
            if (preg_match('/<title>([^<]+)<\/title>/i', $content, $matches)) {
                $title = trim($matches[1]);
            }
        }

        $allFiles[] = [
            'path' => $urlPath,
            'title' => $title,
            'modified' => $file->getMTime(),
            'type' => $extension,
        ];
    }

    // Sort by modification time (newest first)
    usort($allFiles, fn($a, $b) => $b['modified'] - $a['modified']);

    // Return limited number
    return array_slice($allFiles, 0, $limit);
}

/**
 * Render recent files sidebar
 *
 * @param int $limit Maximum number of files to show
 * @return string HTML content
 */
function render_recent_files(int $limit = 20): string {
    $recentFiles = get_recent_files(DOCI_ROOT . '/files', $limit);

    if (empty($recentFiles)) {
        return '';
    }

    $html = '<aside class="recent-sidebar">' . "\n";
    $html .= '  <div class="recent-header">Recent Changes</div>' . "\n";
    $html .= '  <ul class="recent-list">' . "\n";

    $lastDate = '';
    foreach ($recentFiles as $file) {
        $date = date('M j', $file['modified']);
        $time = date('H:i', $file['modified']);
        $typeClass = $file['type'] === 'html' ? 'html' : 'md';
        $typeLabel = strtoupper($file['type']);
        $filePath = $file['path'] . '.' . $file['type'];

        // Show date separator if date changed
        if ($date !== $lastDate) {
            if ($lastDate !== '') {
                $html .= '    </ul></li>' . "\n";
            }
            $html .= '    <li class="recent-date-group"><span class="recent-date">' . $date . '</span><ul>' . "\n";
            $lastDate = $date;
        }

        $html .= '      <li class="recent-item">';
        $html .= '<a href="/' . htmlspecialchars($file['path']) . '.html">';
        $html .= '<div class="recent-item-main">';
        $html .= '<span class="file-type ' . $typeClass . '">' . $typeLabel . '</span>';
        $html .= '<span class="recent-title">' . htmlspecialchars($file['title']) . '</span>';
        $html .= '</div>';
        $html .= '<div class="recent-item-meta">';
        $html .= '<span class="recent-time">' . $time . '</span>';
        $html .= '<span class="recent-path">' . htmlspecialchars($filePath) . '</span>';
        $html .= '</div>';
        $html .= '</a></li>' . "\n";
    }

    if ($lastDate !== '') {
        $html .= '    </ul></li>' . "\n";
    }

    $html .= '  </ul>' . "\n";
    $html .= '</aside>' . "\n";

    return $html;
}

/**
 * Render search results page
 *
 * @param string $query  Search query
 * @param string $folder Optional folder filter (e.g. "operator")
 * @return string HTML content
 */
function render_search_page(string $query, string $folder = ''): string {
    $html = '<div class="search-page">' . "\n";

    // The breadcrumbs bar at the top of every page already carries the
    // search input (.search-bar-form) with the current query and the
    // same submit target, so we don't render a second one here.

    // Show folder scope indicator
    if ($folder !== '') {
        $folderTitle = ucwords(str_replace(['-', '_'], ' ', $folder));
        $html .= '<div class="search-scope">';
        $html .= 'Searching in: <a href="/' . htmlspecialchars($folder) . '.html">' . htmlspecialchars($folderTitle) . '</a>';
        $html .= ' <a href="/search.html' . ($query ? '?q=' . urlencode($query) : '') . '" class="search-scope-clear">[all documents]</a>';
        $html .= '</div>' . "\n";
    }

    if (empty(trim($query))) {
        $html .= '<p class="search-hint">Enter a query to search across ' . ($folder ? htmlspecialchars($folder) . '/' : 'all') . ' documents.</p>' . "\n";
        $html .= '</div>' . "\n";
        return $html;
    }

    $results = searchMesh($query, 30, $folder ?: null);

    if (empty($results)) {
        $html .= '<p class="search-no-results">No results found for "' . htmlspecialchars($query) . '"</p>' . "\n";
        $html .= '</div>' . "\n";
        return $html;
    }

    $html .= '<div class="search-results-count">' . count($results) . ' results</div>' . "\n";
    $html .= '<div class="search-results">' . "\n";

    foreach ($results as $result) {
        // Extract doci-path from tags
        $docPath = '';
        foreach ($result['tags'] ?? [] as $tag) {
            if (strpos($tag, 'doci-path:') === 0) {
                $docPath = substr($tag, strlen('doci-path:'));
                break;
            }
        }

        // Build URL path (remove .md extension)
        $urlPath = preg_replace('/\.md$/', '', $docPath);

        // Try to get title from DOCI DB or extract from content
        $title = '';
        $dbDoc = null;
        if ($docPath) {
            $dbDoc = get_document_by_path($docPath);
            if ($dbDoc && !empty($dbDoc['title'])) {
                $title = $dbDoc['title'];
            }
        }

        // Fallback: extract title from markdown content
        if (empty($title) && !empty($result['content'])) {
            if (preg_match('/^#\s+(.+)$/m', $result['content'], $matches)) {
                $title = trim($matches[1]);
            }
        }

        // Fallback: use filename
        if (empty($title) && $docPath) {
            $title = ucwords(str_replace(['-', '_'], ' ', basename($docPath, '.md')));
        }

        // Build breadcrumb path
        $breadcrumbPath = '';
        if ($docPath) {
            $segments = explode('/', preg_replace('/\.md$/', '', $docPath));
            $breadcrumbParts = [];
            foreach ($segments as $seg) {
                $breadcrumbParts[] = htmlspecialchars($seg);
            }
            $breadcrumbPath = implode(' / ', $breadcrumbParts);
        }

        // Generate snippet
        $snippet = generateSearchSnippet($result['content'] ?? '', $query);

        // Score and date
        $score = round(($result['similarity_score'] ?? 0) * 100);
        $date = '';
        if (!empty($result['created_at'])) {
            $ts = strtotime($result['created_at']);
            if ($ts) {
                $date = date('M j, Y', $ts);
            }
        }

        $cardUrl = '/' . htmlspecialchars($urlPath) . '.html';
        $html .= '<div class="search-result-card" data-href="' . $cardUrl . '">' . "\n";
        if ($breadcrumbPath) {
            $html .= '  <div class="search-result-breadcrumb">' . $breadcrumbPath . '</div>' . "\n";
        }
        $html .= '  <div class="search-result-title">' . htmlspecialchars($title) . '</div>' . "\n";
        $html .= '  <div class="search-result-snippet">' . highlightSearchMatches(htmlspecialchars($snippet), $query) . '</div>' . "\n";
        $html .= '  <div class="search-result-meta">';
        if ($date) {
            $html .= '<span>' . $date . '</span>';
        }
        $html .= '<span>Score: ' . $score . '%</span>';
        $html .= '</div>' . "\n";
        $html .= '</div>' . "\n";
    }

    $html .= '</div>' . "\n"; // .search-results
    $html .= '</div>' . "\n"; // .search-page

    return $html;
}

/**
 * Generate a search snippet from document content
 *
 * Strips markdown syntax, finds query text, extracts surrounding context.
 *
 * @param string $content Full document content
 * @param string $query   Search query
 * @return string 300-char snippet
 */
function generateSearchSnippet(string $content, string $query): string {
    // Strip markdown syntax
    $text = $content;
    $text = preg_replace('/^#{1,6}\s+/m', '', $text);         // headings
    $text = preg_replace('/\*\*([^*]+)\*\*/', '$1', $text);   // bold
    $text = preg_replace('/\*([^*]+)\*/', '$1', $text);        // italic
    $text = preg_replace('/\[([^\]]+)\]\([^)]+\)/', '$1', $text); // links
    $text = preg_replace('/```[\s\S]*?```/', '', $text);       // code blocks
    $text = preg_replace('/`([^`]+)`/', '$1', $text);          // inline code
    $text = preg_replace('/^\s*[-*+]\s+/m', '', $text);        // list markers
    $text = preg_replace('/^\s*\d+\.\s+/m', '', $text);        // numbered lists
    $text = preg_replace('/\n{2,}/', ' ', $text);              // multiple newlines
    $text = preg_replace('/\s+/', ' ', $text);                 // collapse whitespace
    $text = trim($text);

    $maxLen = 300;

    // Try to find query in text (case-insensitive)
    $pos = mb_stripos($text, $query);
    if ($pos !== false) {
        // Center the snippet around the match
        $start = max(0, $pos - 100);
        $snippet = mb_substr($text, $start, $maxLen);
        if ($start > 0) {
            $snippet = '...' . $snippet;
        }
        if (mb_strlen($text) > $start + $maxLen) {
            $snippet .= '...';
        }
        return $snippet;
    }

    // Try individual words
    $words = preg_split('/\s+/', $query);
    foreach ($words as $word) {
        if (mb_strlen($word) < 3) continue;
        $pos = mb_stripos($text, $word);
        if ($pos !== false) {
            $start = max(0, $pos - 100);
            $snippet = mb_substr($text, $start, $maxLen);
            if ($start > 0) $snippet = '...' . $snippet;
            if (mb_strlen($text) > $start + $maxLen) $snippet .= '...';
            return $snippet;
        }
    }

    // Fallback: first 300 chars
    $snippet = mb_substr($text, 0, $maxLen);
    if (mb_strlen($text) > $maxLen) {
        $snippet .= '...';
    }
    return $snippet;
}

/**
 * Highlight search query matches in already-escaped HTML text.
 *
 * Wraps matching substrings with <mark> tags.
 * First tries the full query, then individual words (3+ chars).
 *
 * @param string $escapedText HTML-escaped snippet text
 * @param string $query       Raw search query
 * @return string Text with <mark> highlights
 */
function highlightSearchMatches(string $escapedText, string $query): string {
    if (empty($query)) return $escapedText;

    // Try full query first
    $escapedQuery = preg_quote(htmlspecialchars($query), '/');
    $result = preg_replace('/(' . $escapedQuery . ')/iu', '<mark>$1</mark>', $escapedText);

    // If full query didn't match, try individual words
    if ($result === $escapedText) {
        $words = preg_split('/\s+/', $query);
        foreach ($words as $word) {
            if (mb_strlen($word) < 3) continue;
            $escapedWord = preg_quote(htmlspecialchars($word), '/');
            $result = preg_replace('/(' . $escapedWord . ')/iu', '<mark>$1</mark>', $result);
        }
    }

    return $result;
}

/**
 * Render 404 error page
 */
function render_404(): void {
    $errorContent = '<div class="error-page" style="text-align:center;padding:80px 20px;">'
        . '<h1 style="font-size:48px;margin-bottom:16px;opacity:0.6;">404</h1>'
        . '<p style="font-size:18px;margin-bottom:24px;">The requested document could not be found.</p>'
        . '<p><a href="/">Return to homepage</a></p>'
        . '</div>';
    render_page('404 - Page Not Found', $errorContent, '', false);
}
