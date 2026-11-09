<?php
/**
 * Thread Reply API
 *
 * POST /api/thread-reply.php { threadGuid, message }
 *
 * Continues conversation in an AI thread.
 * Appends user message and AI response to the thread file.
 */

require_once __DIR__ . '/../src/config.php';
require_once __DIR__ . '/../src/documents.php';
require_once __DIR__ . '/../src/ai.php';

header('Content-Type: application/json');

// Require authentication
$auth = require_api_auth();
$authUser = $auth['user'];

// CSRF protection
require_csrf_token();

$method = $_SERVER['REQUEST_METHOD'];

if ($method !== 'POST') {
    http_response_code(405);
    echo json_encode(['success' => false, 'error' => 'Method not allowed']);
    exit;
}

try {
    $input = json_decode(file_get_contents('php://input'), true);

    if (!$input) {
        throw new Exception('Invalid JSON input');
    }

    $threadGuid = $input['threadGuid'] ?? null;
    $message = trim($input['message'] ?? '');

    if (!$threadGuid) {
        throw new Exception('threadGuid is required');
    }

    if (!$message) {
        throw new Exception('message is required');
    }

    // Get thread document
    $thread = get_document_by_guid($threadGuid);
    if (!$thread) {
        throw new Exception('Thread not found');
    }

    if ($thread['doc_type'] !== 'thread') {
        throw new Exception('Document is not a thread');
    }

    $username = get_current_username() ?? 'anonymous';

    doci_log('thread-reply.start', [
        'thread_guid' => $threadGuid,
        'message_length' => strlen($message),
        'username' => $username
    ]);

    // Get thread file path
    $threadFilePath = __DIR__ . '/../files/' . $thread['path'];
    if (!file_exists($threadFilePath)) {
        throw new Exception('Thread file not found');
    }

    // Read current thread content (before adding new message)
    $threadContent = file_get_contents($threadFilePath);

    // Append user message to thread
    $timestamp = date('Y-m-d H:i');
    $userSection = "\n\n---\n\n";
    $userSection .= "**{$username}** ({$timestamp})\n\n";
    $userSection .= $message . "\n";

    // Add loading placeholder
    $placeholder = "\n\n---\n\n<!-- ai-pending:sonnet -->\n";

    file_put_contents($threadFilePath, $userSection . $placeholder, FILE_APPEND);

    doci_log('thread-reply.user_message_appended', [
        'thread_guid' => $threadGuid
    ]);

    // Get original document for context
    $parentDoc = get_document_by_guid($thread['parent_guid']);
    $originalGuid = null;

    if ($parentDoc) {
        if ($parentDoc['doc_type'] === 'version') {
            $originalGuid = $parentDoc['original_guid'];
        } else {
            $originalGuid = $parentDoc['guid'];
        }
    }

    // Save context for async AI processing (like initial thread creation)
    $threadDir = dirname($threadFilePath);
    $contextPath = $threadDir . '/.ai-reply-pending.json';
    $aiContext = [
        'threadGuid' => $threadGuid,
        'originalGuid' => $originalGuid,
        'threadContent' => $threadContent,
        'message' => $message,
        'username' => $username,
        'model' => 'sonnet',
        'created' => date('Y-m-d H:i:s')
    ];
    file_put_contents($contextPath, json_encode($aiContext));

    doci_log('thread-reply.context_saved', [
        'thread_guid' => $threadGuid,
        'context_path' => $contextPath
    ]);

    // Return immediately - AI will be processed via AJAX
    echo json_encode([
        'success' => true,
        'pending' => true
    ]);

} catch (Exception $e) {
    $code = http_response_code();
    json_exception($e, ($code >= 400 ? $code : 400), 'thread-reply.error');
}
