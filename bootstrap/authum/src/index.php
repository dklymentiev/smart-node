<?php
declare(strict_types=1);

/**
 * Authum - Lightweight Authentication Service
 *
 * Single-file auth service with WebAuthn/Passkey support.
 * Designed for Traefik forward auth integration.
 *
 * @license MIT
 * @version 1.0.0
 */

// Configuration from environment variables
$config = [
    'app_name' => getenv('AUTHUM_RP_NAME') ?: 'Authum',
    'domain' => getenv('AUTHUM_DOMAIN') ?: '.example.com',
    'rp_id' => getenv('AUTHUM_RP_ID') ?: 'example.com',
    'auth_url' => getenv('AUTHUM_AUTH_URL') ?: 'https://auth.example.com',
    // Where a login with no ?rd= lands, and what the post-auth button opens.
    // Deployers pass AUTHUM_DEFAULT_REDIRECT (personalize-node.sh does) to set this
    // per node; the fallback below only applies when that env var is unset.
    'default_redirect' => getenv('AUTHUM_DEFAULT_REDIRECT') ?: 'https://studio.example.com/',
    // A session ends after this long WITHOUT a request (sliding: every request
    // renews it), or this long after sign-in whatever happens.
    'session_lifetime' => (int)(getenv('AUTHUM_SESSION_LIFETIME') ?: 43200),
    'session_max_age' => (int)(getenv('AUTHUM_SESSION_MAX_AGE') ?: 2592000),
    // Sessions live in the data volume. PHP's default is /tmp inside the
    // container with a 24-minute garbage collector: a page left alone for half
    // an hour, or any restart of the container, signed everyone out while the
    // browser cookie still claimed 12 hours, and every page failed silently
    'session_dir' => '/app/data/sessions',
    'credentials_file' => '/app/auth/credentials.json',
    'users_file' => '/app/auth/users.json',
    'ratelimit_dir' => '/app/data/ratelimit',
    'max_login_attempts' => (int)(getenv('AUTHUM_MAX_LOGIN_ATTEMPTS') ?: 5),
    'lockout_duration' => (int)(getenv('AUTHUM_LOCKOUT_DURATION') ?: 300),
    // Password login. ON by default and it must stay that way: a node whose
    // operator has not registered a passkey has no other way in, and flipping
    // the default would lock out every existing install.
    //
    // Set AUTHUM_PASSWORD_LOGIN=off once at least two passkeys are registered.
    // Two, not one -- with a single credential, losing that device leaves no
    // way back in except shell access to the node.
    //
    // BREAK-GLASS, deliberately not automated: whoever has root on the node can
    // set this back to on and recreate the container. That is the recovery
    // path; there is no email reset and no recovery code, on purpose.
    'password_login' => strtolower(getenv('AUTHUM_PASSWORD_LOGIN') ?: 'on') !== 'off',
];

// Generate CSP nonce for inline scripts
$cspNonce = base64_encode(random_bytes(16));

// Security headers
header('X-Content-Type-Options: nosniff');
header('X-Frame-Options: DENY');
header('Referrer-Policy: strict-origin-when-cross-origin');
// The UI kit lives with the Studio (the default redirect), under /welcome/ui/.
$kitParts = parse_url($config['default_redirect']);
$kitOrigin = ($kitParts['scheme'] ?? 'https') . '://' . ($kitParts['host'] ?? '') . (isset($kitParts['port']) ? ':' . $kitParts['port'] : '');
$kitUrl = $kitOrigin . '/welcome/ui/';
header("Content-Security-Policy: default-src 'self'; script-src 'self' 'nonce-{$cspNonce}'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com {$kitOrigin}; font-src https://fonts.gstatic.com");

if (!is_dir($config['session_dir'])) {
    mkdir($config['session_dir'], 0700, true);
}
session_save_path($config['session_dir']);
// The collector removes a file untouched for longer than the idle limit; every
// request touches it, so only an idle session is collected, never a used one.
ini_set('session.gc_maxlifetime', (string)$config['session_lifetime']);

// The cookie lasts as long as the absolute limit. It is set once at sign-in
// and never renewed, because the renewal would have to travel back through
// Traefik's forward auth; the idle limit is enforced here, on the server, in
// isAuthenticated().
session_set_cookie_params([
    'lifetime' => $config['session_max_age'],
    'path' => '/',
    'domain' => $config['domain'],
    // https everywhere, except a local node served on plain http (its auth URL
    // says so): a Secure cookie is never sent back over http.
    'secure' => !str_starts_with($config['auth_url'], 'http://'),
    'httponly' => true,
    'samesite' => 'Lax'
]);
session_name('authum_session');
session_start();

// ============================================================================
// CBOR Decoder (minimal implementation for WebAuthn)
// ============================================================================

/**
 * Decode CBOR data.
 *
 * @param string $data Raw CBOR bytes
 * @return mixed Decoded value or null on error
 */
function cbor_decode(string $data): mixed {
    $offset = 0;
    $length = strlen($data);
    try {
        return cbor_decode_item($data, $offset, $length);
    } catch (Exception $e) {
        return null;
    }
}

/**
 * Decode a single CBOR item.
 *
 * @throws Exception on malformed data
 */
function cbor_decode_item(string $data, int &$offset, int $length): mixed {
    if ($offset >= $length) {
        throw new Exception('CBOR: unexpected end of data');
    }

    $initial = ord($data[$offset++]);
    $major = $initial >> 5;
    $additional = $initial & 0x1f;

    $value = cbor_decode_length($data, $offset, $length, $additional);

    switch ($major) {
        case 0: // Unsigned integer
            return $value;
        case 1: // Negative integer
            return -1 - $value;
        case 2: // Byte string
            if ($offset + $value > $length) {
                throw new Exception('CBOR: byte string exceeds data length');
            }
            $result = substr($data, $offset, $value);
            $offset += $value;
            return $result;
        case 3: // Text string
            if ($offset + $value > $length) {
                throw new Exception('CBOR: text string exceeds data length');
            }
            $result = substr($data, $offset, $value);
            $offset += $value;
            return $result;
        case 4: // Array
            $result = [];
            for ($i = 0; $i < $value; $i++) {
                $result[] = cbor_decode_item($data, $offset, $length);
            }
            return $result;
        case 5: // Map
            $result = [];
            for ($i = 0; $i < $value; $i++) {
                $key = cbor_decode_item($data, $offset, $length);
                $result[$key] = cbor_decode_item($data, $offset, $length);
            }
            return $result;
        case 6: // Tagged item
            return cbor_decode_item($data, $offset, $length);
        case 7: // Primitives (false, true, null, undefined, float)
            if ($additional === 20) return false;
            if ($additional === 21) return true;
            if ($additional === 22) return null;
            return $value;
        default:
            return null;
    }
}

/**
 * Decode CBOR length/value field.
 *
 * @throws Exception on malformed data
 */
function cbor_decode_length(string $data, int &$offset, int $length, int $additional): int {
    if ($additional < 24) {
        return $additional;
    }
    if ($additional === 24) {
        if ($offset >= $length) {
            throw new Exception('CBOR: unexpected end reading length');
        }
        return ord($data[$offset++]);
    }
    if ($additional === 25) {
        if ($offset + 2 > $length) {
            throw new Exception('CBOR: unexpected end reading 2-byte length');
        }
        $result = unpack('n', substr($data, $offset, 2))[1];
        $offset += 2;
        return $result;
    }
    if ($additional === 26) {
        if ($offset + 4 > $length) {
            throw new Exception('CBOR: unexpected end reading 4-byte length');
        }
        $result = unpack('N', substr($data, $offset, 4))[1];
        $offset += 4;
        return $result;
    }
    return 0;
}

// ============================================================================
// WebAuthn Crypto Functions
// ============================================================================

