<?php
/**
 * Document Registration API
 *
 * POST /api/register.php - Register a new document in the database
 *
 * Body: {path, title?}
 * Returns: {success, guid, path, title}
 *
 * created_by is derived from the authenticated session/API-key
 * user; any value in the request body is ignored (attribution
 * cannot be spoofed by API clients).
 *
 * This endpoint should be called when creating new documents
 * (e.g., from scriber, imports, or other tools).
 */

require_once __DIR__ . '/../src/config.php';
require_once __DIR__ . '/../src/documents.php';

header('Content-Type: application/json');

// Require authentication
$auth = require_api_auth();
require_csrf_token();

$method = $_SERVER['REQUEST_METHOD'];

doci_log('register.request', [
    'method' => $method
]);

try {
    if ($method !== 'POST') {
        doci_log('register.error', ['error' => 'Method not allowed'], 'ERROR');
        throw new Exception('Method not allowed');
    }

    $input = json_decode(file_get_contents('php://input'), true);

    if (!$input) {
        doci_log('register.error', ['error' => 'Invalid JSON input'], 'ERROR');
        throw new Exception('Invalid JSON input');
    }

    $path = $input['path'] ?? null;
    $title = $input['title'] ?? null;
    // Attribution is server-side only. Ignore any created_by in body.
    $createdBy = get_current_username() ?? 'system';

    doci_log('register.start', [
        'path' => $path,
        'title' => $title,
        'created_by' => $createdBy
    ]);

    if (!$path) {
        doci_log('register.error', ['error' => 'path is required'], 'ERROR');
        throw new Exception('path is required');
    }

    // Normalize path - ensure .md extension
    if (!str_ends_with($path, '.md')) {
        $path .= '.md';
    }

    // Check if already registered
    $existing = get_document_by_path($path);
    if ($existing) {
        doci_log('register.exists', [
            'path' => $path,
            'guid' => $existing['guid']
        ]);

        echo json_encode([
            'success' => true,
            'guid' => $existing['guid'],
            'path' => $existing['path'],
            'title' => $existing['title'],
            'already_exists' => true
        ]);
        exit;
    }

    // Check if file exists
    $filePath = __DIR__ . '/../files/' . $path;
    if (!file_exists($filePath)) {
        doci_log('register.error', ['error' => 'File not found', 'path' => $filePath], 'ERROR');
        throw new Exception('File not found: ' . $path);
    }

    // Extract title from content if not provided
    if (!$title) {
        $content = file_get_contents($filePath);
        if (preg_match('/^#\s+(.+)$/m', $content, $matches)) {
            $title = trim($matches[1]);
        } else {
            // Use filename without extension
            $title = basename($path, '.md');
            $title = ucwords(str_replace(['-', '_'], ' ', $title));
        }
    }

    // Register the document
    $guid = register_document($path, [
        'title' => $title,
        'doc_type' => 'document',
        'created_by' => $createdBy
    ]);

    if (!$guid) {
        doci_log('register.error', ['error' => 'Failed to register document'], 'ERROR');
        throw new Exception('Failed to register document');
    }

    doci_log('register.success', [
        'path' => $path,
        'guid' => $guid,
        'title' => $title
    ]);

    echo json_encode([
        'success' => true,
        'guid' => $guid,
        'path' => $path,
        'title' => $title,
        'already_exists' => false
    ]);

} catch (Exception $e) {
    $code = http_response_code();
    json_exception($e, ($code >= 400 ? $code : 400), 'register.exception');
}
