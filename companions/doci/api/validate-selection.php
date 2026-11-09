<?php
/**
 * Validate if selected text can become a thread link
 *
 * POST /api/validate-selection.php
 * Body:    {documentGuid, selectedText, occurrenceIndex?}
 * Returns: {success, data: {valid, reason, markdownFragment}, request_id}
 *          (errors use the standard {success: false, error, request_id}
 *          envelope shared with every other endpoint.)
 */

require_once __DIR__ . '/../src/config.php';
require_once __DIR__ . '/../lib/markdown.php';

header('Content-Type: application/json');

// Require authentication
$auth = require_api_auth();

require_once __DIR__ . '/../lib/response.php';

if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    doci_log('validate.error', ['error' => 'Method not allowed'], 'ERROR');
    json_method_not_allowed(['POST']);
}

try {
    $input = json_decode(file_get_contents('php://input'), true);

    if (!$input) {
        doci_log('validate.error', ['error' => 'Invalid JSON input'], 'ERROR');
        throw new Exception('Invalid JSON input');
    }

    $documentGuid = $input['documentGuid'] ?? null;
    $selectedText = $input['selectedText'] ?? '';
    $occurrenceIndex = (int) ($input['occurrenceIndex'] ?? 0);

    doci_log('validate.start', [
        'documentGuid' => $documentGuid,
        'selectedText' => substr($selectedText, 0, 50) . (strlen($selectedText) > 50 ? '...' : ''),
        'selectedLength' => strlen($selectedText),
        'occurrence' => $occurrenceIndex,
    ]);

    if (!$documentGuid) {
        doci_log('validate.error', ['error' => 'documentGuid is required'], 'ERROR');
        throw new Exception('documentGuid is required');
    }
    if (!$selectedText) {
        doci_log('validate.error', ['error' => 'selectedText is required'], 'ERROR');
        throw new Exception('selectedText is required');
    }

    // Get document from database
    $pdo = get_db();
    $stmt = $pdo->prepare("SELECT path, title FROM documents WHERE guid = ?");
    $stmt->execute([$documentGuid]);
    $doc = $stmt->fetch();

    if (!$doc) {
        doci_log('validate.error', ['error' => 'Document not found', 'guid' => $documentGuid], 'ERROR');
        throw new Exception('Document not found');
    }

    doci_log('validate.document', [
        'guid' => $documentGuid,
        'title' => $doc['title'],
        'path' => $doc['path']
    ]);

    // Read markdown content
    $filePath = __DIR__ . '/../files/' . $doc['path'];
    if (!file_exists($filePath)) {
        doci_log('validate.error', ['error' => 'File not found', 'path' => $filePath], 'ERROR');
        throw new Exception('Document file not found');
    }

    $markdown = file_get_contents($filePath);

    // Run the SAME matcher that thread.php will use at creation time. If the
    // selection can't be located now, the modal/comment step is grayed out so
    // the user doesn't waste effort writing a comment for a thread that won't
    // create.
    $match = find_thread_quote_in_source($markdown, $selectedText, $occurrenceIndex);

    if ($match === null) {
        $result = [
            'valid' => false,
            'reason' => 'Could not locate this selection in the source markdown. Try a shorter span or pick a single paragraph.',
            'markdownFragment' => '',
        ];
    } else {
        $check = check_thread_wrap_target($markdown, $match['start'], $match['raw']);
        if ($check['ok']) {
            $result = [
                'valid' => true,
                'reason' => '',
                'markdownFragment' => $match['raw'],
            ];
        } else {
            $result = [
                'valid' => false,
                'reason' => $check['reason'],
                'markdownFragment' => $match['raw'],
            ];
        }
    }

    doci_log('validate.result', [
        'valid' => $result['valid'],
        'reason' => $result['reason'] ?? null,
    ]);

    // Standard success envelope. Validate-specific payload lives under
    // `data` so generic clients can rely on the shared {success,
    // request_id} top level.
    json_success([
        'data' => $result,
        'request_id' => bin2hex(random_bytes(4)),
    ]);

} catch (Exception $e) {
    $code = http_response_code();
    json_exception($e, ($code >= 400 ? $code : 400), 'validate.exception');
}