/**
 * Extract public key from WebAuthn attestation object.
 *
 * Parses the CBOR-encoded attestation object and extracts the
 * credential public key in PEM format.
 *
 * @param string $attestationObject Raw attestation object bytes
 * @return array|null Array with 'type', 'alg', 'pem' keys or null on failure
 */
function extractPublicKeyFromAttestation(string $attestationObject): ?array {
    // Decode CBOR attestation object
    $attestation = cbor_decode($attestationObject);
    if (!$attestation || !isset($attestation['authData'])) {
        return null;
    }

    $authData = $attestation['authData'];
    $authDataLen = strlen($authData);

    // AuthData structure:
    // - 32 bytes: rpIdHash
    // - 1 byte: flags
    // - 4 bytes: signCount
    // - variable: attestedCredentialData (if AT flag set)

    if ($authDataLen < 37) {
        return null;
    }

    $flags = ord($authData[32]);
    $hasAttestedCredential = ($flags & 0x40) !== 0;

    if (!$hasAttestedCredential || $authDataLen < 55) {
        return null;
    }

    // Skip to credentialPublicKey
    // 32 (rpIdHash) + 1 (flags) + 4 (signCount) + 16 (aaguid) + 2 (credIdLen)
    $credIdLen = unpack('n', substr($authData, 53, 2))[1];

    // Skip credentialId to get to public key
    $pubKeyOffset = 55 + $credIdLen;
    if ($pubKeyOffset >= $authDataLen) {
        return null;
    }
    $pubKeyData = substr($authData, $pubKeyOffset);

    // Decode COSE_Key
    $coseKey = cbor_decode($pubKeyData);
    if (!$coseKey) {
        return null;
    }

    // COSE key parameters:
    // 1 = kty (key type): 2 = EC2, 3 = RSA
    // 3 = alg: -7 = ES256, -257 = RS256
    // -1 = crv (for EC): 1 = P-256
    // -2 = x coordinate
    // -3 = y coordinate

    $kty = $coseKey[1] ?? null;

    if ($kty === 2) { // EC2 (ECDSA)
        $x = $coseKey[-2] ?? null;
        $y = $coseKey[-3] ?? null;

        if (!$x || !$y || strlen($x) !== 32 || strlen($y) !== 32) {
            return null;
        }

        // Create uncompressed EC point: 0x04 || x || y
        $point = "\x04" . $x . $y;

        // Create DER-encoded SubjectPublicKeyInfo for P-256
        // OID for P-256: 1.2.840.10045.3.1.7
        $der = "\x30\x59" .                                    // SEQUENCE (89 bytes)
               "\x30\x13" .                                    // SEQUENCE (AlgorithmIdentifier)
               "\x06\x07\x2a\x86\x48\xce\x3d\x02\x01" .        // OID ecPublicKey
               "\x06\x08\x2a\x86\x48\xce\x3d\x03\x01\x07" .    // OID P-256
               "\x03\x42\x00" . $point;                        // BIT STRING

        return [
            'type' => 'EC',
            'alg' => 'ES256',
            'pem' => "-----BEGIN PUBLIC KEY-----\n" .
                     chunk_split(base64_encode($der), 64, "\n") .
                     "-----END PUBLIC KEY-----\n"
        ];
    }

    return null; // Only EC P-256 supported
}

/**
 * Verify WebAuthn assertion signature.
 *
 * @param string $publicKeyPem Public key in PEM format
 * @param string $authenticatorData Raw authenticator data
 * @param string $clientDataJSON Raw client data JSON
 * @param string $signature Raw signature bytes
 * @return bool True if signature is valid
 */
function verifyWebAuthnSignature(
    string $publicKeyPem,
    string $authenticatorData,
    string $clientDataJSON,
    string $signature
): bool {
    // WebAuthn signature is over: authenticatorData || SHA256(clientDataJSON)
    $clientDataHash = hash('sha256', $clientDataJSON, true);
    $signedData = $authenticatorData . $clientDataHash;

    // For ES256, the signature from WebAuthn is in raw format (r || s, 64 bytes)
    // OpenSSL expects DER format, so we need to convert
    if (strlen($signature) === 64) {
        $signature = convertRawEcdsaToDer($signature);
    }

    $pubKey = openssl_pkey_get_public($publicKeyPem);
    if (!$pubKey) {
        return false;
    }

    $result = openssl_verify($signedData, $signature, $pubKey, OPENSSL_ALGO_SHA256);
    return $result === 1;
}

/**
 * Convert raw ECDSA signature (r||s) to DER format for OpenSSL.
 *
 * @param string $raw 64-byte raw signature (32 bytes r + 32 bytes s)
 * @return string DER-encoded signature
 */
function convertRawEcdsaToDer(string $raw): string {
    // Split into r and s (each 32 bytes for P-256)
    $r = substr($raw, 0, 32);
    $s = substr($raw, 32, 32);

    // Remove leading zeros but keep one if high bit set (for positive number)
    $r = ltrim($r, "\x00");
    if (strlen($r) === 0 || ord($r[0]) >= 0x80) {
        $r = "\x00" . $r;
    }

    $s = ltrim($s, "\x00");
    if (strlen($s) === 0 || ord($s[0]) >= 0x80) {
        $s = "\x00" . $s;
    }

    // Build DER SEQUENCE
    $rDer = "\x02" . chr(strlen($r)) . $r;
    $sDer = "\x02" . chr(strlen($s)) . $s;
    $der = "\x30" . chr(strlen($rDer) + strlen($sDer)) . $rDer . $sDer;

    return $der;
}

/**
 * Verify rpIdHash in authenticatorData matches expected RP ID.
 *
 * @param string $authenticatorData Raw authenticator data
 * @param string $rpId Expected Relying Party ID
 * @return bool True if rpIdHash matches
 */
function verifyRpIdHash(string $authenticatorData, string $rpId): bool {
    if (strlen($authenticatorData) < 32) {
        return false;
    }

    $rpIdHash = substr($authenticatorData, 0, 32);
    $expectedHash = hash('sha256', $rpId, true);

    return hash_equals($expectedHash, $rpIdHash);
}

/**
 * Extract signCount from authenticatorData.
 *
 * @param string $authenticatorData Raw authenticator data
 * @return int Sign count value (0 if invalid)
 */
function extractSignCount(string $authenticatorData): int {
    if (strlen($authenticatorData) < 37) {
        return 0;
    }

    // signCount is at offset 33-36 (4 bytes, big-endian)
    $signCountBytes = substr($authenticatorData, 33, 4);
    return unpack('N', $signCountBytes)[1];
}

/**
 * Verify signCount has increased (detects cloned authenticators).
 *
 * @param int $newSignCount Sign count from current assertion
 * @param int $storedSignCount Previously stored sign count
 * @return bool True if valid (new > stored, or both are 0)
 */
function verifySignCount(int $newSignCount, int $storedSignCount): bool {
    // If both are 0, authenticator doesn't support sign count - allow
    if ($newSignCount === 0 && $storedSignCount === 0) {
        return true;
    }

    // New sign count must be greater than stored
    return $newSignCount > $storedSignCount;
}

// ============================================================================
// Helper Functions
// ============================================================================

/**
 * Load JSON data from file with shared lock.
 */
function loadJson(string $file): array {
    if (!file_exists($file)) {
        return [];
    }
    $handle = fopen($file, 'r');
    if (!$handle) {
        return [];
    }
    flock($handle, LOCK_SH);
    $stat = fstat($handle);
    $size = $stat['size'] ?? 0;
    $data = $size > 0 ? json_decode(fread($handle, $size), true) : [];
    flock($handle, LOCK_UN);
    fclose($handle);
    return $data ?: [];
}

/**
 * Save data to JSON file with exclusive lock.
 */
