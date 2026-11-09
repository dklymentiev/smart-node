<?php
/**
 * DOCI -- Key (canonical) documents API.
 *
 * Endpoints:
 *   GET    /api/key-document.php                     -> all rows
 *   GET    /api/key-document.php?domain=marketing    -> filtered
 *   GET    /api/key-document.php?guid=<g>            -> all rows for one guid
 *   POST   /api/key-document.php  (JSON body)         -> create
 *       { guid, domain, label?, description?, update_trigger? }
 *   DELETE /api/key-document.php?id=N                -> remove
 */

require_once __DIR__ . '/../src/config.php';
require_once __DIR__ . '/../lib/key-documents.php';
require_once __DIR__ . '/../lib/response.php';

header('Content-Type: application/json; charset=UTF-8');

require_api_auth();

$method = $_SERVER['REQUEST_METHOD'];

try {
    if ($method === 'GET') {
        $guid = $_GET['guid'] ?? null;
        $domain = $_GET['domain'] ?? null;
        if ($guid) {
            echo json_encode(['success' => true, 'items' => get_key_document_records($guid)]);
        } else {
            echo json_encode(['success' => true, 'items' => list_key_documents($domain)]);
        }
        exit;
    }

    if ($method === 'POST') {
        require_csrf_token();
        $raw = file_get_contents('php://input');
        $data = json_decode($raw, true);
        if (!is_array($data)) {
            http_response_code(400);
            echo json_encode(['success' => false, 'error' => 'Invalid JSON input']);
            exit;
        }
        $result = add_key_document($data);
        if (isset($result['error'])) {
            http_response_code(400);
            echo json_encode(['success' => false, 'error' => $result['error']]);
            exit;
        }
        doci_log('key_document.add', ['id' => $result['id'], 'guid' => $data['guid'] ?? null, 'domain' => $data['domain'] ?? null]);
        echo json_encode(['success' => true, 'id' => $result['id']]);
        exit;
    }

    if ($method === 'DELETE') {
        require_csrf_token();
        $id = isset($_GET['id']) ? (int)$_GET['id'] : 0;
        if ($id <= 0) {
            http_response_code(400);
            echo json_encode(['success' => false, 'error' => 'id is required']);
            exit;
        }
        $result = remove_key_document($id);
        if (isset($result['error'])) {
            http_response_code(500);
            echo json_encode(['success' => false, 'error' => $result['error']]);
            exit;
        }
        doci_log('key_document.remove', ['id' => $id, 'removed' => $result['removed']]);
        echo json_encode(['success' => true, 'removed' => $result['removed']]);
        exit;
    }

    http_response_code(405);
    echo json_encode(['success' => false, 'error' => 'Method not allowed']);
} catch (Throwable $e) {
    json_exception($e, 500, 'key_document.error');
}
