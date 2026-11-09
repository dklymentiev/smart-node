<?php
/**
 * Versions API
 *
 * GET /api/versions.php?document={guid} - List versions of a document
 *
 * Returns all versions (snapshots) created for a document,
 * along with their threads.
 */

require_once __DIR__ . '/../src/config.php';
require_once __DIR__ . '/../src/documents.php';

header('Content-Type: application/json');

// Require authentication
$auth = require_api_auth();

$method = $_SERVER['REQUEST_METHOD'];

doci_log('versions.request', [
    'method' => $method,
    'document' => $_GET['document'] ?? null,
    'user' => $auth['user']
]);

try {
    if ($method === 'POST') {
        // Manual snapshot: freeze a document's current state into a version row.
        // The thread-creation flow auto-creates versions; this endpoint exists for
        // explicit "save a checkpoint" gestures (UI buttons, agents, seed scripts).
        require_csrf_token();
        $input = json_decode(file_get_contents('php://input'), true) ?: [];
        $documentGuid = $input['document_guid'] ?? $input['documentGuid'] ?? null;
        if (!$documentGuid) {
            throw new Exception('document_guid is required');
        }
        $createdBy = get_current_username() ?? 'system';
        $version = create_version($documentGuid, $createdBy);
        if (!$version) {
            http_response_code(400);
            echo json_encode(['success' => false, 'error' => 'Failed to create version']);
            exit;
        }
        // Commit the new version file to git so git log shows the snapshot.
        if (function_exists('git_commit_and_push')) {
            require_once __DIR__ . '/../lib/git.php';
            git_commit_and_push($version['path'], 'Snapshot ' . $version['original_guid'] . ' -> version ' . $version['guid'], $createdBy);
        }
        echo json_encode([
            'success' => true,
            'version' => $version,
        ]);
        exit;
    }

    if ($method !== 'GET') {
        doci_log('versions.error', ['error' => 'Method not allowed'], 'ERROR');
        throw new Exception('Method not allowed');
    }

    $documentGuid = $_GET['document'] ?? null;

    if (!$documentGuid) {
        doci_log('versions.error', ['error' => 'document parameter is required'], 'ERROR');
        throw new Exception('document parameter is required');
    }

    $pdo = get_db();

    // Get the document to check its type
    $stmt = $pdo->prepare("SELECT guid, path, title, doc_type, original_guid FROM documents WHERE guid = ?");
    $stmt->execute([$documentGuid]);
    $doc = $stmt->fetch();

    if (!$doc) {
        doci_log('versions.error', ['error' => 'Document not found', 'guid' => $documentGuid], 'ERROR');
        throw new Exception('Document not found');
    }

    doci_log('versions.document', [
        'guid' => $documentGuid,
        'doc_type' => $doc['doc_type'],
        'title' => $doc['title']
    ]);

    // If this is a version, get the original document's guid
    $originalGuid = $documentGuid;
    if ($doc['doc_type'] === 'version') {
        $originalGuid = $doc['original_guid'];
        doci_log('versions.resolve_original', ['original_guid' => $originalGuid]);
    } elseif ($doc['doc_type'] !== 'document') {
        doci_log('versions.error', [
            'error' => 'Cannot get versions for this document type',
            'doc_type' => $doc['doc_type']
        ], 'ERROR');
        throw new Exception('Cannot get versions for this document type');
    }

    // Get all versions for the original document
    $stmt = $pdo->prepare("
        SELECT
            v.guid,
            v.path,
            v.title,
            v.created_by,
            v.created_at,
            (SELECT COUNT(*) FROM documents t WHERE t.parent_guid = v.guid AND t.doc_type = 'thread' AND t.deleted_at IS NULL) as thread_count
        FROM documents v
        WHERE v.original_guid = :original_guid
          AND v.doc_type = 'version'
          AND v.deleted_at IS NULL
        ORDER BY v.created_at ASC
    ");
    $stmt->execute(['original_guid' => $originalGuid]);
    $versions = $stmt->fetchAll();

    // Get original document info
    $stmt = $pdo->prepare("SELECT guid, path, title, created_by, created_at FROM documents WHERE guid = ?");
    $stmt->execute([$originalGuid]);
    $original = $stmt->fetch();

    doci_log('versions.success', [
        'original_guid' => $originalGuid,
        'version_count' => count($versions)
    ]);

    echo json_encode([
        'success' => true,
        'original' => [
            'guid' => $original['guid'],
            'path' => $original['path'],
            'title' => $original['title'],
            'created_by' => $original['created_by'],
            'created_at' => $original['created_at']
        ],
        'versions' => array_map(function($v) {
            return [
                'guid' => $v['guid'],
                'path' => $v['path'],
                'created_by' => $v['created_by'],
                'created_at' => $v['created_at'],
                'thread_count' => (int)$v['thread_count'],
                'url' => '/' . $v['guid']
            ];
        }, $versions),
        'total_versions' => count($versions)
    ]);

} catch (Exception $e) {
    doci_log('versions.exception', ['error' => $e->getMessage()], 'ERROR');
    http_response_code(400);
    echo json_encode([
        'success' => false,
        'error' => $e->getMessage()
    ]);
}