function saveJson(string $file, array $data): bool {
    $dir = dirname($file);
    if (!is_dir($dir)) {
        mkdir($dir, 0755, true);
    }
    $handle = fopen($file, 'c');
    if (!$handle) {
        return false;
    }
    flock($handle, LOCK_EX);
    ftruncate($handle, 0);
    fwrite($handle, json_encode($data, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES));
    fflush($handle);
    flock($handle, LOCK_UN);
    fclose($handle);
    return true;
}

/**
 * Check if current session is authenticated.
 */
function isAuthenticated(): bool {
    global $config;
    if (empty($_SESSION['authenticated']) || empty($_SESSION['user'])) {
        return false;
    }
    $now = time();
    $since = (int)($_SESSION['auth_time'] ?? 0);
    $seen = (int)($_SESSION['last_seen'] ?? $since);
    if ($now - $since > $config['session_max_age'] || $now - $seen > $config['session_lifetime']) {
        // Signed out, not destroyed: the rest of this request may still use
        // the session (a CSRF token, a login challenge).
        unset($_SESSION['authenticated'], $_SESSION['user'], $_SESSION['last_seen']);
        return false;
    }
    // Written once a minute, not on every forward-auth call: the idle limit is
    // counted in hours, and the file is touched by every request anyway.
    if ($now - $seen >= 60) {
        $_SESSION['last_seen'] = $now;
    }
    return true;
}

/**
 * Send JSON response and exit.
 */
function jsonResponse(array $data, int $code = 200): never {
    http_response_code($code);
    header('Content-Type: application/json');
    echo json_encode($data);
    exit;
}

/**
 * Get and validate redirect URL from request.
 */
function getRedirectUrl(): ?string {
    $url = $_GET['rd'] ?? $_SERVER['HTTP_X_ORIGINAL_URL'] ?? null;
    return validateRedirectUrl($url);
}

/**
 * Validate redirect URL to prevent open redirect attacks.
 * Only allows relative URLs or URLs matching configured domain.
 */
function validateRedirectUrl(?string $url): ?string {
    global $config;

    if (!$url) {
        return null;
    }

    // Allow relative URLs
    if (str_starts_with($url, '/')) {
        return $url;
    }

    $parsed = parse_url($url);
    if (!$parsed || empty($parsed['host'])) {
        return null;
    }

    // Get base domain from config (remove leading dot)
    $allowedDomain = ltrim($config['domain'], '.');
    $host = $parsed['host'];

    // Allow exact match or subdomains
    if ($host === $allowedDomain || str_ends_with($host, '.' . $allowedDomain)) {
        return $url;
    }

    return null;
}

/**
 * Generate or retrieve CSRF token for current session.
 */
function generateCsrfToken(): string {
    if (empty($_SESSION['csrf_token'])) {
        $_SESSION['csrf_token'] = bin2hex(random_bytes(32));
    }
    return $_SESSION['csrf_token'];
}

/**
 * Verify CSRF token using constant-time comparison.
 */
function verifyCsrfToken(?string $token): bool {
    if (empty($_SESSION['csrf_token']) || empty($token)) {
        return false;
    }
    return hash_equals($_SESSION['csrf_token'], $token);
}

/**
 * Check if identifier is rate limited.
 */
function checkRateLimit(string $identifier, string $action): bool {
    global $config;

    $dir = $config['ratelimit_dir'];
    if (!is_dir($dir)) {
        mkdir($dir, 0755, true);
    }

    $file = $dir . '/' . $action . '_' . md5($identifier) . '.json';
    $data = loadJson($file);
    $now = time();

    // Clean old attempts
    $attempts = array_filter(
        $data['attempts'] ?? [],
        fn($t) => $t > $now - $config['lockout_duration']
    );

    return count($attempts) < $config['max_login_attempts'];
}

/**
 * Record a failed authentication attempt.
 */
function recordFailedAttempt(string $identifier, string $action): void {
    global $config;

    $dir = $config['ratelimit_dir'];
    if (!is_dir($dir)) {
        mkdir($dir, 0755, true);
    }

    $file = $dir . '/' . $action . '_' . md5($identifier) . '.json';
    $data = loadJson($file);
    $now = time();

    $attempts = array_filter(
        $data['attempts'] ?? [],
        fn($t) => $t > $now - $config['lockout_duration']
    );
    $attempts[] = $now;

    saveJson($file, ['attempts' => array_values($attempts)]);
}

/**
 * Clear rate limit for identifier after successful auth.
 */
function clearRateLimit(string $identifier, string $action): void {
    global $config;
    $file = $config['ratelimit_dir'] . '/' . $action . '_' . md5($identifier) . '.json';
    if (file_exists($file)) {
        unlink($file);
    }
}

/**
 * Log authentication event.
 */
function logAuth(string $event, array $context = []): void {
    $log = array_merge([
        'time' => date('c'),
        'event' => $event,
        'ip' => $_SERVER['HTTP_X_FORWARDED_FOR'] ?? $_SERVER['REMOTE_ADDR'] ?? '-',
        'user' => $_SESSION['user'] ?? '-',
    ], $context);
    error_log('[authum] ' . json_encode($log));
}

/**
 * Get client IP address from headers.
 * Note: Trusts X-Forwarded-For, should only be used behind trusted proxy.
 */
function getClientIp(): string {
    $ip = $_SERVER['HTTP_X_FORWARDED_FOR'] ?? $_SERVER['REMOTE_ADDR'] ?? '127.0.0.1';
    // Take first IP if multiple (proxy chain)
    if (strpos($ip, ',') !== false) {
        $ip = trim(explode(',', $ip)[0]);
    }
    return $ip;
}

// ============================================================================
// API Endpoints
// ============================================================================

$action = $_GET['action'] ?? null;

// Forward Auth - Traefik calls this to check if user is authenticated
if ($action === 'verify') {
    if (isAuthenticated()) {
        header('Remote-User: ' . $_SESSION['user']);
        http_response_code(200);
        exit;
    }

    // Not authenticated - redirect to login
    $originalUrl = $_SERVER['HTTP_X_FORWARDED_PROTO'] ?? 'https';
    $originalUrl .= '://' . ($_SERVER['HTTP_X_FORWARDED_HOST'] ?? $_SERVER['HTTP_HOST']);
    $originalUrl .= $_SERVER['HTTP_X_FORWARDED_URI'] ?? $_SERVER['REQUEST_URI'];

    $loginUrl = $config['auth_url'] . '/?rd=' . urlencode($originalUrl);

    // Traefik forward auth - always redirect to login page
    // (Traefik will pass 302 to browser, 401 would show error page)
    header('Location: ' . $loginUrl);
    http_response_code(302);
    exit;
}

// Health check endpoint
if ($action === 'health') {
    jsonResponse([
        'status' => 'ok',
        'version' => '1.0.0',
        'timestamp' => date('c')
    ]);
}

// WebAuthn: Generate challenge
if ($action === 'challenge') {
    // Generate 32 random bytes and store as base64
    $challengeBytes = random_bytes(32);
    $challengeBase64 = base64_encode($challengeBytes);
    $_SESSION['webauthn_challenge'] = $challengeBase64;
    $_SESSION['webauthn_challenge_time'] = time();

    $credentials = loadJson($config['credentials_file']);
    $hasCredentials = count($credentials) > 0;

    // Return credential IDs for WebAuthn allowCredentials list
    // Note: Browser needs these for non-discoverable credentials
    // Security relies on challenge verification and signature validation
    $credentialIds = array_keys($credentials);

    jsonResponse([
        'challenge' => $challengeBase64,
        'rpId' => $config['rp_id'],
        'rpName' => $config['app_name'],
        'hasCredentials' => $hasCredentials,
        'credentialIds' => $credentialIds,
        'origin' => rtrim($config['auth_url'], '/')
    ]);
}

