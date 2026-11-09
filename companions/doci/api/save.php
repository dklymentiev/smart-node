<?php
/**
 * DOCI - Save Endpoint
 *
 * Saves markdown file with local git commit + push.
 * POST /api/save.php
 */

// Log errors but don't display to users
error_reporting(E_ALL);
ini_set('display_errors', '0');
ini_set('log_errors', '1');

require_once __DIR__ . '/../src/config.php';
require_once __DIR__ . '/../lib/git.php';
require_once __DIR__ . '/../lib/validation.php';
require_once __DIR__ . '/../lib/response.php';

// Only accept POST requests
if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    doci_log('save.error', ['error' => 'Method not allowed'], 'ERROR');
    json_method_not_allowed(['POST']);
}

// CSRF protection
require_csrf_token();

// Get user info - require authentication
$username = get_current_username();

if (!$username || $username === 'unknown') {
    doci_log('save.error', ['error' => 'Not authenticated'], 'ERROR');
    json_error_exit('Not authenticated', 401);
}

// Get POST data
$input = json_decode(file_get_contents('php://input'), true);

if (!$input || !isset($input['path']) || !isset($input['content'])) {
    doci_log('save.error', ['error' => 'Missing path or content'], 'ERROR');
    json_error_exit('Missing path or content');
}

$requestedPath = $input['path'];
$content = $input['content'];

$path = sanitize_path($requestedPath);

doci_log('save.start', [
    'requested_path' => $requestedPath,
    'sanitized_path' => $path,
    'content_length' => strlen($content),
    'user' => $username
]);

if (empty($path)) {
    doci_log('save.error', ['error' => 'Invalid path'], 'ERROR');
    json_error_exit('Invalid path');
}

// Ensure .md extension
if (!str_ends_with($path, '.md')) {
    $path .= '.md';
}

// Check file exists
$localPath = FILES_PATH . '/' . $path;
if (!file_exists($localPath)) {
    doci_log('save.error', ['error' => 'File not found', 'path' => $path], 'ERROR');
    json_not_found('File not found');
}

// Normalise internal path links to GUID URLs before persisting, so
// links survive future renames and folder moves.
require_once __DIR__ . '/../lib/markdown.php';
$content = rewrite_md_links_to_guids($content);

// Write file
if (file_put_contents($localPath, $content) === false) {
    doci_log('save.error', ['error' => 'Failed to write file', 'path' => $path], 'ERROR');
    json_server_error('Failed to write file');
}

doci_log('save.file_written', ['path' => $path, 'size' => strlen($content)]);

// Commit and push to git
$commitResult = git_commit_and_push($path, "Update $path", $username);

if (isset($commitResult['error'])) {
    doci_log('save.git_error', [
        'path' => $path,
        'error' => $commitResult['error']
    ], 'WARN');
} else {
    doci_log('save.git_commit', [
        'path' => $path,
        'hash' => $commitResult['hash'] ?? ''
    ]);
}

// Update document metadata in database
try {
    $pdo = get_db();
    $stmt = $pdo->prepare("
        UPDATE documents
        SET updated_at = NOW(), updated_by = ?
        WHERE path = ?
    ");
    $stmt->execute([$username, $path]);

    doci_log('save.db_update', ['path' => $path, 'user' => $username]);
} catch (Exception $e) {
    // Log but don't fail - file and git already succeeded
    doci_log('save.db_error', ['error' => $e->getMessage()], 'WARN');
}

doci_log('save.success', [
    'path' => $path,
    'size' => strlen($content),
    'hash' => $commitResult['hash'] ?? '',
    'user' => $username
]);

json_success([
    'path' => $path,
    'user' => $username,
    'hash' => $commitResult['hash'] ?? null,
    'message' => 'Saved and committed locally'
]);
