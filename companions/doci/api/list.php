<?php
/**
 * Documents list API
 *
 * GET /api/list.php                       - every live document, for building a tree
 * GET /api/list.php?q=text                - filter by path or title, case-insensitive
 * GET /api/list.php?recent=20             - the N most recently updated, newest first
 * GET /api/list.php?folder=example-project/ - every document under a folder prefix
 *
 * Why this file exists: Studio has called GET /api/list.php since its Documents
 * section shipped (smart-node-office 335a715), and DOCI has never had the file.
 * The page rendered "No documents yet" against a full store, so the failure read
 * as emptiness rather than as an error and nobody chased it. Confirmed here on
 * 2026-08-30 against a store holding 8146 live documents.
 *
 * The response shape is not invented here. Studio already parses it:
 *     app.js:2230  _docsAll = Array.isArray(data.documents) ? data.documents : []
 *     app.js:2260  _docsAll.find((d) => d.guid && norm(d.path) === norm(path))
 * so every row must carry at least `guid` and `path`. The remaining columns are
 * what a tree view needs in order to render without a second call per row.
 *
 * The `folder` param was added for MCP clients that need to list a subtree
 * without knowing the file names inside it. It applies a prefix match
 * on `path`, so it returns every descendant of the folder recursively.
 *
 * Authentication: Traefik/Authum (Remote-User header) OR API key (X-API-Key).
 */

require_once __DIR__ . '/../src/config.php';
require_once __DIR__ . '/../lib/response.php';
require_once __DIR__ . '/../lib/validation.php';

header('Content-Type: application/json');

$auth = require_api_auth();

if ($_SERVER['REQUEST_METHOD'] !== 'GET') {
    json_error_exit('Only GET is supported', 405);
}

// `recent` is a count, not a flag: Studio sends the number of rows it wants to
// show. Clamped because the value arrives from the query string, and an
// unbounded LIMIT here turns any stray request into a full-store read.
$recent = isset($_GET['recent']) ? (int) $_GET['recent'] : 0;
if ($recent < 0)   { $recent = 0; }
if ($recent > 500) { $recent = 500; }

$q = isset($_GET['q']) ? trim((string) $_GET['q']) : '';
if (strlen($q) > 200) { $q = substr($q, 0, 200); }

$folder = isset($_GET['folder']) ? trim((string) $_GET['folder']) : '';
if (strlen($folder) > 500) { $folder = substr($folder, 0, 500); }
$folderPrefix = '';
if ($folder !== '') {
    // Normalise into a canonical prefix, then bail rather than silently
    // return the whole store if the caller passed something that cannot
    // point at a real subtree. Path traversal segments (`..`), dot-prefixed
    // segments (`.git`) and anything outside `[A-Za-z0-9_.-]` are dropped
    // by sanitize_path, which is the same rule the write paths already use.
    $normalised = sanitize_path($folder);
    if ($normalised === '') {
        json_error_exit('Invalid folder', 400);
    }
    $folderPrefix = $normalised . '/';
}

doci_log('list.api.request', [
    'user' => $auth['user'],
    'q' => $q,
    'recent' => $recent,
    'folder' => $folderPrefix,
]);

try {
    $pdo = get_db();

    $sql = 'SELECT guid, path, title, doc_type, summary, created_by, created_at, updated_at
              FROM documents
             WHERE deleted_at IS NULL';
    $params = [];

    if ($q !== '') {
        // ILIKE keeps the match case-insensitive on Postgres without lowering an
        // indexed column for every row in the store.
        $sql .= ' AND (path ILIKE :q OR title ILIKE :q)';
        $params[':q'] = '%' . $q . '%';
    }

    if ($folderPrefix !== '') {
        // LIKE with escaped metacharacters: sanitize_path already rules out
        // `%` and `_` from a legal path, but keep the ESCAPE clause explicit
        // so a later relaxation of sanitize_path does not turn into a query
        // that returns unrelated rows. Postgres LIKE is case-sensitive, which
        // matches how paths are stored.
        $safe = addcslashes($folderPrefix, '\\%_');
        $sql .= " AND path LIKE :folder_prefix ESCAPE '\\'";
        $params[':folder_prefix'] = $safe . '%';
    }

    // Recency order only when a count was asked for. Without one the caller is
    // building a tree, and a tree wants a stable alphabetical order.
    $sql .= $recent > 0
        ? ' ORDER BY updated_at DESC NULLS LAST LIMIT ' . $recent
        : ' ORDER BY path ASC';

    $stmt = $pdo->prepare($sql);
    $stmt->execute($params);
    $documents = $stmt->fetchAll(PDO::FETCH_ASSOC);

    json_success(['documents' => $documents, 'count' => count($documents)]);
} catch (Throwable $e) {
    // The detail goes to the log and never to the caller: a PDO message names
    // the schema, and this endpoint answers a browser.
    doci_log('list.api.error', ['class' => get_class($e), 'message' => $e->getMessage()], 'ERROR');
    json_error('Internal error', 500);
}