// WebAuthn: Register new credential
if ($action === 'register' && $_SERVER['REQUEST_METHOD'] === 'POST') {
    if (!isAuthenticated()) {
        jsonResponse(['error' => 'Not authenticated'], 401);
    }

    $input = json_decode(file_get_contents('php://input'), true);
    if (!$input || empty($input['credentialId'])) {
        jsonResponse(['error' => 'Invalid credential data'], 400);
    }

    // Verify challenge was recently generated
    $storedChallenge = $_SESSION['webauthn_challenge'] ?? null;
    $challengeTime = $_SESSION['webauthn_challenge_time'] ?? 0;
    if (!$storedChallenge || time() - $challengeTime > 120) { // 2 minute timeout
        jsonResponse(['error' => 'Challenge expired'], 400);
    }

    // Verify clientDataJSON contains our challenge
    if (empty($input['clientDataJSON'])) {
        jsonResponse(['error' => 'Missing clientDataJSON'], 400);
    }
    $clientData = json_decode(base64_decode($input['clientDataJSON']), true);
    // Convert stored base64 to base64url for comparison
    $expectedChallenge = rtrim(strtr($storedChallenge, '+/', '-_'), '=');
    if (!$clientData || ($clientData['challenge'] ?? '') !== $expectedChallenge) {
        logAuth('registration_failed', ['reason' => 'challenge_mismatch']);
        jsonResponse(['error' => 'Challenge verification failed'], 400);
    }

    // Verify type is webauthn.create
    if (($clientData['type'] ?? '') !== 'webauthn.create') {
        jsonResponse(['error' => 'Invalid clientData type'], 400);
    }

    // Verify origin matches auth URL (not rpId - rpId can be parent domain)
    $expectedOrigin = rtrim($config['auth_url'], '/');
    if (($clientData['origin'] ?? '') !== $expectedOrigin) {
        logAuth('registration_failed', ['reason' => 'origin_mismatch', 'expected' => $expectedOrigin, 'got' => $clientData['origin'] ?? 'missing']);
        jsonResponse(['error' => 'Origin verification failed'], 400);
    }

    // Extract public key from attestationObject
    $publicKeyData = null;
    $initialSignCount = 0;
    if (!empty($input['attestationObject'])) {
        $attestationObject = base64_decode($input['attestationObject']);
        $publicKeyData = extractPublicKeyFromAttestation($attestationObject);

        // Extract initial signCount from attestation authData
        $attestation = cbor_decode($attestationObject);
        if ($attestation && isset($attestation['authData'])) {
            $initialSignCount = extractSignCount($attestation['authData']);
        }
    }

    if (!$publicKeyData || empty($publicKeyData['pem'])) {
        logAuth('registration_failed', ['reason' => 'public_key_extraction_failed']);
        jsonResponse(['error' => 'Failed to extract public key'], 400);
    }

    $credentials = loadJson($config['credentials_file']);
    $credentials[$input['credentialId']] = [
        'publicKey' => $publicKeyData['pem'],
        'algorithm' => $publicKeyData['alg'],
        'signCount' => $initialSignCount,
        'name' => substr($input['name'] ?? 'Passkey', 0, 50), // Limit name length
        'user' => $_SESSION['user'],
        'userAgent' => substr($_SERVER['HTTP_USER_AGENT'] ?? '', 0, 500),
        'registeredAt' => date('Y-m-d H:i:s'),
        'ip' => getClientIp()
    ];
    saveJson($config['credentials_file'], $credentials);

    // Clear challenge after use
    unset($_SESSION['webauthn_challenge']);
    unset($_SESSION['webauthn_challenge_time']);

    logAuth('credential_registered', ['credential_name' => $input['name'] ?? 'Passkey']);

    jsonResponse(['success' => true, 'message' => 'Credential registered']);
}

// WebAuthn: Authenticate
if ($action === 'authenticate' && $_SERVER['REQUEST_METHOD'] === 'POST') {
    $clientIp = getClientIp();

    // Rate limiting
    if (!checkRateLimit($clientIp, 'webauthn')) {
        logAuth('auth_ratelimited', ['method' => 'webauthn']);
        jsonResponse(['success' => false, 'error' => 'Too many attempts. Try again later.'], 429);
    }

    $input = json_decode(file_get_contents('php://input'), true);
    if (!$input || empty($input['credentialId'])) {
        jsonResponse(['success' => false, 'error' => 'No credential'], 400);
    }

    // Verify challenge exists and is recent
    $storedChallenge = $_SESSION['webauthn_challenge'] ?? null;
    $challengeTime = $_SESSION['webauthn_challenge_time'] ?? 0;

    if (!$storedChallenge || time() - $challengeTime > 120) {
        recordFailedAttempt($clientIp, 'webauthn');
        logAuth('auth_failed', ['method' => 'webauthn', 'reason' => 'challenge_expired']);
        jsonResponse(['success' => false, 'error' => 'Challenge expired'], 401);
    }

    // Verify authenticatorData was provided (proves browser actually did WebAuthn ceremony)
    if (empty($input['authenticatorData']) || empty($input['clientDataJSON']) || empty($input['signature'])) {
        recordFailedAttempt($clientIp, 'webauthn');
        logAuth('auth_failed', ['method' => 'webauthn', 'reason' => 'missing_webauthn_data']);
        jsonResponse(['success' => false, 'error' => 'Invalid authentication data'], 400);
    }

    // Decode and verify clientDataJSON
    $clientDataRaw = base64_decode($input['clientDataJSON'], true);
    if ($clientDataRaw === false) {
        recordFailedAttempt($clientIp, 'webauthn');
        logAuth('auth_failed', ['method' => 'webauthn', 'reason' => 'invalid_base64']);
        jsonResponse(['success' => false, 'error' => 'Invalid authentication data'], 400);
    }

    $clientData = json_decode($clientDataRaw, true);
    if (!$clientData) {
        recordFailedAttempt($clientIp, 'webauthn');
        logAuth('auth_failed', ['method' => 'webauthn', 'reason' => 'invalid_client_data']);
        jsonResponse(['success' => false, 'error' => 'Invalid client data'], 400);
    }

    // Verify challenge (stored as base64, clientData has base64url)
    $expectedChallenge = rtrim(strtr($storedChallenge, '+/', '-_'), '=');
    if (($clientData['challenge'] ?? '') !== $expectedChallenge) {
        recordFailedAttempt($clientIp, 'webauthn');
        logAuth('auth_failed', ['method' => 'webauthn', 'reason' => 'challenge_mismatch']);
        jsonResponse(['success' => false, 'error' => 'Challenge verification failed'], 401);
    }

    // Verify origin matches auth URL (not rpId - rpId can be parent domain)
    $expectedOrigin = rtrim($config['auth_url'], '/');
    if (($clientData['origin'] ?? '') !== $expectedOrigin) {
        recordFailedAttempt($clientIp, 'webauthn');
        logAuth('auth_failed', ['method' => 'webauthn', 'reason' => 'origin_mismatch']);
        jsonResponse(['success' => false, 'error' => 'Origin verification failed'], 401);
    }

    // Verify type is webauthn.get
    if (($clientData['type'] ?? '') !== 'webauthn.get') {
        recordFailedAttempt($clientIp, 'webauthn');
        logAuth('auth_failed', ['method' => 'webauthn', 'reason' => 'invalid_type']);
        jsonResponse(['success' => false, 'error' => 'Invalid client data type'], 400);
    }

    $credentials = loadJson($config['credentials_file']);
    if (!isset($credentials[$input['credentialId']])) {
        recordFailedAttempt($clientIp, 'webauthn');
        logAuth('auth_failed', ['method' => 'webauthn', 'reason' => 'unknown_credential']);
        jsonResponse(['success' => false, 'error' => 'Authentication failed'], 401);
    }

    $credential = $credentials[$input['credentialId']];

    // Verify cryptographic signature using stored public key
    $publicKeyPem = $credential['publicKey'] ?? '';
    if (empty($publicKeyPem)) {
        // Legacy credential without public key - require re-registration
        recordFailedAttempt($clientIp, 'webauthn');
        logAuth('auth_failed', ['method' => 'webauthn', 'reason' => 'missing_public_key']);
        jsonResponse(['success' => false, 'error' => 'Credential requires re-registration'], 401);
    }

    // Decode binary data with strict validation
    $authenticatorData = base64_decode($input['authenticatorData'], true);
    $signature = base64_decode($input['signature'], true);

    if ($authenticatorData === false || $signature === false) {
        recordFailedAttempt($clientIp, 'webauthn');
        logAuth('auth_failed', ['method' => 'webauthn', 'reason' => 'invalid_base64_data']);
        jsonResponse(['success' => false, 'error' => 'Invalid authentication data encoding'], 400);
    }

    // Verify rpIdHash matches our RP ID
    if (!verifyRpIdHash($authenticatorData, $config['rp_id'])) {
        recordFailedAttempt($clientIp, 'webauthn');
        logAuth('auth_failed', ['method' => 'webauthn', 'reason' => 'rpid_mismatch']);
        jsonResponse(['success' => false, 'error' => 'RP ID verification failed'], 401);
    }

    // Verify signCount to detect cloned authenticators
    $newSignCount = extractSignCount($authenticatorData);
    $storedSignCount = $credential['signCount'] ?? 0;

    if (!verifySignCount($newSignCount, $storedSignCount)) {
        recordFailedAttempt($clientIp, 'webauthn');
        logAuth('auth_failed', [
            'method' => 'webauthn',
            'reason' => 'signcount_invalid',
            'stored' => $storedSignCount,
            'received' => $newSignCount
        ]);
        jsonResponse(['success' => false, 'error' => 'Possible cloned authenticator detected'], 401);
    }

    if (!verifyWebAuthnSignature($publicKeyPem, $authenticatorData, $clientDataRaw, $signature)) {
        recordFailedAttempt($clientIp, 'webauthn');
        logAuth('auth_failed', ['method' => 'webauthn', 'reason' => 'signature_invalid']);
        jsonResponse(['success' => false, 'error' => 'Signature verification failed'], 401);
    }

    // Signature verified - authentication successful

    // Update last used and signCount
    $credentials[$input['credentialId']]['lastUsed'] = date('Y-m-d H:i:s');
    $credentials[$input['credentialId']]['signCount'] = $newSignCount;
    saveJson($config['credentials_file'], $credentials);

    // Regenerate session ID to prevent fixation
    session_regenerate_id(true);

    // Create session
    $_SESSION['authenticated'] = true;
    $_SESSION['user'] = $credential['user'] ?? 'admin';
    $_SESSION['auth_time'] = time();
    $_SESSION['last_seen'] = time();
    $_SESSION['auth_method'] = 'webauthn';

    // Clear challenge
    unset($_SESSION['webauthn_challenge']);
    unset($_SESSION['webauthn_challenge_time']);

    // Clear rate limit on success
    clearRateLimit($clientIp, 'webauthn');

    logAuth('auth_success', ['method' => 'webauthn', 'credential' => substr($input['credentialId'], 0, 20)]);

    jsonResponse(['success' => true, 'redirect' => getRedirectUrl()]);
}

