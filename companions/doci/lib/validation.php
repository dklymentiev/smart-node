<?php
/**
 * DOCI - Validation Library
 *
 * Path sanitization, GUID validation, and other security checks.
 */

/**
 * Extensions DOCI treats as first-class documents.
 *
 * `.md` is the historical default and stays so: paths without any
 * recognised extension get `.md` auto-appended on create/get. `.html`
 * and `.svg` are additional first-class targets. Anything with a
 * different explicit extension (e.g. `.txt`, `.pdf`) is rejected at
 * the API layer so that we know exactly which content types the
 * render layer must handle.
 *
 * Override with env DOCI_ALLOWED_EXTENSIONS="md,html,svg,txt" if a
 * deployment adds new renderers.
 */
function doci_allowed_extensions(): array {
    static $cached = null;
    if ($cached !== null) {
        return $cached;
    }
    $env = getenv('DOCI_ALLOWED_EXTENSIONS');
    $raw = ($env !== false && $env !== '') ? $env : 'md,html,svg';
    $exts = array_filter(array_map(
        fn(string $s): string => strtolower(trim($s, ". \t\n\r\0")),
        explode(',', $raw)
    ));
    // Guard: only lowercase alphanumerics, 1-8 chars. Anything else is a
    // config bug; drop it silently rather than let an operator slip a
    // shell metacharacter into a filename via env.
    $cached = array_values(array_filter(
        $exts,
        fn(string $e): bool => preg_match('/^[a-z0-9]{1,8}$/', $e) === 1
    ));
    if (empty($cached)) {
        // Never permit an empty allowlist; fall back to `.md` so a bad
        // env var cannot turn every create into a 400.
        $cached = ['md'];
    }
    return $cached;
}

/**
 * Extract the lowercase file extension from a path, or null if none.
 *
 * @param string $path A DOCI path (may or may not have an extension).
 * @return string|null Extension without the dot, lowercased.
 */
function path_extension(string $path): ?string {
    $base = basename($path);
    $pos = strrpos($base, '.');
    if ($pos === false || $pos === 0) {
        return null;
    }
    return strtolower(substr($base, $pos + 1));
}

/**
 * Is the given path's extension in the DOCI allowlist?
 *
 * A path with no recognisable extension returns false -- call sites
 * decide whether to auto-append `.md` (create/get) or reject (rename).
 *
 * @param string $path
 * @return bool
 */
function path_extension_allowed(string $path): bool {
    $ext = path_extension($path);
    if ($ext === null) {
        return false;
    }
    return in_array($ext, doci_allowed_extensions(), true);
}

/**
 * Sanitize file path - prevent path traversal attacks
 *
 * Removes null bytes, normalizes separators, and filters unsafe segments.
 * Only allows alphanumeric characters, underscores, hyphens, and dots.
 *
 * @param string $path Raw path from user input
 * @return string Sanitized path (may be empty if all segments invalid)
 */
function sanitize_path(string $path): string {
    // Remove null bytes
    $path = str_replace("\0", '', $path);
    // Normalize path separators
    $path = str_replace('\\', '/', $path);
    // Remove leading/trailing slashes
    $path = trim($path, '/');

    $segments = explode('/', $path);
    $sanitized = [];

    foreach ($segments as $segment) {
        // Skip empty, current dir, and parent dir references.
        if ($segment === '' || $segment === '.' || $segment === '..') {
            continue;
        }
        // Reject any segment that starts with a dot. files/.git,
        // files/.data, files/.ai-pending.json and similar dot-prefixed
        // paths are runtime/internal state -- never targets the
        // request layer should be able to address.
        if ($segment[0] === '.') {
            continue;
        }
        // Only allow safe characters
        if (!preg_match('/^[a-zA-Z0-9_\-\.]+$/', $segment)) {
            continue;
        }
        $sanitized[] = $segment;
    }

    return implode('/', $sanitized);
}

/**
 * Validate UUID/GUID format (RFC 4122)
 *
 * @param string $guid GUID to validate
 * @return bool True if valid UUID v4 format
 */
function is_valid_guid(string $guid): bool {
    return preg_match('/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i', $guid) === 1;
}

/**
 * Validate that a path is within allowed base directory
 *
 * Uses realpath() to resolve symlinks and prevent escaping.
 *
 * @param string $path Path to validate
 * @param string $basePath Allowed base directory
 * @return bool True if path is within base directory
 */
function validate_path_within(string $path, string $basePath): bool {
    $realBasePath = realpath($basePath);
    if ($realBasePath === false) {
        return false;
    }

    // For new files, check parent directory
    $realPath = realpath($path);
    if ($realPath === false) {
        $realPath = realpath(dirname($path));
        if ($realPath === false) {
            // Parent doesn't exist - reject (cannot verify without resolved path)
            return false;
        }
    }

    return str_starts_with($realPath, $realBasePath);
}

/**
 * Parse PostgreSQL array format to PHP array
 *
 * @param string|null $str PostgreSQL array string like '{a,b,c}'
 * @return array PHP array
 */
function parse_pg_array(?string $str): array {
    if (empty($str) || $str === '{}') {
        return [];
    }
    $str = trim($str, '{}');
    if (empty($str)) {
        return [];
    }
    return array_map(fn($s) => trim($s, '"'), explode(',', $str));
}

/**
 * Convert PHP array to PostgreSQL array format
 *
 * Tags must be pre-validated before calling this function.
 *
 * @param array $arr PHP array
 * @return string PostgreSQL array string
 */
function to_pg_array(array $arr): string {
    return '{' . implode(',', $arr) . '}';
}

/**
 * Validate and sanitize tags array
 *
 * Only allows alphanumeric, hyphen, underscore (max 50 chars each).
 *
 * @param string|array $tags Tags as comma-separated string or array
 * @return array Validated tags array
 */
function validate_tags($tags): array {
    if (is_string($tags)) {
        $tags = array_map('trim', explode(',', $tags));
    }
    if (!is_array($tags)) {
        return [];
    }
    return array_filter($tags, fn($t) => preg_match('/^[a-zA-Z0-9_-]{1,50}$/', $t));
}
