<?php
/**
 * Generate a bcrypt hash for DOCI_API_KEY_HASH.
 *
 * Usage:
 *   php scripts/hash-api-key.php                  # generates a random key + hash
 *   php scripts/hash-api-key.php "your-key-here"  # hashes the given key
 *
 * Pass the key (first arg) to clients via the X-API-Key header;
 * store the hash (this script's stdout) in DOCI_API_KEY_HASH.
 * Bcrypt is non-deterministic, so running this twice with the same
 * input produces different valid hashes -- either verifies the key.
 */

$key = $argv[1] ?? bin2hex(random_bytes(32));
if (strlen($key) < 16) {
    fwrite(STDERR, "API key must be at least 16 characters.\n");
    exit(1);
}

$hash = password_hash($key, PASSWORD_BCRYPT);

if (!isset($argv[1])) {
    echo "Generated API key (give to clients): $key\n";
    echo "\n";
}
echo "DOCI_API_KEY_HASH=$hash\n";