// One-time sign-in link. The node writes /app/auth/onetime.json
// {"sha256": <hash of the token>, "user": ..., "expires": <unix time>} -- core-api
// when the first-run page creates the owner, or the owner's login command later.
// The file is removed on the first attempt, right or wrong, so a link works once.
if ($action === 'onetime' && $_SERVER['REQUEST_METHOD'] === 'GET') {
    $file = dirname($config['users_file']) . '/onetime.json';
    $token = (string)($_GET['t'] ?? '');
    $rec = is_file($file) ? json_decode((string)file_get_contents($file), true) : null;
    @unlink($file);
    if (!is_array($rec) || $token === '' || time() > (int)($rec['expires'] ?? 0)
        || !hash_equals((string)($rec['sha256'] ?? ''), hash('sha256', $token))) {
        logAuth('auth_failed', ['method' => 'onetime']);
        header('Location: /');
        exit;
    }
    session_regenerate_id(true);
    $_SESSION['authenticated'] = true;
    $_SESSION['user'] = (string)($rec['user'] ?? 'admin');
    $_SESSION['auth_time'] = time();
    $_SESSION['last_seen'] = time();
    $_SESSION['auth_method'] = 'onetime';
    logAuth('auth_success', ['method' => 'onetime']);
    header('Location: ' . getRedirectUrl());
    exit;
}

// Password authentication
if ($action === 'password' && $_SERVER['REQUEST_METHOD'] === 'POST') {
    // Refused on the SERVER, not merely hidden in the page. Hiding the form
    // would leave the endpoint answering to anything that posts to it, which
    // is not a disabled password -- it is an invisible one.
    if (!$config['password_login']) {
        logAuth('auth_password_disabled', ['method' => 'password']);
        jsonResponse(['success' => false, 'error' => 'Password login is disabled on this node. Use a passkey.'], 403);
    }
    $clientIp = getClientIp();

    // Rate limiting
    if (!checkRateLimit($clientIp, 'password')) {
        logAuth('auth_ratelimited', ['method' => 'password']);
        jsonResponse(['success' => false, 'error' => 'Too many attempts. Try again later.'], 429);
    }

    $input = json_decode(file_get_contents('php://input'), true);
    $username = $input['username'] ?? '';
    $password = $input['password'] ?? '';

    $users = loadJson($config['users_file']);

    // Constant-time comparison to prevent timing attacks
    $hash = $users[$username]['password'] ?? '$argon2id$v=19$m=65536,t=4,p=1$dummysalt$dummyhashvalue';
    $validUser = isset($users[$username]);
    $validPassword = password_verify($password, $hash);

    if ($validUser && $validPassword) {
        // Regenerate session ID to prevent fixation
        session_regenerate_id(true);

        $_SESSION['authenticated'] = true;
        $_SESSION['user'] = $username;
        $_SESSION['auth_time'] = time();
        $_SESSION['last_seen'] = time();
        $_SESSION['auth_method'] = 'password';

        // Clear rate limit on success
        clearRateLimit($clientIp, 'password');

        logAuth('auth_success', ['method' => 'password']);

        jsonResponse(['success' => true, 'redirect' => getRedirectUrl()]);
    } else {
        recordFailedAttempt($clientIp, 'password');
        logAuth('auth_failed', ['method' => 'password', 'username' => $username]);
        jsonResponse(['success' => false, 'error' => 'Invalid credentials'], 401);
    }
}

// Logout - POST only with CSRF protection
if ($action === 'logout' && $_SERVER['REQUEST_METHOD'] === 'POST') {
    $input = json_decode(file_get_contents('php://input'), true);
    $csrfToken = $input['csrf_token'] ?? $_POST['csrf_token'] ?? '';

    if (!verifyCsrfToken($csrfToken)) {
        jsonResponse(['error' => 'Invalid CSRF token'], 403);
    }

    logAuth('logout');
    session_destroy();
    header('Location: /');
    exit;
}

