<?php
/**
 * Thread API
 *
 * POST /api/thread.php - Create new thread (creates version first)
 * GET /api/thread.php?document={guid} - List threads for document
 *
 * Versioned Threads Architecture:
 * When creating a thread, we first create a VERSION (snapshot) of the document,
 * then insert the thread link into the VERSION - not the original.
 * This allows multiple users to create threads on overlapping text selections.
 */

require_once __DIR__ . '/../src/config.php';
require_once __DIR__ . '/../src/documents.php';
require_once __DIR__ . '/../src/ai.php';
require_once __DIR__ . '/../lib/markdown.php';

header('Content-Type: application/json');

// Require authentication
$auth = require_api_auth();
$authUser = $auth['user'];

// CSRF protection for state-changing requests
if ($_SERVER['REQUEST_METHOD'] !== 'GET') {
    require_csrf_token();
}

$method = $_SERVER['REQUEST_METHOD'];

try {
    $pdo = get_db();

    if ($method === 'POST') {
        // Create new thread
        $input = json_decode(file_get_contents('php://input'), true);

        if (!$input) {
            doci_log('thread.create.error', ['error' => 'Invalid JSON input'], 'ERROR');
            throw new Exception('Invalid JSON input');
        }

        $documentGuid = $input['documentGuid'] ?? null;
        $quote = $input['quote'] ?? '';
        $comment = $input['comment'] ?? '';
        $username = get_current_username() ?? 'anonymous';

        doci_log('thread.create.start', [
            'documentGuid' => $documentGuid,
            'quote_length' => strlen($quote),
            'comment_length' => strlen($comment),
            'has_context' => !empty($input['contextBefore']) || !empty($input['contextAfter'])
        ]);

        if (!$documentGuid) {
            doci_log('thread.create.error', ['error' => 'documentGuid is required'], 'ERROR');
            throw new Exception('documentGuid is required');
        }
        if (!$comment) {
            doci_log('thread.create.error', ['error' => 'comment is required'], 'ERROR');
            throw new Exception('comment is required');
        }

        // Get document - could be original document or already a version
        $stmt = $pdo->prepare("SELECT guid, path, title, doc_type, original_guid FROM documents WHERE guid = ?");
        $stmt->execute([$documentGuid]);
        $sourceDoc = $stmt->fetch();

        if (!$sourceDoc) {
            doci_log('thread.create.error', ['error' => 'Document not found', 'guid' => $documentGuid], 'ERROR');
            throw new Exception('Document not found');
        }

        doci_log('thread.create.source', [
            'source_guid' => $documentGuid,
            'source_type' => $sourceDoc['doc_type'],
            'source_title' => $sourceDoc['title']
        ]);

        // Determine the original document and whether we need to create a version
        $originalGuid = $documentGuid;
        $versionGuid = null;
        $versionPath = null;

        if ($sourceDoc['doc_type'] === 'document' || $sourceDoc['doc_type'] === 'thread') {
            // Source is document or thread - create a version
            doci_log('version.create.start', [
                'original_guid' => $documentGuid,
                'original_type' => $sourceDoc['doc_type']
            ]);

            $version = create_version($documentGuid, $username);
            if (!$version) {
                doci_log('version.create.error', ['error' => 'Failed to create version'], 'ERROR');
                throw new Exception('Failed to create version');
            }
            $versionGuid = $version['guid'];
            $versionPath = $version['path'];

            doci_log('version.create.success', [
                'version_guid' => $versionGuid,
                'version_path' => $versionPath,
                'original_guid' => $documentGuid
            ]);
        } elseif ($sourceDoc['doc_type'] === 'version') {
            // Source is already a version - use it directly
            $versionGuid = $documentGuid;
            $versionPath = $sourceDoc['path'];
            $originalGuid = $sourceDoc['original_guid'];

            doci_log('version.reuse', [
                'version_guid' => $versionGuid,
                'original_guid' => $originalGuid
            ]);
        } else {
            doci_log('thread.create.error', [
                'error' => 'Cannot create thread on this document type',
                'doc_type' => $sourceDoc['doc_type']
            ], 'ERROR');
            throw new Exception('Cannot create thread on this document type');
        }

        // Generate thread title from comment (first line or first N chars)
        $title = strtok($comment, "\n");
        if (strlen($title) > 100) {
            $title = substr($title, 0, 97) . '...';
        }

        // Get original document for thread content reference
        $stmt = $pdo->prepare("SELECT guid, path, title FROM documents WHERE guid = ?");
        $stmt->execute([$originalGuid]);
        $originalDoc = $stmt->fetch();

        // Start transaction for thread creation
        $pdo->beginTransaction();
        doci_log('thread.create.transaction.start', ['version_guid' => $versionGuid]);

        try {
            // Insert thread into database (parent is the VERSION, not original)
            $stmt = $pdo->prepare("
                INSERT INTO documents (path, parent_guid, doc_type, title, created_by, quote)
                VALUES ('', ?, 'thread', ?, ?, ?)
                RETURNING guid
            ");
            $stmt->execute([$versionGuid, $title, $username, $quote ?: null]);
            $result = $stmt->fetch();
            $threadGuid = $result['guid'];

            doci_log('thread.create.db_insert', [
                'thread_guid' => $threadGuid,
                'parent_guid' => $versionGuid,
                'title' => $title
            ]);

            // Create thread directory and path
            $threadDir = __DIR__ . '/../files/.data/threads/' . $versionGuid . '/' . $threadGuid;
            $threadPath = '.data/threads/' . $versionGuid . '/' . $threadGuid . '/index.md';

            // Create thread directory
            if (!mkdir($threadDir, 0775, true)) {
                doci_log('thread.create.error', ['error' => 'Failed to create thread directory', 'dir' => $threadDir], 'ERROR');
                throw new Exception('Failed to create thread directory');
            }

            doci_log('thread.create.mkdir', ['dir' => $threadDir]);

            // Update path in database
            $stmt = $pdo->prepare("UPDATE documents SET path = ? WHERE guid = ?");
            $stmt->execute([$threadPath, $threadGuid]);

            // Find raw markdown quote from version document. The browser sends
            // plain selection text (no `**`, no backticks); we walk the source
            // token-by-token so markdown markers between tokens are absorbed.
            $markdownQuote = $quote; // fallback to browser text
            $versionFilePath = __DIR__ . '/../files/' . $versionPath;
            $versionContent = '';
            $pos = false;
            $matchLength = strlen($quote);
            $occurrenceIndex = (int) ($input['occurrenceIndex'] ?? 0);

            if ($quote && file_exists($versionFilePath)) {
                $versionContent = file_get_contents($versionFilePath);

                doci_log('thread.link.start', [
                    'version_path' => $versionPath,
                    'quote' => substr($quote, 0, 50) . (strlen($quote) > 50 ? '...' : ''),
                    'occurrence' => $occurrenceIndex,
                ]);

                $match = find_thread_quote_in_source($versionContent, $quote, $occurrenceIndex);
                if ($match !== null) {
                    $check = check_thread_wrap_target($versionContent, $match['start'], $match['raw']);
                    if (!$check['ok']) {
                        doci_log('thread.link.refused', [
                            'reason' => $check['reason'],
                            'quote' => substr($quote, 0, 60),
                        ], 'WARN');
                        throw new Exception($check['reason']);
                    }
                    $pos = $match['start'];
                    $matchLength = $match['length'];
                    $markdownQuote = $match['raw'];
                    doci_log('thread.link.matched', [
                        'position' => $pos,
                        'length' => $matchLength,
                        'occurrence' => $occurrenceIndex,
                    ]);
                }
            }

            // Create thread content with raw markdown quote
            $content = "**Thread** | " . $username . " | " . date('Y-m-d H:i') . "\n";
            $content .= "**Document:** [" . $originalDoc['title'] . "](/" . $originalGuid . ")";
            $content .= " | **Version:** [" . $username . "'s version](/" . $versionGuid . ")\n\n";

            $content .= "**" . $username . "** — _" . $comment . "_\n\n---\n";

            // Write thread file
            $fullPath = __DIR__ . '/../files/' . $threadPath;
            if (file_put_contents($fullPath, $content) === false) {
                doci_log('thread.create.error', ['error' => 'Failed to write thread file', 'path' => $fullPath], 'ERROR');
                throw new Exception('Failed to write thread file');
            }

            doci_log('thread.create.file_write', [
                'path' => $threadPath,
                'size' => strlen($content)
            ]);

            // Wrap selected span with inline HTML comments. No newlines around the
            // markers -- they sit literally adjacent to the selected characters so
            // they don't introduce block boundaries inside lists, tables, or
            // headers. The rendered surface is built at parse time by
            // ParsedownExtended (span vs block-fragment based on whether the body
            // crosses a paragraph break).
            if ($quote && $pos !== false) {
                $replacement = '<!-- @thread:' . $threadGuid . ' -->' . $markdownQuote . '<!-- @/thread:' . $threadGuid . ' -->';

                $newContent = substr_replace($versionContent, $replacement, $pos, $matchLength);
                $writeResult = file_put_contents($versionFilePath, $newContent);
                if ($writeResult === false) {
                    doci_log('thread.link.write_error', ['path' => $versionFilePath], 'WARN');
                } else {
                    doci_log('thread.link.success', [
                        'position' => $pos,
                        'replacement_length' => strlen($replacement),
                    ]);
                }

                // Also wrap the live source document where the user actually made
                // the selection -- the chip should appear in the surface the user
                // was clicking on, not only in the snapshot. Skip when the source
                // was already a version (wrap stays in that version only).
                if ($sourceDoc['doc_type'] === 'document' || $sourceDoc['doc_type'] === 'thread') {
                    $liveFilePath = __DIR__ . '/../files/' . $sourceDoc['path'];
                    if (file_exists($liveFilePath)) {
                        $liveContent = file_get_contents($liveFilePath);
                        $liveMatch = find_thread_quote_in_source($liveContent, $quote, $occurrenceIndex);
                        if ($liveMatch !== null) {
                            $liveReplacement = '<!-- @thread:' . $threadGuid . ' -->' . $liveMatch['raw'] . '<!-- @/thread:' . $threadGuid . ' -->';
                            $newLiveContent = substr_replace($liveContent, $liveReplacement, $liveMatch['start'], $liveMatch['length']);
                            if (file_put_contents($liveFilePath, $newLiveContent) !== false) {
                                doci_log('thread.link.live_propagated', [
                                    'source_path' => $sourceDoc['path'],
                                    'position' => $liveMatch['start'],
                                ]);
                            } else {
                                doci_log('thread.link.live_write_error', ['path' => $liveFilePath], 'WARN');
                            }
                        } else {
                            doci_log('thread.link.live_not_found', [
                                'source_path' => $sourceDoc['path'],
                            ], 'WARN');
                        }
                    }
                }
            } elseif ($quote && $pos === false) {
                doci_log('thread.link.not_found', [
                    'quote' => substr($quote, 0, 100),
                    'occurrence' => $occurrenceIndex,
                ], 'WARN');
                throw new Exception('Could not locate the selected text in the document source -- the thread was not created. This usually happens on very long multi-section selections; try a shorter span.');
            }

            $pdo->commit();
            doci_log('thread.create.success', [
                'thread_guid' => $threadGuid,
                'version_guid' => $versionGuid,
                'original_guid' => $originalGuid
            ]);

            // Handle AI response request - add placeholder and save context for AJAX
            $aiRequested = !empty($input['requestAiResponse']);
            $aiModel = $input['aiModel'] ?? 'sonnet';

            if ($aiRequested) {
                // Add loading placeholder to thread file
                $placeholder = "\n\n---\n\n<!-- ai-pending:" . $aiModel . " -->\n";
                file_put_contents($fullPath, $placeholder, FILE_APPEND);

                // Save AI context for AJAX processing
                $aiContext = [
                    'threadGuid' => $threadGuid,
                    'originalGuid' => $originalGuid,
                    'quote' => $quote,
                    'comment' => $comment,
                    'model' => $aiModel,
                    'username' => $username,
                    'created' => date('Y-m-d H:i:s')
                ];
                $contextPath = dirname($fullPath) . '/.ai-pending.json';
                file_put_contents($contextPath, json_encode($aiContext));

                doci_log('thread.ai.placeholder', [
                    'thread_guid' => $threadGuid,
                    'model' => $aiModel,
                    'context_path' => $contextPath
                ]);
            }

        } catch (Exception $e) {
            $pdo->rollback();
            doci_log('thread.create.rollback', ['error' => $e->getMessage()], 'ERROR');
            // Clean up thread file if it was created
            if (isset($fullPath) && file_exists($fullPath)) {
                @unlink($fullPath);
            }
            throw $e;
        }

        $response = [
            'success' => true,
            'thread' => [
                'guid' => $threadGuid,
                'path' => $threadPath,
                'title' => $title,
                'url' => '/' . $threadGuid
            ],
            'version' => [
                'guid' => $versionGuid,
                'path' => $versionPath,
                'url' => '/' . $versionGuid
            ],
            'original' => [
                'guid' => $originalGuid
            ]
        ];

        if ($aiRequested) {
            $response['ai'] = [
                'pending' => true,
                'model' => $aiModel
            ];
        }

        echo json_encode($response);

    } elseif ($method === 'GET') {
        // List threads for document
        $documentGuid = $_GET['document'] ?? null;

        doci_log('thread.list.start', ['document_guid' => $documentGuid]);

        if (!$documentGuid) {
            throw new Exception('document parameter is required');
        }

        // Check if this is an original or a version
        $stmt = $pdo->prepare("SELECT doc_type FROM documents WHERE guid = ?");
        $stmt->execute([$documentGuid]);
        $doc = $stmt->fetch();

        if (!$doc) {
            doci_log('thread.list.error', ['error' => 'Document not found'], 'ERROR');
            throw new Exception('Document not found');
        }

        if ($doc['doc_type'] === 'document') {
            // Original document - get threads from all versions
            $stmt = $pdo->prepare("
                SELECT t.guid, t.path, t.title, t.created_by, t.created_at, v.guid as version_guid
                FROM documents t
                JOIN documents v ON t.parent_guid = v.guid
                WHERE v.original_guid = ? AND t.doc_type = 'thread' AND t.deleted_at IS NULL
                ORDER BY t.created_at DESC
            ");
            $stmt->execute([$documentGuid]);
        } else {
            // Version or thread - get threads for this document only
            $stmt = $pdo->prepare("
                SELECT guid, path, title, created_by, created_at
                FROM documents
                WHERE parent_guid = ? AND doc_type = 'thread' AND deleted_at IS NULL
                ORDER BY created_at DESC
            ");
            $stmt->execute([$documentGuid]);
        }

        $threads = $stmt->fetchAll();

        doci_log('thread.list.success', [
            'document_guid' => $documentGuid,
            'doc_type' => $doc['doc_type'],
            'thread_count' => count($threads)
        ]);

        echo json_encode([
            'success' => true,
            'threads' => $threads
        ]);

    } else {
        throw new Exception('Method not allowed');
    }

} catch (Exception $e) {
    $code = http_response_code();
    json_exception($e, ($code >= 400 ? $code : 400), 'thread.api.error');
}
