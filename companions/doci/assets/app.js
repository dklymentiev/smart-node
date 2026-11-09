/**
 * DOCI - Main Application JavaScript
 *
 * Requires window.__DOCI config object to be set before loading:
 *   window.__DOCI = {
 *     rawMarkdown: string,
 *     currentPath: string,
 *     editPath: string,
 *     documentGuid: string
 *   };
 */

(function() {
    'use strict';

    var config = window.__DOCI || {};

    // CSRF token for API requests
    var csrfToken = (function() {
        var meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.getAttribute('content') : '';
    })();

    // Raw markdown content for precise thread linking
    window.rawMarkdown = config.rawMarkdown || '';

    document.addEventListener('DOMContentLoaded', function() {
        initBackToTop();
        initCopyGuid();
        initSidebar();
        initExternalLinks();
        initHeaderIds();
        initAjaxNavigation();
        initTableSorting();
        initEditMode();
        initContextMenu();
        initThreadModal();
        initAiResponse();
        initThreadBlocks();
        initThreadReply();
        initEntityChips();
        initSearchPreview();
        initKeyDocToggle();
    });

    function initKeyDocToggle() {
        var input = document.getElementById('key-doc-toggle');
        if (!input) return;
        input.addEventListener('change', async function () {
            var guid = input.dataset.guid;
            var checked = input.checked;
            input.disabled = true;
            try {
                if (checked) {
                    var resp = await fetch('/api/key-document.php', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
                        credentials: 'same-origin',
                        body: JSON.stringify({ guid: guid, domain: 'default' })
                    });
                    var data = await resp.json();
                    if (!data.success) throw new Error(data.error || 'mark failed');
                } else {
                    var rg = await fetch('/api/key-document.php?guid=' + encodeURIComponent(guid), { credentials: 'same-origin' });
                    var info = await rg.json();
                    var items = (info && info.items) || [];
                    for (var i = 0; i < items.length; i++) {
                        var rd = await fetch('/api/key-document.php?id=' + items[i].id, { method: 'DELETE', headers: { 'X-CSRF-Token': csrfToken }, credentials: 'same-origin' });
                        var jr = await rd.json();
                        if (!jr.success) throw new Error(jr.error || 'unmark failed');
                    }
                }
            } catch (err) {
                input.checked = !checked;
                alert('Toggle failed: ' + err.message);
            } finally {
                input.disabled = false;
            }
        });
    }

    // ========================================================================
    // Back to Top Button
    // ========================================================================
    function initBackToTop() {
        var backToTop = document.getElementById('back-to-top');
        var mainContent = document.querySelector('.main-content');
        if (!backToTop || !mainContent) return;

        function positionButton() {
            var rect = mainContent.getBoundingClientRect();
            var rightPos = window.innerWidth - (rect.left + rect.width) + 10;
            backToTop.style.right = Math.max(rightPos, 20) + 'px';
        }
        positionButton();
        window.addEventListener('resize', positionButton);
        window.addEventListener('scroll', function() {
            backToTop.style.display = window.scrollY > 300 ? 'block' : 'none';
        });
        backToTop.addEventListener('click', function() {
            window.scrollTo({ top: 0, behavior: 'smooth' });
        });
    }

    // ========================================================================
    // Copy GUID URL
    // ========================================================================
    function initCopyGuid() {
        var copyGuid = document.getElementById('copy-guid');
        if (!copyGuid) return;

        copyGuid.addEventListener('click', function() {
            var url = this.dataset.url;
            navigator.clipboard.writeText(url).then(function() {
                copyGuid.classList.add('copied');
                var original = copyGuid.textContent;
                copyGuid.textContent = 'Copied!';
                setTimeout(function() {
                    copyGuid.classList.remove('copied');
                    copyGuid.textContent = original;
                }, 1500);
            });
        });
    }

    // ========================================================================
    // Sidebar Tree Navigation
    // ========================================================================
    function initSidebar() {
        var currentPath = config.currentPath || '';
        var sidebar = document.querySelector('.sidebar-nav');
        var sidebarContainer = document.querySelector('.sidebar');
        var STORAGE_KEY = 'papers_tree_state';
        var SCROLL_KEY = 'papers_sidebar_scroll';

        // Restore sidebar scroll position
        if (sidebarContainer) {
            var savedScroll = sessionStorage.getItem(SCROLL_KEY);
            if (savedScroll) {
                sidebarContainer.scrollTop = parseInt(savedScroll, 10);
            }
            window.addEventListener('beforeunload', function() {
                sessionStorage.setItem(SCROLL_KEY, sidebarContainer.scrollTop);
            });
            if (sidebar) {
                sidebar.addEventListener('click', function(e) {
                    if (e.target.tagName === 'A') {
                        sessionStorage.setItem(SCROLL_KEY, sidebarContainer.scrollTop);
                    }
                });
            }
        }

        if (!sidebar) return;

        // Tree folder expand/collapse with localStorage persistence
        var allFolders = sidebar.querySelectorAll('.tree-folder[data-folder-id]');
        var savedState = {};
        try {
            savedState = JSON.parse(localStorage.getItem(STORAGE_KEY)) || {};
        } catch (e) {}

        allFolders.forEach(function(folder) {
            var folderId = folder.getAttribute('data-folder-id');
            if (savedState[folderId]) {
                folder.classList.add('open');
            }
        });

        // Expand parents of active link
        var activeLink = sidebar.querySelector('a.active');
        if (activeLink) {
            var parent = activeLink.parentElement;
            while (parent && parent !== sidebar) {
                if (parent.classList.contains('tree-folder')) {
                    parent.classList.add('open');
                }
                parent = parent.parentElement;
            }
        }

        // Toggle handler with state save
        sidebar.addEventListener('click', function(e) {
            if (e.target.classList.contains('folder-toggle')) {
                var folder = e.target.closest('.tree-folder');
                if (folder) {
                    folder.classList.toggle('open');
                    var folderId = folder.getAttribute('data-folder-id');
                    try {
                        var state = JSON.parse(localStorage.getItem(STORAGE_KEY)) || {};
                        state[folderId] = folder.classList.contains('open');
                        localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
                    } catch (e) {}
                }
            }

            // Show more files
            if (e.target.classList.contains('show-more-btn')) {
                var p = e.target.closest('.tree-folder') || sidebar;
                p.querySelectorAll('.hidden-file').forEach(function(f) {
                    f.classList.remove('hidden-file');
                });
                e.target.parentElement.style.display = 'none';
            }
        });
    }

    // ========================================================================
    // External Links
    // ========================================================================
    function initExternalLinks() {
        processExternalLinks();
    }

    function processExternalLinks() {
        document.querySelectorAll('.markdown-content a').forEach(function(link) {
            if (link.hostname && link.hostname !== window.location.hostname) {
                link.setAttribute('target', '_blank');
                link.setAttribute('rel', 'noopener noreferrer');
            }
        });
    }

    // ========================================================================
    // Header Anchor IDs
    // ========================================================================
    function initHeaderIds() {
        generateHeaderIds();
    }

    function generateHeaderIds() {
        document.querySelectorAll('.markdown-content h2, .markdown-content h3').forEach(function(header) {
            var text = header.textContent.trim();
            var id = text.toLowerCase()
                .replace(/[^a-z0-9\s-]/g, '')
                .replace(/\s+/g, '-')
                .replace(/-+/g, '-')
                .replace(/^-|-$/g, '');
            if (id) {
                header.id = id;
            }
        });
    }

    // ========================================================================
    // AJAX Navigation
    // ========================================================================
    function initAjaxNavigation() {
        var contentArea = document.querySelector('.markdown-content');
        // Update only the breadcrumb link list -- the surrounding `.breadcrumbs`
        // container also holds the search form and the edit/delete controls,
        // which must survive SPA navigation.
        var breadcrumbsArea = document.querySelector('.breadcrumbs-links');
        var sidebarNav = document.querySelector('.sidebar-nav');
        var recentSidebar = document.querySelector('.recent-sidebar');

        if (!contentArea) return;

        function loadPage(url, pushState) {
            var path = url.replace(/\.html$/, '').replace(/^\//, '') || 'index';
            // Preserve query string (for search ?q=...)
            var queryString = '';
            var qIdx = path.indexOf('?');
            if (qIdx !== -1) {
                queryString = '&' + path.substring(qIdx + 1);
                path = path.substring(0, qIdx);
            }
            var ajaxUrl = '/?path=' + encodeURIComponent(path) + '&ajax=1' + queryString;

            contentArea.style.opacity = '0.5';

            fetch(ajaxUrl)
                .then(function(response) {
                    if (!response.ok) throw new Error('Page not found');
                    return response.json();
                })
                .then(function(data) {
                    contentArea.innerHTML = data.content;
                    contentArea.style.opacity = '1';

                    if (breadcrumbsArea) {
                        breadcrumbsArea.innerHTML = data.breadcrumbs;
                    }

                    // Update document metadata
                    var metaSection = document.querySelector('.document-meta');
                    if (metaSection && data.meta) {
                        if (data.meta.isDirectory) {
                            metaSection.style.display = 'none';
                        } else {
                            var metaHtml = '';

                            if (data.meta.isThread && data.meta.thread) {
                                metaHtml += '<span class="meta-item"><span class="meta-label">Thread by</span> ' + escapeHtml(data.meta.thread.author) + '</span>';
                                metaHtml += '<span class="meta-sep">|</span>';
                            }

                            metaHtml += '<span class="meta-item"><span class="meta-label">Path:</span> ' + escapeHtml(data.meta.path) + '</span>';

                            if (data.meta.guid) {
                                var guidUrl = window.location.origin + '/' + data.meta.guid;
                                metaHtml += '<span class="meta-sep">|</span>';
                                metaHtml += '<span class="meta-item"><span class="meta-label">GUID:</span> <span class="document-guid" id="copy-guid" data-url="' + guidUrl + '">' + escapeHtml(data.meta.guid) + '</span></span>';
                                metaHtml += '<span class="meta-sep">|</span>';
                                metaHtml += '<label class="key-switch" title="' + (data.meta.isKey ? 'Marked as canonical' : 'Mark as canonical') + '">';
                                metaHtml += '<input type="checkbox" id="key-doc-toggle" data-guid="' + escapeHtml(data.meta.guid) + '"' + (data.meta.isKey ? ' checked' : '') + '>';
                                metaHtml += '<span class="key-switch-slider"></span><span class="key-switch-label">Canonical</span>';
                                metaHtml += '</label>';
                            }

                            if (data.meta.commits && data.meta.commits.length > 0 && data.meta.commitUrlBase) {
                                metaHtml += '<span class="meta-sep">|</span>';
                                metaHtml += '<span class="meta-item"><span class="meta-label">History:</span> ';
                                var commitLinks = data.meta.commits.map(function(c) {
                                    return '<a href="' + data.meta.commitUrlBase + '/' + c.hash + '" target="_blank" title="' + escapeHtml(c.message) + ' by ' + escapeHtml(c.author) + ' ' + escapeHtml(c.date) + '">' + c.hash + '</a>';
                                });
                                metaHtml += commitLinks.join(', ');
                                metaHtml += '</span>';
                            }

                            metaSection.innerHTML = metaHtml;
                            metaSection.style.display = '';

                            // Re-attach click handler for GUID copy
                            var copyGuid = document.getElementById('copy-guid');
                            if (copyGuid) {
                                copyGuid.onclick = function() {
                                    navigator.clipboard.writeText(this.dataset.url);
                                    var original = this.textContent;
                                    this.textContent = 'Copied!';
                                    setTimeout(function() { copyGuid.textContent = original; }, 1500);
                                };
                            }

                            // Re-attach handler for the freshly-rebuilt Key switch.
                            initKeyDocToggle();
                        }
                    }

                    // Swap the version bar with the one rendered for the
                    // new page. data.versionBar is '' when the page has
                    // no versions to navigate -- in that case we just
                    // remove the stale bar from prior page.
                    var versionBar = document.querySelector('.version-bar');
                    var newBarHtml = (data.versionBar || '').trim();
                    if (newBarHtml) {
                        if (versionBar) {
                            versionBar.outerHTML = newBarHtml;
                        } else {
                            // No bar in DOM yet -- insert before article.
                            var article = contentArea;
                            article.insertAdjacentHTML('beforebegin', newBarHtml);
                        }
                    } else if (versionBar) {
                        versionBar.remove();
                    }

                    // Sync edit/delete controls visibility with the new path
                    syncEditControls(data.meta);

                    document.title = data.title + ' - DOCI';

                    if (pushState) {
                        history.pushState({ path: url }, data.title, url);
                    }

                    // Hide/show meta section for search pages
                    if (data.meta && data.meta.isSearch && metaSection) {
                        metaSection.style.display = 'none';
                    }

                    // Re-bind search forms and preview after AJAX load
                    bindSearchForms();
                    bindSearchPreview();

                    // Newly loaded content has fresh DOM nodes -- rebind
                    // anything that listens for events on rendered content.
                    initThreadBlocks();
                    initEntityChips();
                    initAiResponse();
                    initThreadReply();

                    updateActiveLink(data.path);
                    window.scrollTo(0, 0);
                    processExternalLinks();
                    generateHeaderIds();
                })
                .catch(function(err) {
                    console.error('AJAX navigation error:', err);
                    window.location.href = url;
                });
        }

        function updateActiveLink(path) {
            document.querySelectorAll('.nav-file.nav-active').forEach(function(div) {
                div.classList.remove('nav-active');
            });

            function expandParents(element) {
                var parent = element.parentElement;
                while (parent) {
                    if (parent.classList.contains('nav-folder')) {
                        parent.classList.add('open');
                        var toggle = parent.querySelector(':scope > .nav-folder-toggle');
                        if (toggle) toggle.textContent = '[-]';
                    }
                    parent = parent.parentElement;
                }
            }

            var selector = 'a[href="/' + path + '.html"]';
            document.querySelectorAll(selector).forEach(function(a) {
                var navFile = a.closest('.nav-file');
                if (navFile) {
                    navFile.classList.add('nav-active');
                    expandParents(navFile);
                }
            });

            if (path === '' || path === 'index') {
                document.querySelectorAll('a[href="/index.html"], a[href="/"]').forEach(function(a) {
                    var navFile = a.closest('.nav-file');
                    if (navFile) {
                        navFile.classList.add('nav-active');
                        expandParents(navFile);
                    }
                });
            }
        }

        function handleClick(e) {
            var link = e.target.closest('a');
            if (!link) return;

            var href = link.getAttribute('href');
            if (!href) return;

            if (href.startsWith('http') || href.startsWith('#') || href.startsWith('mailto:')) return;
            if (!href.startsWith('/')) return;
            // Skip static asset paths and known binary types -- let the browser
            // handle those natively (download or open in tab).
            if (/^\/(assets|files\/\.data|api)\//.test(href)) return;
            if (/\.(pdf|zip|tar|gz|png|jpe?g|gif|svg|webp|ico|mp4|mp3|wav|woff2?|ttf|css|js|json|xml)(\?|#|$)/i.test(href)) return;
            if (e.ctrlKey || e.metaKey || e.shiftKey) return;

            e.preventDefault();
            loadPage(href, true);
        }

        if (sidebarNav) sidebarNav.addEventListener('click', handleClick);
        if (recentSidebar) recentSidebar.addEventListener('click', handleClick);
        contentArea.addEventListener('click', handleClick);

        window.addEventListener('popstate', function(e) {
            if (e.state && e.state.path) {
                loadPage(e.state.path, false);
            } else {
                loadPage(window.location.pathname, false);
            }
        });

        history.replaceState({ path: window.location.pathname }, document.title, window.location.pathname);

        // Search form AJAX handler
        function bindSearchForms() {
            document.querySelectorAll('.search-bar-form, .search-page-form, .folder-search-form').forEach(function(form) {
                // Avoid binding twice
                if (form.dataset.bound) return;
                form.dataset.bound = '1';
                form.addEventListener('submit', function(e) {
                    e.preventDefault();
                    var input = form.querySelector('input[name="q"]');
                    var q = (input ? input.value : '').trim();
                    if (!q) return;
                    var url = '/search.html?q=' + encodeURIComponent(q);
                    var folderInput = form.querySelector('input[name="folder"]');
                    if (folderInput && folderInput.value) {
                        url += '&folder=' + encodeURIComponent(folderInput.value);
                    }
                    loadPage(url, true);
                });
            });
        }
        bindSearchForms();
    }

    // ========================================================================
    // Table Sorting
    // ========================================================================
    function initTableSorting() {
        var table = document.querySelector('.file-list-table');
        if (!table) return;

        var headers = table.querySelectorAll('thead th');
        var currentSortColumn = null;
        var currentSortDirection = 'asc';

        headers.forEach(function(header, index) {
            header.style.cursor = 'pointer';
            header.setAttribute('data-sort-column', index);

            header.addEventListener('click', function() {
                var tbody = table.querySelector('tbody');
                var rows = Array.from(tbody.querySelectorAll('tr'));

                if (currentSortColumn === index) {
                    currentSortDirection = currentSortDirection === 'asc' ? 'desc' : 'asc';
                } else {
                    currentSortDirection = 'asc';
                    currentSortColumn = index;
                }

                headers.forEach(function(h) {
                    h.classList.remove('sort-asc', 'sort-desc');
                });

                rows.sort(function(a, b) {
                    var aDataSort = a.cells[index].getAttribute('data-sort');
                    var bDataSort = b.cells[index].getAttribute('data-sort');

                    if (aDataSort !== null && bDataSort !== null) {
                        var aVal = parseInt(aDataSort, 10) || 0;
                        var bVal = parseInt(bDataSort, 10) || 0;
                        var comparison = aVal - bVal;
                        return currentSortDirection === 'asc' ? comparison : -comparison;
                    }

                    var aText = a.cells[index].textContent.trim();
                    var bText = b.cells[index].textContent.trim();
                    var comparison = aText.localeCompare(bText, undefined, { numeric: true, sensitivity: 'base' });
                    return currentSortDirection === 'asc' ? comparison : -comparison;
                });

                rows.forEach(function(row) { tbody.appendChild(row); });
                header.classList.add(currentSortDirection === 'asc' ? 'sort-asc' : 'sort-desc');
            });
        });
    }

    // ========================================================================
    // Edit Mode
    // ========================================================================
    function initEditMode() {
        var editBtn = document.getElementById('edit-btn');
        var saveBtn = document.getElementById('save-btn');
        var cancelBtn = document.getElementById('cancel-btn');
        var editStatus = document.getElementById('edit-status');
        var viewMode = document.getElementById('view-mode');
        var editMode = document.getElementById('edit-mode');
        var editor = document.getElementById('editor');

        if (!editBtn || !editor) return;

        var controls = document.querySelector('.edit-controls');
        var originalContent = editor.value;
        function getEditPath() {
            return (controls && controls.dataset.editPath) || config.editPath || '';
        }
        function getDocGuid() {
            return (controls && controls.dataset.docGuid) || config.documentGuid || '';
        }

        editBtn.addEventListener('click', function() {
            viewMode.style.display = 'none';
            editMode.style.display = 'block';
            editBtn.style.display = 'none';
            saveBtn.style.display = 'inline-block';
            cancelBtn.style.display = 'inline-block';
            editStatus.textContent = '';
            editor.focus();
        });

        cancelBtn.addEventListener('click', function() {
            editor.value = originalContent;
            viewMode.style.display = 'block';
            editMode.style.display = 'none';
            editBtn.style.display = 'inline-block';
            saveBtn.style.display = 'none';
            cancelBtn.style.display = 'none';
            editStatus.textContent = '';
        });

        saveBtn.addEventListener('click', function() {
            editStatus.textContent = 'Saving...';
            editStatus.className = 'edit-status';
            saveBtn.disabled = true;

            fetch('/api/save.php', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
                body: JSON.stringify({
                    path: getEditPath(),
                    content: editor.value
                })
            })
            .then(function(response) { return response.json(); })
            .then(function(data) {
                if (data.success) {
                    editStatus.textContent = 'Saved!';
                    editStatus.className = 'edit-status success';
                    originalContent = editor.value;
                    setTimeout(function() { window.location.reload(); }, 500);
                } else {
                    editStatus.textContent = 'Error: ' + (data.error || 'Unknown error');
                    editStatus.className = 'edit-status error';
                    saveBtn.disabled = false;
                }
            })
            .catch(function(err) {
                editStatus.textContent = 'Error: ' + err.message;
                editStatus.className = 'edit-status error';
                saveBtn.disabled = false;
            });
        });

        // Warn before leaving with unsaved changes
        window.addEventListener('beforeunload', function(e) {
            if (editMode.style.display !== 'none' && editor.value !== originalContent) {
                e.preventDefault();
                e.returnValue = '';
            }
        });

        // Delete button handler
        var deleteBtn = document.getElementById('delete-btn');
        if (deleteBtn) {
            deleteBtn.addEventListener('click', function() {
                var docGuid = getDocGuid();
                if (!docGuid) {
                    alert('Cannot delete: document not registered');
                    return;
                }
                if (!confirm('Are you sure you want to delete this document?')) {
                    return;
                }
                deleteBtn.disabled = true;
                deleteBtn.textContent = 'Deleting...';

                fetch('/api/documents.php?guid=' + encodeURIComponent(docGuid), {
                    method: 'DELETE',
                    headers: { 'X-CSRF-Token': csrfToken },
                    credentials: 'same-origin'
                })
                .then(function(res) { return res.json(); })
                .then(function(data) {
                    if (data.success) {
                        var redirectTo = data.redirect_to ? '/' + data.redirect_to : '/';
                        window.location.href = redirectTo;
                    } else {
                        alert('Delete failed: ' + (data.error || 'Unknown error'));
                        deleteBtn.disabled = false;
                        deleteBtn.textContent = 'Delete';
                    }
                })
                .catch(function(err) {
                    alert('Delete failed: ' + err.message);
                    deleteBtn.disabled = false;
                    deleteBtn.textContent = 'Delete';
                });
            });
        }
    }

    // Called after AJAX navigation to re-sync the edit/delete controls
    // and the editor textarea with the newly loaded document.
    function syncEditControls(meta) {
        var controls = document.querySelector('.edit-controls');
        var editor = document.getElementById('editor');
        var editMode = document.getElementById('edit-mode');
        if (!controls || !meta) return;

        if (meta.isEditable) {
            controls.style.display = '';
            controls.dataset.editPath = meta.editPath || '';
            controls.dataset.docGuid = meta.guid || '';
            if (editor) {
                editor.value = meta.rawContent || '';
            }
        } else {
            controls.style.display = 'none';
            controls.dataset.editPath = '';
            controls.dataset.docGuid = '';
            if (editor) editor.value = '';
        }

        // If the editor was open for the previous page, snap back to view mode.
        if (editMode && editMode.style.display !== 'none') {
            editMode.style.display = 'none';
            var viewMode = document.getElementById('view-mode');
            if (viewMode) viewMode.style.display = 'block';
            ['edit-btn','save-btn','cancel-btn'].forEach(function(id) {
                var el = document.getElementById(id);
                if (!el) return;
                el.style.display = (id === 'edit-btn') ? 'inline-block' : 'none';
            });
        }
    }

    // ========================================================================
    // Context Menu
    // ========================================================================
    function initContextMenu() {
        var contextMenu = document.getElementById('context-menu');
        var contextMenuDoc = document.getElementById('context-menu-doc');
        if (!contextMenu) return;

        var selectedText = '';
        var selectionRange = null;
        var validationResult = null;
        var startThreadItem = contextMenu.querySelector('[data-action="start-thread"]');
        // Read the current page's GUID from the live DOM (#copy-guid),
        // not from a closure captured at first page load -- otherwise
        // creating a thread after SPA navigation posts to the stale doc.
        function getCurrentDocumentGuid() {
            var el = document.getElementById('copy-guid');
            return (el && el.textContent.trim()) || config.documentGuid || '';
        }

        // Handle "Discuss Document" click
        if (contextMenuDoc) {
            contextMenuDoc.addEventListener('click', function(e) {
                var item = e.target.closest('.context-menu-item');
                if (!item) return;
                if (item.dataset.action === 'discuss-document') {
                    contextMenuDoc.classList.remove('visible');
                    window.threadContext = {
                        documentGuid: getCurrentDocumentGuid(),
                        selectedText: '',
                        contextBefore: '',
                        contextAfter: '',
                        isBlock: false
                    };
                    window.validationResult = { valid: true };
                    window.openThreadModal('');
                }
            });
        }

        // Show custom context menu on right-click when text is selected
        document.addEventListener('contextmenu', function(e) {
            var selection = window.getSelection();
            var text = selection.toString().trim();

            contextMenu.classList.remove('visible');
            if (contextMenuDoc) contextMenuDoc.classList.remove('visible');

            if (e.target.closest('.context-menu') || e.target.closest('.context-menu-doc') || e.target.closest('textarea') || e.target.closest('input')) {
                return;
            }

            // No text selected - show document-level menu
            if (text.length === 0 && getCurrentDocumentGuid() && e.target.closest('.markdown-content')) {
                e.preventDefault();
                // Menu is position:fixed -- use viewport-relative coordinates,
                // not page coordinates, otherwise the menu drifts down by the
                // current scrollY on long pages.
                var x = e.clientX;
                var y = e.clientY;
                if (x + 200 > window.innerWidth) x = window.innerWidth - 210;
                if (y + 50 > window.innerHeight) y = y - 50;
                contextMenuDoc.style.left = x + 'px';
                contextMenuDoc.style.top = y + 'px';
                contextMenuDoc.classList.add('visible');
                return;
            }

            // Text selected - show selection menu
            if (text.length > 0) {
                e.preventDefault();

                selectedText = text;
                selectionRange = selection.getRangeAt(0).cloneRange();
                validationResult = null;

                // position:fixed -- viewport-relative coords (clientX/Y).
                var x = e.clientX;
                var y = e.clientY;
                var menuWidth = 180;
                var menuHeight = 80;
                if (x + menuWidth > window.innerWidth) {
                    x = window.innerWidth - menuWidth - 10;
                }
                if (y + menuHeight > window.innerHeight) {
                    y = y - menuHeight;
                }

                contextMenu.style.left = x + 'px';
                contextMenu.style.top = y + 'px';

                if (startThreadItem) {
                    startThreadItem.style.opacity = '0.5';
                    startThreadItem.style.pointerEvents = 'none';
                    startThreadItem.title = 'Validating...';
                }

                contextMenu.classList.add('visible');

                validateSelection(text, selectionRange).then(function(result) {
                    validationResult = result;
                    if (startThreadItem) {
                        if (result.valid) {
                            startThreadItem.style.opacity = '1';
                            startThreadItem.style.pointerEvents = 'auto';
                            startThreadItem.title = '';
                        } else {
                            startThreadItem.style.opacity = '0.5';
                            startThreadItem.style.pointerEvents = 'none';
                            startThreadItem.title = result.reason;
                        }
                    }
                });
            }
        });

        // Validate selection via API
        function validateSelection(text, range) {
            var markdown = window.rawMarkdown || '';

            if (!getCurrentDocumentGuid()) {
                return Promise.resolve({ valid: false, reason: 'Document not registered' });
            }

            var contextBefore = '';
            var contextAfter = '';
            var occurrenceIndex = 0;

            try {
                if (markdown && text) {
                    var contentEl = document.querySelector('.markdown-content');
                    if (contentEl && range) {
                        var textBefore = '';
                        var walker = document.createTreeWalker(contentEl, NodeFilter.SHOW_TEXT);
                        var node;
                        while ((node = walker.nextNode())) {
                            if (node === range.startContainer) {
                                textBefore += node.textContent.substring(0, range.startOffset);
                                break;
                            }
                            textBefore += node.textContent;
                        }
                        var count = 0;
                        var idx = 0;
                        while ((idx = textBefore.indexOf(text, idx)) !== -1) {
                            count++;
                            idx += text.length;
                        }
                        occurrenceIndex = count;
                    }

                    var pos = -1;
                    var searchPos = 0;
                    for (var i = 0; i <= occurrenceIndex; i++) {
                        pos = markdown.indexOf(text, searchPos);
                        if (pos === -1) break;
                        searchPos = pos + 1;
                    }

                    if (pos !== -1) {
                        contextBefore = markdown.substring(Math.max(0, pos - 50), pos);
                        contextAfter = markdown.substring(pos + text.length, pos + text.length + 50);
                    }
                }
            } catch (e) {
                console.warn('Context extraction error:', e);
            }

            window.threadContext = {
                selectedText: text,
                contextBefore: contextBefore,
                contextAfter: contextAfter,
                occurrenceIndex: occurrenceIndex,
                documentPath: config.currentPath || '',
                documentGuid: getCurrentDocumentGuid()
            };

            return fetch('/api/validate-selection.php', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'same-origin',
                body: JSON.stringify({
                    documentGuid: getCurrentDocumentGuid(),
                    selectedText: text,
                    occurrenceIndex: occurrenceIndex
                })
            })
            .then(function(res) { return res.json(); })
            .then(function(envelope) {
                // Standard envelope from v0.2: {success, data: {valid,
                // reason, markdownFragment, isBlock?}, request_id}.
                // Unwrap to the legacy shape the rest of the code expects.
                var result = (envelope && envelope.data) ? envelope.data
                    : { valid: false, reason: envelope && envelope.error
                        ? envelope.error : 'Validation failed',
                        markdownFragment: '' };
                window.validationResult = result;
                if (window.threadContext) {
                    window.threadContext.isBlock = result.isBlock || false;
                }
                return result;
            })
            .catch(function(err) {
                console.error('Validation error:', err);
                return { valid: false, reason: 'Validation failed', markdownFragment: '' };
            });
        }

        // Hide menu on click outside or scroll
        document.addEventListener('mousedown', function(e) {
            if (!e.target.closest('.context-menu')) {
                contextMenu.classList.remove('visible');
                if (contextMenuDoc) contextMenuDoc.classList.remove('visible');
            }
        });
        document.addEventListener('scroll', function() {
            contextMenu.classList.remove('visible');
            if (contextMenuDoc) contextMenuDoc.classList.remove('visible');
        });

        // Handle menu actions
        contextMenu.addEventListener('click', function(e) {
            var item = e.target.closest('.context-menu-item');
            if (!item) return;

            var action = item.dataset.action;

            if (action === 'copy') {
                navigator.clipboard.writeText(selectedText);
                contextMenu.classList.remove('visible');
            }

            if (action === 'start-thread') {
                if (!validationResult || !validationResult.valid) {
                    var reason = validationResult ? validationResult.reason : 'Selection not validated';
                    alert(reason);
                    contextMenu.classList.remove('visible');
                    return;
                }

                contextMenu.classList.remove('visible');
                window.openThreadModal(selectedText);
            }
        });
    }

    // ========================================================================
    // Thread Modal
    // ========================================================================
    function initThreadModal() {
        var modal = document.getElementById('thread-modal');
        var quoteEl = document.getElementById('thread-quote');
        var commentEl = document.getElementById('thread-comment');
        var aiToggle = document.getElementById('thread-ai-toggle');
        var modelSelector = document.getElementById('ai-model-selector');
        var modelChips = modal ? modal.querySelectorAll('.ai-model-chip') : [];
        var selectedModel = 'sonnet';
        if (!modal) return;

        // If no AI provider is configured server-side, hide every AI
        // control in the thread modal -- otherwise users tick a checkbox
        // that always fails. The whole label container is removed.
        var aiCfg = (config.ai || { enabled: false, models: [] });
        if (!aiCfg.enabled) {
            var aiLabel = aiToggle ? aiToggle.closest('label') : null;
            if (aiLabel) aiLabel.style.display = 'none';
            if (modelSelector) modelSelector.style.display = 'none';
        } else {
            // Hide chips for model aliases the operator didn't configure.
            var allowedModels = aiCfg.models || [];
            var firstAllowed = null;
            modelChips.forEach(function(chip) {
                if (allowedModels.indexOf(chip.dataset.model) === -1) {
                    chip.style.display = 'none';
                } else if (firstAllowed === null) {
                    firstAllowed = chip.dataset.model;
                }
            });
            if (firstAllowed && allowedModels.indexOf(selectedModel) === -1) {
                selectedModel = firstAllowed;
                modelChips.forEach(function(c) {
                    c.classList.toggle('selected', c.dataset.model === selectedModel);
                });
            }
        }

        if (aiToggle && modelSelector) {
            aiToggle.addEventListener('change', function() {
                modelSelector.classList.toggle('visible', this.checked);
            });
        }

        modelChips.forEach(function(chip) {
            chip.addEventListener('click', function() {
                modelChips.forEach(function(c) { c.classList.remove('selected'); });
                this.classList.add('selected');
                selectedModel = this.dataset.model;
            });
        });

        window.openThreadModal = function(quoteText) {
            var isDocDiscuss = !quoteText;
            quoteEl.textContent = quoteText;
            quoteEl.style.display = isDocDiscuss ? 'none' : '';
            var debugBlock = modal.querySelector('.debug-context');
            if (debugBlock) debugBlock.style.display = isDocDiscuss ? 'none' : '';
            modal.querySelector('.modal-header span').textContent = isDocDiscuss ? 'Discuss Document' : 'Start Thread';
            commentEl.placeholder = isDocDiscuss ? 'What would you like to discuss about this document?' : 'Write something...';
            commentEl.value = '';
            if (aiToggle) aiToggle.checked = true;
            if (modelSelector) modelSelector.classList.add('visible');
            selectedModel = 'sonnet';
            modelChips.forEach(function(c) {
                c.classList.toggle('selected', c.dataset.model === 'sonnet');
            });

            // Fill debug context
            var ctx = window.threadContext || {};
            var validation = window.validationResult || {};
            var debugBefore = document.getElementById('debug-context-before');
            var debugSelected = document.getElementById('debug-context-selected');
            var debugAfter = document.getElementById('debug-context-after');
            var debugMarkdown = document.getElementById('debug-markdown-fragment');
            var debugFull = document.getElementById('debug-context-full');
            if (debugBefore) debugBefore.textContent = ctx.contextBefore || '(empty)';
            if (debugSelected) debugSelected.textContent = ctx.selectedText || '(empty)';
            if (debugAfter) debugAfter.textContent = ctx.contextAfter || '(empty)';
            if (debugMarkdown) debugMarkdown.textContent = validation.markdownFragment || '(empty)';
            if (debugFull) debugFull.textContent = (ctx.contextBefore || '') + '[' + (ctx.selectedText || '') + ']' + (ctx.contextAfter || '');

            modal.classList.add('visible');
            commentEl.focus();
        };

        function closeModal() {
            modal.classList.remove('visible');
        }

        modal.addEventListener('click', function(e) {
            if (e.target === modal) {
                closeModal();
            }
        });

        modal.addEventListener('click', function(e) {
            var action = e.target.dataset.action;
            if (action === 'close') {
                closeModal();
            }
            if (action === 'create') {
                var comment = commentEl.value.trim();
                if (!comment) {
                    commentEl.focus();
                    return;
                }

                var requestAi = aiToggle && aiToggle.checked;

                e.target.disabled = true;
                e.target.textContent = requestAi ? 'Creating & asking AI...' : 'Creating...';

                fetch('/api/thread.php', {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
                    body: JSON.stringify({
                        documentGuid: window.threadContext.documentGuid,
                        quote: window.threadContext.selectedText,
                        occurrenceIndex: window.threadContext.occurrenceIndex || 0,
                        comment: comment,
                        requestAiResponse: requestAi,
                        aiModel: requestAi ? selectedModel : null,
                        isBlock: window.threadContext.isBlock || false
                    })
                })
                .then(function(res) { return res.json(); })
                .then(function(data) {
                    if (data.success) {
                        closeModal();
                        window.location.href = data.thread.url;
                    } else {
                        alert('Error: ' + data.error);
                        e.target.disabled = false;
                        e.target.textContent = 'Create Thread';
                    }
                })
                .catch(function(err) {
                    alert('Error: ' + err.message);
                    e.target.disabled = false;
                    e.target.textContent = 'Create Thread';
                });
            }
        });

        document.addEventListener('keydown', function(e) {
            if (e.key === 'Escape' && modal.classList.contains('visible')) {
                closeModal();
            }
        });
    }

    // ========================================================================
    // AI Response via AJAX
    // ========================================================================
    function initAiResponse() {
        var aiLoading = document.querySelector('.ai-loading');
        if (!aiLoading) return;

        var pathParts = window.location.pathname.split('/');
        var threadGuid = pathParts[pathParts.length - 1] || pathParts[pathParts.length - 2];

        if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(threadGuid)) {
            aiLoading.innerHTML = '<div class="ai-loading-text">Could not determine thread ID</div>';
            return;
        }

        var loadingText = aiLoading.querySelector('.ai-loading-text');
        if (loadingText) {
            loadingText.textContent = 'Calling AI...';
        }

        fetch('/api/ai-response.php', {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
            body: JSON.stringify({ threadGuid: threadGuid })
        })
        .then(function(res) { return res.json(); })
        .then(function(data) {
            if (data.success) {
                window.location.reload();
            } else {
                aiLoading.innerHTML = '<div class="ai-loading-text" style="color: #f44;">AI error: ' + (data.error || 'Unknown error') + '</div>';
            }
        })
        .catch(function(err) {
            aiLoading.innerHTML = '<div class="ai-loading-text" style="color: #f44;">Request failed: ' + err.message + '</div>';
        });
    }

    // ========================================================================
    // Thread Block Interactions
    // ========================================================================
    function initThreadBlocks() {
        var threadBlocks = document.querySelectorAll('.thread-block, .thread-block-fragment');
        if (!threadBlocks.length) return;

        threadBlocks.forEach(function(block) {
            var guid = block.dataset.threadGuid;
            if (!guid) return;

            block.setAttribute('role', 'link');
            block.setAttribute('tabindex', '0');

            block.addEventListener('click', function(e) {
                // Honour clicks that landed on real anchors inside the block.
                if (e.target.closest('a')) return;
                // Ignore clicks during a text selection drag.
                var sel = window.getSelection();
                if (sel && sel.toString().length > 0) return;
                window.location.href = '/' + guid;
            });

            block.addEventListener('keydown', function(e) {
                if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    window.location.href = '/' + guid;
                }
            });
        });

        // Highlight thread block from URL hash
        var hash = window.location.hash;
        if (hash && hash.startsWith('#thread-')) {
            var targetGuid = hash.substring(8);
            var targetBlock = document.querySelector('.thread-block[data-thread-guid="' + targetGuid + '"]');
            if (targetBlock) {
                targetBlock.classList.add('active');
                targetBlock.scrollIntoView({ behavior: 'smooth', block: 'center' });
            }
        }
    }

    // ========================================================================
    // Thread Reply
    // ========================================================================
    function initThreadReply() {
        var replySection = document.getElementById('thread-reply-section');
        if (!replySection) return;

        var replyInput = document.getElementById('thread-reply-input');
        var replyBtn = document.getElementById('thread-reply-btn');

        var pathParts = window.location.pathname.split('/');
        var threadGuid = pathParts[pathParts.length - 1] || pathParts[pathParts.length - 2];

        if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(threadGuid)) {
            replySection.style.display = 'none';
            return;
        }

        // Auto-resize textarea
        replyInput.addEventListener('input', function() {
            this.style.height = 'auto';
            this.style.height = Math.min(this.scrollHeight, 200) + 'px';
        });

        // Send on Enter (without Shift)
        replyInput.addEventListener('keydown', function(e) {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                replyBtn.click();
            }
        });

        replyBtn.addEventListener('click', function() {
            var message = replyInput.value.trim();
            if (!message) return;

            replyBtn.disabled = true;
            replyBtn.textContent = 'Sending...';

            fetch('/api/thread-reply.php', {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
                body: JSON.stringify({
                    threadGuid: threadGuid,
                    message: message
                })
            })
            .then(function(res) { return res.json(); })
            .then(function(data) {
                if (data.success) {
                    window.location.reload();
                } else {
                    alert('Error: ' + (data.error || 'Unknown error'));
                    replyBtn.disabled = false;
                    replyBtn.textContent = 'Send';
                }
            })
            .catch(function(err) {
                alert('Request failed: ' + err.message);
                replyBtn.disabled = false;
                replyBtn.textContent = 'Send';
            });
        });
    }

    // ========================================================================
    // Entity Chips (hover tooltip with entity data)
    // ========================================================================
    function initEntityChips() {
        var pinnedChip = null; // currently pinned tooltip

        function buildTooltipHtml(data) {
            var typeLabel = data.type || 'unknown';
            var html = '<div class="entity-tooltip-header">'
                + '<span class="entity-tooltip-type entity-tooltip-type-' + typeLabel + '">' + typeLabel + '</span>'
                + '<span class="entity-tooltip-name">' + escapeHtml(data.name || '') + '</span>'
                + '</div>';
            if (data.description) html += '<div class="entity-tooltip-desc">' + escapeHtml(data.description) + '</div>';

            var meta = [];
            if (data.country && data.country.length === 2) meta.push(data.country.toUpperCase());
            if (data.industry) meta.push(escapeHtml(data.industry));
            if (data.category) meta.push(escapeHtml(data.category));
            if (data.role) meta.push(escapeHtml(data.role));
            if (meta.length) html += '<div class="entity-tooltip-meta">' + meta.join(' &middot; ') + '</div>';

            if (data.links && data.links.length) {
                html += '<div class="entity-tooltip-links">';
                for (var i = 0; i < data.links.length && i < 5; i++) {
                    var link = data.links[i];
                    html += '<a href="' + escapeHtml(link.url) + '" target="_blank" rel="noopener noreferrer">' + escapeHtml(link.title || link.platform || 'link') + '</a>';
                }
                html += '</div>';
            }
            // Copy buttons (only in pinned mode, but always build html)
            html += '<div class="entity-tooltip-actions">'
                + '<button class="entity-copy-btn" data-copy="text">Copy text</button>'
                + '<button class="entity-copy-btn" data-copy="json">Copy JSON</button>'
                + '</div>';

            return html;
        }

        function handleCopyClick(e, data) {
            var btn = e.target.closest('.entity-copy-btn');
            if (!btn) return;
            e.stopPropagation();
            var mode = btn.getAttribute('data-copy');
            var text;
            if (mode === 'json') {
                text = JSON.stringify(data, null, 2);
            } else {
                var parts = [data.name || ''];
                if (data.type) parts.push('[' + data.type + ']');
                if (data.description) parts.push(data.description);
                if (data.country) parts.push('Country: ' + data.country.toUpperCase());
                if (data.industry) parts.push('Industry: ' + data.industry);
                if (data.links && data.links.length) {
                    data.links.forEach(function(l) { parts.push((l.title || l.platform) + ': ' + l.url); });
                }
                text = parts.join('\n');
            }
            navigator.clipboard.writeText(text).then(function() {
                btn.textContent = 'Copied!';
                setTimeout(function() { btn.textContent = mode === 'json' ? 'Copy JSON' : 'Copy text'; }, 1500);
            });
        }

        function createTooltip(chip, data, pinned) {
            var tooltip = document.createElement('div');
            tooltip.className = 'entity-tooltip' + (pinned ? ' entity-tooltip-pinned' : '');
            tooltip.innerHTML = buildTooltipHtml(data);
            if (pinned) {
                tooltip.addEventListener('click', function(e) { handleCopyClick(e, data); });
            }
            chip.appendChild(tooltip);

            // Reposition if off-screen
            var rect = tooltip.getBoundingClientRect();
            if (rect.top < 0) { tooltip.style.bottom = 'auto'; tooltip.style.top = 'calc(100% + 8px)'; }
            if (rect.left < 0) { tooltip.style.left = '0'; tooltip.style.transform = 'none'; }
            else if (rect.right > window.innerWidth) { tooltip.style.left = 'auto'; tooltip.style.right = '0'; tooltip.style.transform = 'none'; }

            return tooltip;
        }

        function unpinAll() {
            if (pinnedChip) {
                var pt = pinnedChip.querySelector('.entity-tooltip-pinned');
                if (pt) pinnedChip.removeChild(pt);
                pinnedChip.classList.remove('entity-chip-pinned');
                pinnedChip = null;
            }
        }

        function bindChip(chip) {
            var hoverTooltip = null;

            chip.addEventListener('mouseenter', function() {
                if (pinnedChip === chip) return; // pinned, don't add hover tooltip
                if (hoverTooltip) return;
                var data;
                try { data = JSON.parse(chip.getAttribute('data-entity-json')); } catch(e) { return; }
                hoverTooltip = createTooltip(chip, data, false);
            });

            chip.addEventListener('mouseleave', function() {
                if (hoverTooltip) { chip.removeChild(hoverTooltip); hoverTooltip = null; }
            });

            chip.addEventListener('click', function(e) {
                e.stopPropagation();
                if (hoverTooltip) { chip.removeChild(hoverTooltip); hoverTooltip = null; }

                if (pinnedChip === chip) {
                    unpinAll(); // click again to unpin
                    return;
                }
                unpinAll();

                var data;
                try { data = JSON.parse(chip.getAttribute('data-entity-json')); } catch(e) { return; }
                createTooltip(chip, data, true);
                chip.classList.add('entity-chip-pinned');
                pinnedChip = chip;
            });
        }

        // Close pinned tooltip on click outside
        document.addEventListener('click', function(e) {
            if (pinnedChip && !pinnedChip.contains(e.target)) {
                unpinAll();
            }
        });

        document.querySelectorAll('.entity-chip').forEach(bindChip);

        // Re-bind after AJAX navigation
        var observer = new MutationObserver(function(mutations) {
            mutations.forEach(function(m) {
                m.addedNodes.forEach(function(node) {
                    if (node.nodeType === 1) {
                        if (node.classList && node.classList.contains('entity-chip')) bindChip(node);
                        node.querySelectorAll && node.querySelectorAll('.entity-chip').forEach(bindChip);
                    }
                });
            });
        });
        var content = document.querySelector('.markdown-content');
        if (content) observer.observe(content, { childList: true, subtree: true });
    }

    // ========================================================================
    // Search Preview Panel (slide-out)
    // ========================================================================
    function initSearchPreview() {
        bindSearchPreview();

        // Close on Escape
        document.addEventListener('keydown', function(e) {
            if (e.key === 'Escape') closeSearchPreview();
        });

        // Close on overlay click
        var overlay = document.getElementById('search-preview-overlay');
        if (overlay) {
            overlay.addEventListener('click', closeSearchPreview);
        }
    }

    function bindSearchPreview() {
        var cards = document.querySelectorAll('.search-result-card[data-href]');
        cards.forEach(function(card) {
            if (card.dataset.previewBound) return;
            card.dataset.previewBound = '1';
            card.style.cursor = 'pointer';

            card.addEventListener('click', function(e) {
                if (e.ctrlKey || e.metaKey || e.button === 1) {
                    window.open(card.dataset.href, '_blank');
                    return;
                }
                e.preventDefault();
                openSearchPreview(card);
            });

            card.addEventListener('dblclick', function() {
                window.location.href = card.dataset.href;
            });
        });
    }

    function openSearchPreview(card) {
        var href = card.dataset.href;
        var path = href.replace(/\.html$/, '').replace(/^\//, '') || 'index';
        var ajaxUrl = '/?path=' + encodeURIComponent(path) + '&ajax=1';

        // Mark active card
        document.querySelectorAll('.search-result-card.active').forEach(function(c) {
            c.classList.remove('active');
        });
        card.classList.add('active');

        var panel = document.getElementById('search-preview-panel');
        var overlay = document.getElementById('search-preview-overlay');
        var titleEl = document.getElementById('search-preview-title');
        var pathEl = document.getElementById('search-preview-path');
        var bodyEl = document.getElementById('search-preview-body');
        var openBtn = document.getElementById('search-preview-open');

        if (!panel) return;

        // Show panel with loading
        titleEl.textContent = 'Loading...';
        pathEl.textContent = '';
        bodyEl.innerHTML = '<div style="padding:40px;text-align:center;color:#666">Loading document...</div>';
        openBtn.href = href;
        panel.classList.add('visible');
        overlay.classList.add('visible');

        fetch(ajaxUrl)
            .then(function(res) {
                if (!res.ok) throw new Error('Not found');
                return res.json();
            })
            .then(function(data) {
                titleEl.textContent = data.title || 'Document';
                pathEl.textContent = data.meta && data.meta.path ? data.meta.path : path;
                bodyEl.innerHTML = data.content;
            })
            .catch(function(err) {
                titleEl.textContent = 'Error';
                bodyEl.innerHTML = '<div style="padding:40px;text-align:center;color:#f44">' + escapeHtml(err.message) + '</div>';
            });
    }

    // Global so onclick="closeSearchPreview()" works from HTML
    window.closeSearchPreview = function() {
        var panel = document.getElementById('search-preview-panel');
        var overlay = document.getElementById('search-preview-overlay');
        if (panel) panel.classList.remove('visible');
        if (overlay) overlay.classList.remove('visible');
        document.querySelectorAll('.search-result-card.active').forEach(function(c) {
            c.classList.remove('active');
        });
    };

    // ========================================================================
    // Utility Functions
    // ========================================================================
    function escapeHtml(text) {
        if (!text) return '';
        var div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

})();