// Sign out from another page (the Studio): it cannot hold this page's CSRF token, so it
// links here (GET) and the person confirms with a form that carries it.
if ($action === 'logout' && $_SERVER['REQUEST_METHOD'] === 'GET') {
    if (!isAuthenticated()) {
        header('Location: /');
        exit;
    }
    $csrfToken = generateCsrfToken();
    ?>
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title><?= htmlspecialchars($config['app_name']) ?> - Sign out</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="<?= htmlspecialchars($kitUrl) ?>tokens.css">
    <link rel="stylesheet" href="<?= htmlspecialchars($kitUrl) ?>base.css">
    <link rel="stylesheet" href="<?= htmlspecialchars($kitUrl) ?>components.css">
    <script nonce="<?= $cspNonce ?>">
        var snTheme = (document.cookie.match(/(?:^|; )sn-theme=(light|dark)/)||[])[1];   // set by Studio and the panel on the node's domain
        document.documentElement.dataset.theme = snTheme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    </script>
    <style>@layer app { body { background: var(--sn-bg); } .sn-page { max-width: 480px; }
        .cred-actions { display: flex; gap: var(--sn-sp-2); margin-top: var(--sn-sp-4); } }</style>
</head>
<body>
    <main class="sn-page">
        <div class="sn-page__head"><h1 class="sn-page__title">Sign out?</h1></div>
        <p class="sn-page__sub">Signed in as <?= htmlspecialchars($_SESSION['user']) ?>. You sign back in with your password or a passkey.</p>
        <form method="POST" action="?action=logout" class="cred-actions">
            <input type="hidden" name="csrf_token" value="<?= htmlspecialchars($csrfToken) ?>">
            <button type="submit" class="sn-btn sn-btn--primary">Sign out</button>
            <a href="<?= htmlspecialchars($_SERVER['HTTP_REFERER'] ?? '/') ?>" class="sn-btn sn-btn--ghost">Stay signed in</a>
        </form>
    </main>
</body>
</html>
    <?php
    exit;
}

// Change the password of the person signed in: the current one, then the new one twice.
// The form is on the settings page; the answer comes back there as ?pw=<code>.
if ($action === 'change_password' && $_SERVER['REQUEST_METHOD'] === 'POST') {
    if (!isAuthenticated()) {
        header('Location: /');
        exit;
    }
    $back = function (string $code): never { header('Location: /?page=settings&pw=' . $code); exit; };
    if (!verifyCsrfToken($_POST['csrf_token'] ?? '')) {
        $back('token');
    }
    $user = $_SESSION['user'];
    $users = loadJson($config['users_file']);
    $current = (string)($_POST['current'] ?? '');
    $new = (string)($_POST['new'] ?? '');
    if (!isset($users[$user]) || !password_verify($current, $users[$user]['password'] ?? '')) {
        logAuth('password_change_refused', ['reason' => 'current']);
        $back('current');
    }
    if (strlen($new) < 10) {
        $back('short');
    }
    if ($new !== (string)($_POST['repeat'] ?? '')) {
        $back('repeat');
    }
    $users[$user]['password'] = password_hash($new, PASSWORD_BCRYPT);
    $users[$user]['password_changed'] = date('c');
    if (!saveJson($config['users_file'], $users)) {
        $back('save');
    }
    logAuth('password_changed');
    $back('ok');
}

// List credentials (for settings page)
if ($action === 'credentials' && isAuthenticated()) {
    $credentials = loadJson($config['credentials_file']);
    $currentUser = $_SESSION['user'];
    $list = [];
    foreach ($credentials as $id => $cred) {
        // Only show credentials belonging to current user
        if (($cred['user'] ?? '') === $currentUser) {
            $list[] = [
                'id' => $id,
                'name' => $cred['name'] ?? 'Passkey',
                'registeredAt' => $cred['registeredAt'] ?? '',
                'lastUsed' => $cred['lastUsed'] ?? 'Never'
            ];
        }
    }
    jsonResponse(['credentials' => $list]);
}

// Delete credential
if ($action === 'delete-credential' && $_SERVER['REQUEST_METHOD'] === 'POST' && isAuthenticated()) {
    $input = json_decode(file_get_contents('php://input'), true);
    $credentialId = $input['credentialId'] ?? '';

    // CSRF check
    $csrfToken = $input['csrf_token'] ?? '';
    if (!verifyCsrfToken($csrfToken)) {
        jsonResponse(['error' => 'Invalid CSRF token'], 403);
    }

    $credentials = loadJson($config['credentials_file']);
    if (isset($credentials[$credentialId])) {
        // Verify ownership
        if (($credentials[$credentialId]['user'] ?? '') !== $_SESSION['user']) {
            jsonResponse(['error' => 'Unauthorized'], 403);
        }

        unset($credentials[$credentialId]);
        saveJson($config['credentials_file'], $credentials);
        logAuth('credential_deleted', ['credential_id' => substr($credentialId, 0, 20) . '...']);
        jsonResponse(['success' => true]);
    }
    jsonResponse(['error' => 'Credential not found'], 404);
}

// ============================================================================
// Settings Page (after auth)
// ============================================================================

