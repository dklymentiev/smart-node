<?php
/**
 * DOCI - Document Functions
 *
 * GUID-based document lookup and hierarchy management.
 */

require_once __DIR__ . '/config.php';
require_once dirname(__DIR__) . '/lib/validation.php';

/**
 * Get document by GUID
 */
function get_document_by_guid(string $guid): ?array {
    if (!is_valid_guid($guid)) {
        return null;
    }

    $db = get_db();
    $stmt = $db->prepare('
        SELECT guid, path, parent_guid, original_guid, doc_type, title, summary, tags,
               created_by, created_at, updated_at, updated_by, access
        FROM documents
        WHERE guid = :guid AND deleted_at IS NULL
    ');
    $stmt->execute(['guid' => $guid]);
    return $stmt->fetch() ?: null;
}

/**
 * Get document by path
 */
function get_document_by_path(string $path): ?array {
    $db = get_db();
    $stmt = $db->prepare('
        SELECT guid, path, parent_guid, original_guid, doc_type, title, summary, tags,
               created_by, created_at, updated_at, updated_by, access
        FROM documents
        WHERE path = :path AND deleted_at IS NULL
    ');
    $stmt->execute(['path' => $path]);
    return $stmt->fetch() ?: null;
}

/**
 * Get document hierarchy (breadcrumbs from root to document)
 */
function get_document_hierarchy(string $guid): array {
    if (!is_valid_guid($guid)) {
        return [];
    }

    $db = get_db();
    $stmt = $db->prepare('SELECT * FROM get_document_hierarchy(:guid)');
    $stmt->execute(['guid' => $guid]);
    return $stmt->fetchAll();
}

/**
 * Get child documents/threads
 */
function get_document_children(string $guid): array {
    if (!is_valid_guid($guid)) {
        return [];
    }

    $db = get_db();
    $stmt = $db->prepare('
        SELECT guid, path, doc_type, title, summary, created_by, created_at
        FROM documents
        WHERE parent_guid = :guid AND deleted_at IS NULL
        ORDER BY created_at DESC
    ');
    $stmt->execute(['guid' => $guid]);
    return $stmt->fetchAll();
}

/**
 * Get threads with quotes for a document (for highlighting in content)
 */
function get_document_threads_with_quotes(string $guid): array {
    if (!is_valid_guid($guid)) {
        return [];
    }

    $db = get_db();
    $stmt = $db->prepare('
        SELECT guid, title, quote
        FROM documents
        WHERE parent_guid = :guid
          AND doc_type = \'thread\'
          AND quote IS NOT NULL
          AND quote != \'\'
          AND deleted_at IS NULL
        ORDER BY created_at ASC
    ');
    $stmt->execute(['guid' => $guid]);
    return $stmt->fetchAll();
}

/**
 * Create or update document in registry
 */
function register_document(string $path, array $data): ?string {
    $db = get_db();

    // Normalize tags: accept array or comma-separated string
    if (isset($data['tags']) && is_string($data['tags'])) {
        $data['tags'] = array_values(array_filter(
            array_map('trim', explode(',', $data['tags'])),
            fn(string $t): bool => $t !== ''
        ));
    }

    // Check if document exists
    $existing = get_document_by_path($path);

    if ($existing) {
        // Update existing
        $stmt = $db->prepare('
            UPDATE documents SET
                title = :title,
                summary = :summary,
                tags = :tags,
                updated_at = NOW(),
                updated_by = :updated_by
            WHERE guid = :guid
            RETURNING guid
        ');
        $stmt->execute([
            'guid' => $existing['guid'],
            'title' => $data['title'] ?? $existing['title'],
            'summary' => $data['summary'] ?? $existing['summary'],
            'tags' => isset($data['tags']) ? '{' . implode(',', $data['tags']) . '}' : null,
            'updated_by' => $data['updated_by'] ?? null
        ]);
        return $existing['guid'];
    } else {
        // Create new
        $stmt = $db->prepare('
            INSERT INTO documents (path, parent_guid, doc_type, title, summary, tags, created_by, updated_by)
            VALUES (:path, :parent_guid, :doc_type, :title, :summary, :tags, :created_by, :created_by)
            RETURNING guid
        ');
        $stmt->execute([
            'path' => $path,
            'parent_guid' => $data['parent_guid'] ?? null,
            'doc_type' => $data['doc_type'] ?? 'document',
            'title' => $data['title'] ?? basename($path, '.md'),
            'summary' => $data['summary'] ?? null,
            'tags' => isset($data['tags']) ? '{' . implode(',', $data['tags']) . '}' : null,
            'created_by' => $data['created_by'] ?? 'system'
        ]);
        $result = $stmt->fetch();
        return $result ? $result['guid'] : null;
    }
}

/**
 * Create a thread for a document
 */
function create_thread(string $parentGuid, string $title, string $createdBy, ?string $summary = null): ?array {
    $parent = get_document_by_guid($parentGuid);
    if (!$parent) {
        return null;
    }

    $db = get_db();

    // Generate thread path
    $threadPath = '.threads/' . $parentGuid . '/' . date('Y-m-d_His') . '.md';

    $stmt = $db->prepare('
        INSERT INTO documents (path, parent_guid, doc_type, title, summary, created_by)
        VALUES (:path, :parent_guid, \'thread\', :title, :summary, :created_by)
        RETURNING guid, path
    ');
    $stmt->execute([
        'path' => $threadPath,
        'parent_guid' => $parentGuid,
        'title' => $title,
        'summary' => $summary,
        'created_by' => $createdBy
    ]);

    return $stmt->fetch() ?: null;
}

/**
 * Render breadcrumbs from document hierarchy
 */
function render_hierarchy_breadcrumbs(array $hierarchy): string {
    if (empty($hierarchy)) {
        return '<a href="/">Home</a>';
    }

    $breadcrumbs = ['<a href="/">Home</a>'];

    foreach ($hierarchy as $i => $doc) {
        $isLast = ($i === count($hierarchy) - 1);
        $title = htmlspecialchars($doc['title'] ?? 'Untitled');

        if ($isLast) {
            $breadcrumbs[] = '<span class="current">' . $title . '</span>';
        } else {
            $url = '/' . htmlspecialchars($doc['guid']);
            $breadcrumbs[] = '<a href="' . $url . '">' . $title . '</a>';
        }
    }

    return implode(' <span class="separator">/</span> ', $breadcrumbs);
}

/**
 * Get AI context for a document (summary + hierarchy)
 */
function get_document_context(string $guid): ?array {
    $doc = get_document_by_guid($guid);
    if (!$doc) {
        return null;
    }

    $hierarchy = get_document_hierarchy($guid);
    $children = get_document_children($guid);

    return [
        'document' => $doc,
        'hierarchy' => $hierarchy,
        'children' => $children,
        'context_summary' => build_context_summary($doc, $hierarchy)
    ];
}

/**
 * Create a version (snapshot) of a document or thread
 * Used when creating threads - the link goes into the version, not the original
 *
 * @param string $originalGuid GUID of the original document or thread
 * @param string $createdBy Username creating the version
 * @return array|null Version info [guid, path] or null on failure
 */
function create_version(string $originalGuid, string $createdBy): ?array {
    doci_log('version.fn.start', [
        'original_guid' => $originalGuid,
        'created_by' => $createdBy
    ]);

    $original = get_document_by_guid($originalGuid);
    if (!$original) {
        doci_log('version.fn.error', ['error' => 'Original document not found'], 'ERROR');
        return null;
    }

    // Allow versions of documents and threads, not versions
    if (!in_array($original['doc_type'], ['document', 'thread'])) {
        doci_log('version.fn.error', [
            'error' => 'Invalid doc_type for versioning',
            'doc_type' => $original['doc_type']
        ], 'ERROR');
        return null;
    }

    $db = get_db();
    $filesDir = FILES_PATH;

    // Read original document content
    $originalPath = $filesDir . '/' . $original['path'];
    if (!file_exists($originalPath)) {
        doci_log('version.fn.error', [
            'error' => 'Original file not found',
            'path' => $originalPath
        ], 'ERROR');
        return null;
    }
    $originalContent = file_get_contents($originalPath);

    doci_log('version.fn.read_original', [
        'path' => $original['path'],
        'size' => strlen($originalContent)
    ]);

    // Start transaction
    $db->beginTransaction();

    try {
        // Insert version record (with empty path first to get GUID)
        $stmt = $db->prepare("
            INSERT INTO documents (path, original_guid, doc_type, title, created_by)
            VALUES ('', :original_guid, 'version', :title, :created_by)
            RETURNING guid
        ");
        $stmt->execute([
            'original_guid' => $originalGuid,
            'title' => $original['title'] . ' (version)',
            'created_by' => $createdBy
        ]);
        $result = $stmt->fetch();
        $versionGuid = $result['guid'];

        doci_log('version.fn.db_insert', ['version_guid' => $versionGuid]);

        // Create version directory structure: .data/versions/{original-guid}/{version-guid}/
        $versionDir = $filesDir . '/.data/versions/' . $originalGuid . '/' . $versionGuid;
        $versionPath = '.data/versions/' . $originalGuid . '/' . $versionGuid . '/index.md';

        if (!mkdir($versionDir, 0775, true)) {
            doci_log('version.fn.error', ['error' => 'Failed to create directory', 'dir' => $versionDir], 'ERROR');
            throw new Exception('Failed to create version directory');
        }

        doci_log('version.fn.mkdir', ['dir' => $versionDir]);

        // Update path in database
        $stmt = $db->prepare("UPDATE documents SET path = :path WHERE guid = :guid");
        $stmt->execute(['path' => $versionPath, 'guid' => $versionGuid]);

        // Write version content (copy of original)
        $versionFullPath = $filesDir . '/' . $versionPath;
        if (file_put_contents($versionFullPath, $originalContent) === false) {
            doci_log('version.fn.error', ['error' => 'Failed to write file', 'path' => $versionFullPath], 'ERROR');
            throw new Exception('Failed to write version file');
        }

        doci_log('version.fn.file_write', [
            'path' => $versionPath,
            'size' => strlen($originalContent)
        ]);

        $db->commit();

        doci_log('version.fn.success', [
            'version_guid' => $versionGuid,
            'original_guid' => $originalGuid,
            'path' => $versionPath
        ]);

        return [
            'guid' => $versionGuid,
            'path' => $versionPath,
            'original_guid' => $originalGuid
        ];

    } catch (Exception $e) {
        $db->rollback();
        doci_log('version.fn.rollback', ['error' => $e->getMessage()], 'ERROR');
        // Clean up directory if created
        if (isset($versionDir) && is_dir($versionDir)) {
            @rmdir($versionDir);
        }
        return null;
    }
}

/**
 * Get all versions of a document
 *
 * @param string $originalGuid GUID of the original document
 * @return array List of versions with guid, created_by, created_at
 */
function get_document_versions(string $originalGuid): array {
    if (!is_valid_guid($originalGuid)) {
        return [];
    }

    $db = get_db();
    $stmt = $db->prepare('
        SELECT guid, path, title, created_by, created_at
        FROM documents
        WHERE original_guid = :original_guid
          AND doc_type = \'version\'
          AND deleted_at IS NULL
        ORDER BY created_at ASC
    ');
    $stmt->execute(['original_guid' => $originalGuid]);
    return $stmt->fetchAll();
}

/**
 * Get the original document for a version
 *
 * @param string $versionGuid GUID of the version document
 * @return array|null Original document or null
 */
function get_original_document(string $versionGuid): ?array {
    $version = get_document_by_guid($versionGuid);
    if (!$version || !$version['original_guid']) {
        return null;
    }
    return get_document_by_guid($version['original_guid']);
}

/**
 * Build text summary for AI context
 */
function build_context_summary(array $doc, array $hierarchy): string {
    $parts = [];

    // Location
    $path = array_map(fn($h) => $h['title'], $hierarchy);
    if (!empty($path)) {
        $parts[] = "📍 Location: " . implode(' → ', $path);
    }

    // Document info
    $parts[] = "📄 Title: " . ($doc['title'] ?? 'Untitled');
    $parts[] = "📝 Type: " . ($doc['doc_type'] ?? 'document');

    if (!empty($doc['summary'])) {
        $parts[] = "📋 Summary: " . $doc['summary'];
    }

    if (!empty($doc['tags'])) {
        $tags = is_array($doc['tags']) ? $doc['tags'] : explode(',', trim($doc['tags'], '{}'));
        $parts[] = "🏷️ Tags: " . implode(', ', $tags);
    }

    $parts[] = "👤 Created by: " . ($doc['created_by'] ?? 'unknown');
    $parts[] = "📅 Updated: " . ($doc['updated_at'] ?? $doc['created_at'] ?? 'unknown');

    return implode("\n", $parts);
}
