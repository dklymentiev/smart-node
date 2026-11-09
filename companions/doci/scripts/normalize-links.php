<?php
/**
 * One-shot migration: rewrite every markdown file under files/ so that
 * internal links use the stable GUID form (/<guid>) instead of the
 * path form (/foo/bar). Run from the container entrypoint after the
 * documents table has been seeded.
 *
 * Idempotent. Files that already use GUID links are unchanged.
 * Files that change get written back in place; the next save/commit
 * cycle will pick them up.
 *
 * Run: php scripts/normalize-links.php
 */

if (php_sapi_name() !== 'cli') {
    echo "CLI only\n";
    exit(1);
}

require_once __DIR__ . '/../src/config.php';
require_once __DIR__ . '/../lib/markdown.php';

$filesDir = realpath(__DIR__ . '/../files');
if ($filesDir === false) {
    echo "files/ not found\n";
    exit(1);
}

$changed = 0;
$scanned = 0;

$iterator = new RecursiveIteratorIterator(
    new RecursiveDirectoryIterator($filesDir, RecursiveDirectoryIterator::SKIP_DOTS),
    RecursiveIteratorIterator::LEAVES_ONLY
);

foreach ($iterator as $file) {
    if (!$file->isFile()) continue;
    if (substr($file->getFilename(), -3) !== '.md') continue;

    // Skip files under .git/ and .data/ (internal storage)
    $rel = substr($file->getPathname(), strlen($filesDir) + 1);
    $relUnix = str_replace('\\', '/', $rel);
    if (strpos($relUnix, '.git/') === 0 || strpos($relUnix, '.data/') === 0) continue;

    $scanned++;
    $before = file_get_contents($file->getPathname());
    if ($before === false) continue;
    $after = rewrite_md_links_to_guids($before);
    if ($after !== $before) {
        file_put_contents($file->getPathname(), $after);
        $changed++;
        echo "  rewrote $relUnix\n";
    }
}

echo "\nScanned: $scanned\nRewritten: $changed\n";