if (($_GET['page'] ?? '') === 'settings') {
    if (!isAuthenticated()) {
        header('Location: /?rd=' . urlencode($_SERVER['REQUEST_URI']));
        exit;
    }

    $credentials = loadJson($config['credentials_file']);
    $currentUser = $_SESSION['user'];
    $csrfToken = generateCsrfToken();
    // Filter to show only current user's credentials
    $userCredentials = array_filter($credentials, fn($c) => ($c['user'] ?? '') === $currentUser);
    ?>
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title><?= htmlspecialchars($config['app_name']) ?> - Sign-in settings</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
    <?php /* the shared UI kit, as on the sign-in page (look audit, 2026-10-30) */ ?>
    <link rel="stylesheet" href="<?= htmlspecialchars($kitUrl) ?>tokens.css">
    <link rel="stylesheet" href="<?= htmlspecialchars($kitUrl) ?>base.css">
    <link rel="stylesheet" href="<?= htmlspecialchars($kitUrl) ?>components.css">
    <script nonce="<?= $cspNonce ?>">
        var snTheme = (document.cookie.match(/(?:^|; )sn-theme=(light|dark)/)||[])[1];   // set by Studio and the panel on the node's domain
        document.documentElement.dataset.theme = snTheme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    </script>
    <style>
        @layer app {
        body { background: var(--sn-bg); }
        .sn-page { max-width: 600px; }
        .cred-list .credential { display: flex; justify-content: space-between; align-items: center; gap: var(--sn-sp-3); padding: var(--sn-sp-3) 0; border-bottom: 1px solid var(--sn-rule); }
        .cred-list .credential:last-child { border-bottom: none; }
        .credential-name { color: var(--sn-ink); font-weight: 500; }
        .credential-meta { color: var(--sn-ink-mute); font-size: 13px; margin-top: 2px; }
        .cred-empty { color: var(--sn-ink-mute); font-size: 13px; padding: var(--sn-sp-2) 0; }
        .cred-actions { display: flex; gap: var(--sn-sp-2); margin-top: var(--sn-sp-4); }
        .pw-form { display: flex; flex-direction: column; gap: var(--sn-sp-3); }
        .pw-row { display: flex; justify-content: space-between; align-items: center; gap: var(--sn-sp-3); }
        .pw-say { font-size: 13px; color: var(--sn-ink-mute); } .pw-say.is-err { color: var(--sn-danger); }
        }
    </style>
</head>
<body>
    <main class="sn-page">
        <div class="sn-page__head"><h1 class="sn-page__title">Sign-in settings</h1>
            <a href="/" class="sn-btn sn-btn--ghost">Done</a></div>
        <p class="sn-page__sub">Signed in as <?= htmlspecialchars($_SESSION['user']) ?></p>

        <?php
        $pwSay = ['ok' => 'The password is changed.', 'current' => 'The current password is not right.',
                  'short' => 'The new password needs at least 10 characters.', 'repeat' => 'The two new passwords differ.',
                  'token' => 'The page was too old. Try again.', 'save' => 'The password could not be saved.'];
        $pwCode = $_GET['pw'] ?? '';
        ?>
        <section class="sn-section">
            <h2 class="sn-section__title">Password</h2>
            <form class="sn-card pw-form" method="POST" action="?action=change_password">
                <input type="hidden" name="csrf_token" value="<?= htmlspecialchars($csrfToken) ?>">
                <label class="sn-field"><input type="password" name="current" placeholder="Current password" autocomplete="current-password" required></label>
                <label class="sn-field"><input type="password" name="new" placeholder="New password, 10 characters or more" autocomplete="new-password" minlength="10" required></label>
                <label class="sn-field"><input type="password" name="repeat" placeholder="New password again" autocomplete="new-password" minlength="10" required></label>
                <div class="pw-row"><span class="pw-say<?= $pwCode && $pwCode !== 'ok' ? ' is-err' : '' ?>"><?= htmlspecialchars($pwSay[$pwCode] ?? '') ?></span>
                    <button type="submit" class="sn-btn sn-btn--primary">Change password</button></div>
            </form>
        </section>

        <section class="sn-section">
            <h2 class="sn-section__title">Passkeys</h2>
            <div class="sn-card cred-list" id="credentials-list">
                <?php if (!$userCredentials): ?>
                <div class="cred-empty">No passkeys yet. A passkey signs you in with your fingerprint, face or device PIN.</div>
                <?php endif; ?>
                <?php foreach ($userCredentials as $id => $cred): ?>
                <div class="credential" data-id="<?= htmlspecialchars($id) ?>">
                    <div>
                        <div class="credential-name"><?= htmlspecialchars($cred['name'] ?? 'Passkey') ?></div>
                        <div class="credential-meta">Added <?= htmlspecialchars($cred['registeredAt'] ?? 'at an unknown time') ?></div>
                    </div>
                    <button class="sn-btn sn-btn--ghost delete-credential-btn" data-credential-id="<?= htmlspecialchars($id) ?>">Delete</button>
                </div>
                <?php endforeach; ?>
            </div>
            <div class="cred-actions">
                <button id="register-passkey-btn" class="sn-btn sn-btn--primary">Add a passkey</button>
                <form method="POST" action="?action=logout">
                    <input type="hidden" name="csrf_token" value="<?= htmlspecialchars($csrfToken) ?>">
                    <button type="submit" class="sn-btn sn-btn--ghost">Sign out</button>
                </form>
            </div>
        </section>
    </main>

    <script nonce="<?= htmlspecialchars($cspNonce) ?>">
    const csrfToken = <?= json_encode($csrfToken) ?>;

    function arrayBufferToBase64(buffer) {
        return btoa(String.fromCharCode(...new Uint8Array(buffer)));
    }

    function base64ToUint8Array(base64) {
        const binary = atob(base64);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i++) {
            bytes[i] = binary.charCodeAt(i);
        }
        return bytes;
    }

    async function registerPasskey() {
        try {
            const name = prompt('Name for this passkey:', navigator.platform || 'My Device');
            if (!name) return;

            const challengeRes = await fetch('/?action=challenge');
            const challengeData = await challengeRes.json();

            const credential = await navigator.credentials.create({
                publicKey: {
                    challenge: base64ToUint8Array(challengeData.challenge),
                    rp: { name: 'Authum', id: challengeData.rpId },
                    user: {
                        id: Uint8Array.from('admin', c => c.charCodeAt(0)),
                        name: 'admin',
                        displayName: 'Admin'
                    },
                    pubKeyCredParams: [
                        { alg: -7, type: 'public-key' },
                        { alg: -257, type: 'public-key' }
                    ],
                    // authenticatorAttachment is deliberately NOT set.
                    //
                    // It used to be 'platform', which means "only the built-in
                    // authenticator of THIS device". That silently excludes
                    // every portable one: a password manager (Bitwarden and
                    // friends), a phone used as a key, a hardware key. The
                    // browser does not even offer them, so an operator who
                    // believes they saved a passkey to their vault has in fact
                    // bound it to one laptop -- and discovers it only when they
                    // try to sign in from their phone and are told no passkey
                    // exists.
                    //
                    // Leaving it unset offers both, and the operator chooses.
                    // That also makes "turn password login off" a real option:
                    // with device-bound-only passkeys, the password is the de
                    // facto fallback for every other device.
                    //
                    // residentKey 'preferred': ask for a DISCOVERABLE
                    // credential. Password managers need it to store and sync
                    // the passkey properly, and it allows a sign-in flow that
                    // does not have to be told the credential id up front.
                    authenticatorSelection: {
                        residentKey: 'preferred',
                        userVerification: 'preferred'
                    },
                    timeout: 60000
                }
            });

            const credentialId = arrayBufferToBase64(credential.rawId);
            const attestationObject = arrayBufferToBase64(credential.response.attestationObject);
            const clientDataJSON = arrayBufferToBase64(credential.response.clientDataJSON);

            const res = await fetch('/?action=register', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    credentialId,
                    attestationObject,
                    clientDataJSON,
                    name,
                    csrf_token: csrfToken
                })
            });

            const data = await res.json();
            if (res.ok) {
                location.reload();
            } else {
                alert('Registration failed: ' + (data.error || 'Unknown error'));
            }
        } catch (e) {
            alert('Error: ' + e.message);
        }
    }

    async function deleteCredential(id) {
        if (!confirm('Delete this passkey?')) return;

        const res = await fetch('/?action=delete-credential', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ credentialId: id, csrf_token: csrfToken })
        });

        if (res.ok) {
            location.reload();
        }
    }

    // Event listeners (no inline handlers)
    document.getElementById('register-passkey-btn')?.addEventListener('click', registerPasskey);

    document.querySelectorAll('.delete-credential-btn').forEach(btn => {
        btn.addEventListener('click', () => deleteCredential(btn.dataset.credentialId));
    });
    </script>
</body>
</html>
    <?php
    exit;
}

// ============================================================================
// Main Login Page
// ============================================================================

$redirectUrl = getRedirectUrl();

// Auto-redirect an authenticated visitor ONLY when something sent them here.
//
// It used to fall back to default_redirect as well, which meant an operator who
// deliberately opened the auth host while signed in was always bounced into the
// app -- so the signed-in landing page below (with "Open <app>" and the
// WebAuthn credentials card) could never be seen, and passkeys could not be
// added, listed or deleted except by typing /?page=settings from memory.
// Nothing links to that URL. The owner hit this trying to register a passkey
// for his phone and reasonably concluded he was doing something wrong.
//
// With rd present the bounce is still right: a proxy or a fresh login sent them
// here to be returned somewhere specific. Without it, they came on purpose.
if (isAuthenticated() && $redirectUrl) {
    header('Location: ' . $redirectUrl);
    exit;
}

$credentials = loadJson($config['credentials_file']);
$hasCredentials = count($credentials) > 0;
$csrfToken = generateCsrfToken();
?>
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title><?= htmlspecialchars($config['app_name']) ?></title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
    <?php /* The shared UI kit, linked from its one source, the Studio, which serves
             it without a session under /welcome/ui/ (look audit, 2026-10-30: this page
             had its own copy of the tokens, 6 px buttons and 14 px text). */ ?>
    <link rel="stylesheet" href="<?= htmlspecialchars($kitUrl) ?>tokens.css">
    <link rel="stylesheet" href="<?= htmlspecialchars($kitUrl) ?>base.css">
    <link rel="stylesheet" href="<?= htmlspecialchars($kitUrl) ?>components.css">
    <script nonce="<?= $cspNonce ?>">
        // the kit's dark theme is data-theme on <html>; this page follows the system's
        var snTheme = (document.cookie.match(/(?:^|; )sn-theme=(light|dark)/)||[])[1];   // set by Studio and the panel on the node's domain
        document.documentElement.dataset.theme = snTheme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    </script>
    <style>
        @layer app {
        body { min-height: 100vh; display: flex; align-items: center; justify-content: center; background: var(--sn-bg); color: var(--sn-ink-2); }
        .auth-container { width: 340px; padding: var(--sn-sp-10) var(--sn-sp-8); text-align: center; }
        .logo { color: var(--sn-ink); font: 600 22px/1.2 var(--sn-font); letter-spacing: -.02em; margin-bottom: var(--sn-sp-8); }
        .fingerprint-icon { margin-bottom: var(--sn-sp-5); opacity: .8; }
        .fingerprint-icon svg { width: 64px; height: 64px; }
        .auth-btn { width: 100%; margin-bottom: var(--sn-sp-2); text-decoration: none; }
        .status, .no-credentials { color: var(--sn-ink-mute); font-size: 13px; margin-top: var(--sn-sp-4); }
        .error { color: var(--sn-danger); font-size: 13px; margin-top: var(--sn-sp-4); display: none; }
        .divider { color: var(--sn-ink-faint); font-size: 13px; margin: var(--sn-sp-5) 0; }
        .password-form { display: none; }
        .password-form .sn-field { margin-bottom: var(--sn-sp-2); }
        .forgot { margin-top: var(--sn-sp-3); font-size: 13px; color: var(--sn-ink-mute); text-align: center; }
        .forgot summary { cursor: pointer; list-style: none; } .forgot summary::-webkit-details-marker { display: none; }
        .forgot summary:hover { color: var(--sn-ink); }
        .forgot p { margin-top: var(--sn-sp-2); line-height: 1.5; }
        }
    </style>
