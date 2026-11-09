<?php
/**
 * Index all documents and folders into database
 * Run: php index-documents.php
 * Or via web: /index-documents.php?run=1
 *
 * Indexes:
 * 1. Folders (doc_type = 'folder') with parent_guid hierarchy
 * 2. Files (doc_type = 'document') with parent_guid pointing to folder
 */

require_once __DIR__ . '/../src/config.php';

// Only allow CLI
if (php_sapi_name() !== 'cli') {
    echo "CLI only\n";
    exit(1);
}

$filesDir = __DIR__ . '/../files';
$pdo = get_db();

// Stats
$foldersIndexed = 0;
$filesIndexed = 0;
$skipped = 0;
$errors = 0;

// Cache: path -> guid
$pathToGuid = [];

// Load existing documents
$stmt = $pdo->query("SELECT guid, path, doc_type FROM documents WHERE deleted_at IS NULL");
while ($row = $stmt->fetch(PDO::FETCH_ASSOC)) {
    $pathToGuid[$row['path']] = $row['guid'];
}
echo "Existing in DB: " . count($pathToGuid) . "\n\n";

// Prepare statements
$insertFolderStmt = $pdo->prepare("
    INSERT INTO documents (path, parent_guid, doc_type, title, created_by)
    VALUES (:path, :parent_guid, 'folder', :title, 'system')
    ON CONFLICT (path) DO UPDATE SET parent_guid = :parent_guid
    RETURNING guid
");

$insertFileStmt = $pdo->prepare("
    INSERT INTO documents (path, parent_guid, doc_type, title, created_by, created_at)
    VALUES (:path, :parent_guid, 'document', :title, 'system', :created_at)
    ON CONFLICT (path) DO UPDATE SET parent_guid = :parent_guid
    RETURNING guid
");

/**
 * Get or create folder GUID, building hierarchy
 */
function ensureFolderExists($folderPath, $pdo, &$pathToGuid, $insertStmt, &$indexed) {
    global $filesDir;

    // Already exists?
    if (isset($pathToGuid[$folderPath])) {
        return $pathToGuid[$folderPath];
    }

    // Get parent folder
    $parentPath = dirname($folderPath);
    $parentGuid = null;

    if ($parentPath !== '.' && $parentPath !== '') {
        $parentGuid = ensureFolderExists($parentPath, $pdo, $pathToGuid, $insertStmt, $indexed);
    }

    // Create folder
    $title = ucwords(str_replace(['-', '_'], ' ', basename($folderPath)));

    try {
        $insertStmt->execute([
            ':path' => $folderPath,
            ':parent_guid' => $parentGuid,
            ':title' => $title
        ]);
        $result = $insertStmt->fetch();
        $guid = $result['guid'];
        $pathToGuid[$folderPath] = $guid;
        $indexed++;
        echo "[F] $folderPath\n";
        return $guid;
    } catch (Exception $e) {
        echo "[!] Folder error: $folderPath - " . $e->getMessage() . "\n";
        return null;
    }
}

echo "=== Indexing folders ===\n";

// Find all directories
$dirs = new RecursiveIteratorIterator(
    new RecursiveDirectoryIterator($filesDir, RecursiveDirectoryIterator::SKIP_DOTS),
    RecursiveIteratorIterator::SELF_FIRST
);

foreach ($dirs as $dir) {
    if (!$dir->isDir()) continue;

    $relativePath = str_replace($filesDir . '/', '', $dir->getPathname());

    // Skip hidden directories
    if (strpos($relativePath, '.') === 0) continue;
    if (strpos($relativePath, '/.') !== false) continue;

    ensureFolderExists($relativePath, $pdo, $pathToGuid, $insertFolderStmt, $foldersIndexed);
}

echo "\n=== Indexing files ===\n";

// Find all files
$files = new RecursiveIteratorIterator(
    new RecursiveDirectoryIterator($filesDir, RecursiveDirectoryIterator::SKIP_DOTS)
);

foreach ($files as $file) {
    if (!$file->isFile()) continue;

    $ext = strtolower($file->getExtension());
    if (!in_array($ext, ['md', 'html'])) continue;

    $fullPath = $file->getPathname();
    $relativePath = str_replace($filesDir . '/', '', $fullPath);

    // Skip hidden files/directories
    if (strpos($relativePath, '.') === 0) continue;
    if (strpos($relativePath, '/.') !== false) continue;

    // Get parent folder guid
    $parentPath = dirname($relativePath);
    $parentGuid = null;
    if ($parentPath !== '.' && $parentPath !== '') {
        $parentGuid = $pathToGuid[$parentPath] ?? null;
    }

    // Extract title
    $title = extractTitle($fullPath, $ext);
    if (!$title) {
        $title = pathinfo($file->getFilename(), PATHINFO_FILENAME);
        $title = ucwords(str_replace(['-', '_'], ' ', $title));
    }

    $createdAt = date('Y-m-d H:i:s', $file->getMTime());

    try {
        $insertFileStmt->execute([
            ':path' => $relativePath,
            ':parent_guid' => $parentGuid,
            ':title' => mb_substr($title, 0, 200),
            ':created_at' => $createdAt
        ]);

        $result = $insertFileStmt->fetch();
        if ($result) {
            $pathToGuid[$relativePath] = $result['guid'];
            $filesIndexed++;
            echo "[D] $relativePath\n";
        }
    } catch (Exception $e) {
        $errors++;
        echo "[!] Error: $relativePath - " . $e->getMessage() . "\n";
    }
}

echo "\n";
echo "=== Indexing complete ===\n";
echo "Folders indexed: $foldersIndexed\n";
echo "Files indexed: $filesIndexed\n";
echo "Errors: $errors\n";

// Show totals
$stmt = $pdo->query("SELECT doc_type, count(*) as cnt FROM documents WHERE deleted_at IS NULL GROUP BY doc_type ORDER BY cnt DESC");
echo "\nTotals by type:\n";
while ($row = $stmt->fetch(PDO::FETCH_ASSOC)) {
    echo "  {$row['doc_type']}: {$row['cnt']}\n";
}

function extractTitle($filePath, $ext) {
    $content = @file_get_contents($filePath, false, null, 0, 2000);
    if (!$content) return null;

    if ($ext === 'md') {
        if (preg_match('/^#\s+(.+)$/m', $content, $matches)) {
            return trim($matches[1]);
        }
    } else if ($ext === 'html') {
        if (preg_match('/<title>([^<]+)<\/title>/i', $content, $matches)) {
            return trim($matches[1]);
        }
        if (preg_match('/<h1[^>]*>([^<]+)<\/h1>/i', $content, $matches)) {
            return trim($matches[1]);
        }
    }

    return null;
}