</head>
<body>

    <div class="auth-container sn-card">
        <div class="logo"><?= htmlspecialchars($config['app_name']) ?></div>

        <?php if (isAuthenticated()): ?>
            <p style="color:var(--text-primary);margin-bottom:20px">Signed in</p>
            <p style="color:#888;font-size:12px;margin-bottom:20px">User: <?= htmlspecialchars($_SESSION['user']) ?></p>
            <a href="<?= htmlspecialchars($config['default_redirect']) ?>" class="sn-btn sn-btn--primary auth-btn">Open <?= htmlspecialchars($config['app_name']) ?></a>
            <a href="?page=settings" class="sn-btn sn-btn--ghost auth-btn">Settings</a>
            <form method="POST" action="?action=logout" style="width:100%">
                <input type="hidden" name="csrf_token" value="<?= htmlspecialchars($csrfToken) ?>">
                <button type="submit" class="sn-btn sn-btn--ghost auth-btn">Logout</button>
            </form>
        <?php else: ?>
            <?php /* Every owner has a password now, a local node's too: the first-run page shows
                     it once. Before, a local node hid the form and only told the person
                     to run a command, so signing out left a non-technical owner locked out. */ ?>
            <?php if ($config['password_login']): ?>
            <div id="password-section">
                <div class="password-form" id="password-form" style="display:block">
                    <label class="sn-field"><input type="text" id="username" placeholder="Username" autocomplete="username"></label>
                    <label class="sn-field"><input type="password" id="password" placeholder="Password" autocomplete="current-password"></label>
                    <button class="sn-btn sn-btn--primary auth-btn" id="login-btn">Login</button>
                </div>
            </div>
            <?php endif; ?>

            <p class="error" id="error"></p>
            <details class="forgot"><summary>Forgot password?</summary>
                <?php if (str_starts_with($config['auth_url'], 'http://')): ?>
                <p>Use the Open Smart Node shortcut on your desktop.</p>
                <?php else: ?>
                <p>On the server, run <code>sudo smart-node-signin</code> and open the link it shows.</p>
                <?php endif; ?>
            </details>

            <?php if ($hasCredentials): ?>
            <?php if ($config['password_login']): ?><div class="divider">or</div><?php endif; ?>

            <div id="webauthn-section">
                <button id="auth-btn" class="sn-btn <?= $config['password_login'] ? 'sn-btn--ghost' : 'sn-btn--primary' ?> auth-btn">Use Passkey</button>
                <p class="status" id="status"></p>
            </div>
            <?php else: ?>
            <?php if (!$config['password_login']): ?>
            <!-- Password login is off and no passkey is registered: there is no
                 way in from this page. Say so, rather than showing an empty box
                 and letting the operator conclude the node is broken. -->
            <p class="status">Password login is disabled on this node and no passkey is registered.
               Recovery needs shell access: set AUTHUM_PASSWORD_LOGIN=on and recreate the container.</p>
            <?php endif; ?>
            <?php endif; ?>
        <?php endif; ?>
    </div>

    <script nonce="<?= htmlspecialchars($cspNonce) ?>">
    const redirectUrl = <?= json_encode($redirectUrl) ?>;

    function showError(msg) {
        const el = document.getElementById('error');
        el.textContent = msg;
        el.style.display = 'block';
    }

    function setStatus(msg) {
        const el = document.getElementById('status');
        if (el) el.textContent = msg;
    }

    function togglePasswordForm() {
        const form = document.getElementById('password-form');
        const btn = document.getElementById('password-toggle');
        if (form.style.display === 'none' || !form.style.display) {
            form.style.display = 'block';
            btn.style.display = 'none';
        }
    }

    function arrayBufferToBase64(buffer) {
        return btoa(String.fromCharCode(...new Uint8Array(buffer)));
    }

    function base64ToUint8Array(base64) {
        const binary = atob(base64);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i++) {
            bytes[i] = binary.charCodeAt(i);
        }
        return bytes;
    }

    async function authenticate() {
        try {
            setStatus('Requesting credential...');

            const challengeRes = await fetch('/?action=challenge');
            const challengeData = await challengeRes.json();

            if (!challengeData.hasCredentials) {
                showError('No passkeys registered');
                return;
            }

            const allowCredentials = challengeData.credentialIds.map(id => ({
                type: 'public-key',
                id: Uint8Array.from(atob(id), c => c.charCodeAt(0))
            }));

            setStatus('Touch sensor...');

            const assertion = await navigator.credentials.get({
                publicKey: {
                    challenge: base64ToUint8Array(challengeData.challenge),
                    rpId: challengeData.rpId,
                    allowCredentials,
                    userVerification: 'preferred',
                    timeout: 60000
                }
            });

            setStatus('Verifying...');

            const credentialId = arrayBufferToBase64(assertion.rawId);
            const authenticatorData = arrayBufferToBase64(assertion.response.authenticatorData);
            const clientDataJSON = arrayBufferToBase64(assertion.response.clientDataJSON);
            const signature = arrayBufferToBase64(assertion.response.signature);

            const res = await fetch('/?action=authenticate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    credentialId,
                    authenticatorData,
                    clientDataJSON,
                    signature
                })
            });

            const data = await res.json();

            if (data.success) {
                setStatus('Success!');
                if (data.redirect) {
                    window.location.href = data.redirect;
                } else {
                    window.location.href = new URLSearchParams(window.location.search).get('rd') || '/';
                }
            } else {
                showError(data.error || 'Authentication failed');
            }
        } catch (e) {
            if (e.name === 'NotAllowedError') {
                setStatus('Touch sensor to continue');
            } else {
                showError(e.message);
            }
        }
    }

    async function loginWithPassword() {
        const username = document.getElementById('username').value;
        const password = document.getElementById('password').value;

        try {
            const res = await fetch('/?action=password', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ username, password })
            });

            const data = await res.json();

            if (data.success) {
                if (data.redirect) {
                    window.location.href = data.redirect;
                } else {
                    window.location.href = new URLSearchParams(window.location.search).get('rd') || '/';
                }
            } else {
                showError(data.error || 'Invalid credentials');
            }
        } catch (e) {
            showError(e.message);
        }
    }

    // Event listeners (no inline handlers)
    document.getElementById('auth-btn')?.addEventListener('click', authenticate);
    document.getElementById('password-toggle')?.addEventListener('click', togglePasswordForm);
    document.getElementById('login-btn')?.addEventListener('click', loginWithPassword);

    // Allow Enter key to submit password form
    document.getElementById('password')?.addEventListener('keypress', (e) => {
        if (e.key === 'Enter') loginWithPassword();
    });
    </script>
</body>
</html>
