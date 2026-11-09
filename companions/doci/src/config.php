<?php
/**
 * DOCI - Documented Agent Control System
 *
 * Authentication handled by Traefik ForwardAuth middleware (Authum).
 * User info passed via Remote-User header.
 */

// Set timezone
date_default_timezone_set(getenv('TZ') ?: 'UTC');

// Database configuration from environment variables
// Default 'postgres' matches the service name in docker-compose.yml's
// production stack. Override via DB_HOST env for any other deployment.
define('DB_HOST', getenv('DB_HOST') ?: 'postgres');
define('DB_PORT', getenv('DB_PORT') ?: '5432');
define('DB_NAME', getenv('DB_NAME') ?: 'doci');
define('DB_USER', getenv('DB_USER') ?: 'doci_app');
define('DB_PASS', getenv('DB_PASS') ?: '');  // REQUIRED: Set via environment variable

// Validate required configuration
if (empty(DB_PASS)) {
    error_log('[DOCI] CRITICAL: DB_PASS environment variable not set');
    if (php_sapi_name() !== 'cli') {
        http_response_code(500);
        die('Configuration error: Database password not configured');
    }
}

// Application root directory (for lib/ files that need to reference files/).
// config.php lives in src/, so the project root is dirname(__DIR__).
define('DOCI_ROOT', dirname(__DIR__));

// Files path. Resolved relative to project root, not src/.
define('FILES_PATH', getenv('FILES_PATH') ?: DOCI_ROOT . '/files');

// AI Gateway configuration
// AI provider URL (OpenAI-compatible /chat/completions endpoint).
// Empty by default -- AI features stay disabled until you point this at
// a real provider (OpenAI, OpenRouter, Together.ai, Groq, Ollama,
// llama.cpp server, or your own gateway speaking the same protocol).
define('AI_GATEWAY_URL', getenv('AI_GATEWAY_URL') ?: '');

// Application URLs (configurable for different deployments)
define('APP_URL', getenv('APP_URL') ?: 'https://doci.example.com');
define('AUTH_URL', getenv('AUTH_URL') ?: '');
define('EXTERNAL_SCRIPTS_URL', getenv('EXTERNAL_SCRIPTS_URL') ?: ''); // Empty = local scripts

/**
 * Get database connection (singleton)
 */
function get_db(): PDO {
    static $pdo = null;
    if ($pdo === null) {
        $dsn = 'pgsql:host=' . DB_HOST . ';port=' . DB_PORT . ';dbname=' . DB_NAME;
        $pdo = new PDO($dsn, DB_USER, DB_PASS, [
            PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
            PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC
        ]);
    }
    return $pdo;
}

// ========================================================================
// HTTP SECURITY HEADERS
// ========================================================================
//
// .htaccess (mod_headers) already emits these for Apache. Repeating
// them here is intentional defense-in-depth: if the container is run
// behind a different webserver, or .htaccess is overridden, or
// AllowOverride is misconfigured, the PHP layer still attaches the
// hardening headers. header() with replace=true means duplicate emission
// is a no-op (last write wins with the same value).
if (!headers_sent()) {
    header("Content-Security-Policy: default-src 'self'; "
        . "script-src 'self' 'unsafe-inline'; "
        . "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        . "font-src 'self' https://fonts.gstatic.com; "
        . "img-src 'self' data: https:; "
        . "connect-src 'self'");
    header('X-Content-Type-Options: nosniff');
    header('X-Frame-Options: DENY');
    header('Referrer-Policy: strict-origin-when-cross-origin');
    // HSTS only over HTTPS, so we don't break local-dev http://.
    if (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off') {
        header('Strict-Transport-Security: max-age=31536000; includeSubDomains');
    }
}

// Secure session configuration
if (session_status() !== PHP_SESSION_ACTIVE) {
    session_set_cookie_params([
        'lifetime' => 43200,  // 12 hours
        'path' => '/',
        'secure' => true,
        'httponly' => true,
        'samesite' => 'Lax'
    ]);
    session_start();
}

// Trust-boundary preflight. DOCI_TRUSTED_PROXIES must be a deliberate
// operator decision in production -- empty (= silently accept no proxy)
// and 0.0.0.0/0 (= trust the world) are both dangerous defaults that
// shouldn't reach a live request handler. The literal value "none"
// declares "intentionally API-key-only", which is treated as empty
// for the trust check but passes the assertion.
$_doci_env = strtolower((string) getenv('DOCI_ENV'));
$_doci_trusted_raw = trim((string) getenv('DOCI_TRUSTED_PROXIES'));
if ($_doci_env === 'production') {
    if ($_doci_trusted_raw === '') {
        error_log('[DOCI] FATAL: DOCI_TRUSTED_PROXIES must be set in production. '
            . 'Use "none" for API-key-only deployments, or a CIDR list.');
        http_response_code(500);
        die('Configuration error: DOCI_TRUSTED_PROXIES unset in production');
    }
    if (preg_match('#(^|,)\s*0\.0\.0\.0/0\s*(,|$)#', $_doci_trusted_raw)) {
        error_log('[DOCI] FATAL: DOCI_TRUSTED_PROXIES=0.0.0.0/0 is not allowed in production. '
            . 'Restrict to specific reverse-proxy CIDRs or use "none".');
        http_response_code(500);
        die('Configuration error: wildcard DOCI_TRUSTED_PROXIES in production');
    }
}
$_doci_trusted_effective = ($_doci_trusted_raw === 'none') ? '' : $_doci_trusted_raw;

// Get user info from reverse-proxy ForwardAuth header.
//
// The Remote-User header is trusted ONLY if the request originated
// from an IP that the operator explicitly allowlisted via
// DOCI_TRUSTED_PROXIES (comma-separated list of IPs and/or CIDR
// ranges, IPv4). The literal "none" explicitly declares no proxy
// trust (API-key only). Without an explicit allowlist, any
// Remote-User header is stripped before reaching the handler --
// this prevents direct-to-container clients (anyone on traefik-net
// or with the published port) from forging the header and
// authenticating as anyone.
$remoteUser = null;
$rawRemoteUser = $_SERVER['HTTP_REMOTE_USER'] ?? null;
if ($rawRemoteUser !== null) {
    $remoteAddr = $_SERVER['REMOTE_ADDR'] ?? '';
    if (doci_is_trusted_proxy($remoteAddr, $_doci_trusted_effective)) {
        // Source IP is trusted; validate header format
        // (alphanumeric, underscore, hyphen only).
        if (preg_match('/^[a-zA-Z0-9_@.+-]+$/', $rawRemoteUser)) {
            $remoteUser = $rawRemoteUser;
        } else {
            // Trusted source but malformed header value -- log + drop.
            error_log('[DOCI] WARN auth.invalid_username raw=' . substr($rawRemoteUser, 0, 50));
        }
    } else {
        // Untrusted source trying to set Remote-User -- always log,
        // always strip. (Uses error_log directly so the warning is
        // visible regardless of DOCI_LOG_LEVEL.)
        error_log('[DOCI] WARN auth.untrusted_remote_user src=' . $remoteAddr
            . ' attempted_user=' . substr($rawRemoteUser, 0, 50));
        unset($_SERVER['HTTP_REMOTE_USER']);
    }
}

// Dev-mode auto-auth.
//
// To activate the bypass two independent env vars must be set, so a
// stray DOCI_DEV_AUTO_AUTH=true in a production .env cannot open the
// door on its own:
//
//   DOCI_DEV_AUTO_AUTH=true   -- the request
//   DOCI_ENV=development      -- the assertion that we are not in prod
//
// Any other combination is a configuration error. The request is
// refused with 500 before any handler runs, never silently downgraded
// to "no auto-auth".
if (strtolower((string) getenv('DOCI_DEV_AUTO_AUTH')) === 'true') {
    if (strtolower((string) getenv('DOCI_ENV')) !== 'development') {
        error_log('[DOCI] FATAL: DOCI_DEV_AUTO_AUTH=true requires DOCI_ENV=development. Refusing to auto-authenticate.');
        http_response_code(500);
        die('Configuration error: dev auto-auth requested outside a development environment');
    }
    if ($remoteUser === null && empty($_SERVER['HTTP_X_API_KEY'])) {
        $remoteUser = 'dev';
        $_SERVER['HTTP_REMOTE_USER'] = 'dev';
    }
}

// Regenerate session ID on authentication change (prevent session fixation)
$previousUser = $_SESSION['myusername'] ?? null;
$_SESSION['myusername'] = $remoteUser ?? 'unknown';
$_SESSION['userid'] = 1;  // Single-user mode (Authum doesn't provide user ID)

if ($previousUser !== $_SESSION['myusername'] && $_SESSION['myusername'] !== 'unknown') {
    $savedCsrf = $_SESSION['csrf_token'] ?? null;
    // Diagnostic log gated on DOCI_LOG_LEVEL=DEBUG. Earlier
    // versions unconditionally error_log'd a CSRF prefix + the
    // session id on every auth change, leaking session-correlation
    // material into production logs on every request.
    //
    // This code runs before the LOGGING section below defines
    // DOCI_LOG_LEVEL / DOCI_LOG_RANK, so guard with defined().
    if (defined('DOCI_LOG_LEVEL')) {
        doci_log('auth.session_regen', [
            'prev' => $previousUser,
            'new' => $_SESSION['myusername'],
            'has_csrf' => $savedCsrf !== null,
        ], 'DEBUG');
    }
    session_regenerate_id(true);
    if ($savedCsrf) {
        $_SESSION['csrf_token'] = $savedCsrf;
    }
}

// Debug CSRF state on POST -- gated on DOCI_LOG_LEVEL=DEBUG. Earlier
// versions unconditionally error_log'd partial tokens and the session
// id, putting CSRF prefixes and SIDs into every prod log line on
// every POST request. defined() guard because doci_log() lives
// below this block.
if (($_SERVER['REQUEST_METHOD'] ?? '') === 'POST' && defined('DOCI_LOG_LEVEL')) {
    doci_log('csrf.preflight', [
        'has_request_token' => isset($_SERVER['HTTP_X_CSRF_TOKEN']) || isset($_POST['csrf_token']),
        'has_session_token' => !empty($_SESSION['csrf_token']),
    ], 'DEBUG');
}
// Note: Authorization is handled by Authum - all authenticated users have full access
// For multi-user RBAC, implement user roles in database

// ========================================================================
// TRUSTED PROXY (Remote-User source enforcement)
// ========================================================================

/**
 * Check whether $ip matches any entry in $trustedList.
 *
 * $trustedList is a comma-separated string of IPv4 addresses and/or
 * CIDR ranges (e.g. "172.20.0.0/16,10.0.0.0/8,127.0.0.1").
 * Empty list returns false. IPv6 source addresses currently return
 * false unless their textual form appears verbatim in the list.
 */
function doci_is_trusted_proxy(string $ip, string $trustedList): bool {
    if ($ip === '' || $trustedList === '') {
        return false;
    }
    foreach (explode(',', $trustedList) as $cidr) {
        $cidr = trim($cidr);
        if ($cidr !== '' && doci_ip_in_cidr($ip, $cidr)) {
            return true;
        }
    }
    return false;
}

function doci_ip_in_cidr(string $ip, string $cidr): bool {
    if ($cidr === $ip) {
        return true;
    }
    if (strpos($cidr, '/') === false) {
        return false;
    }
    [$subnet, $mask] = explode('/', $cidr, 2);
    $maskBits = (int) $mask;
    $ipLong = ip2long($ip);
    $subnetLong = ip2long($subnet);
    if ($ipLong === false || $subnetLong === false || $maskBits < 0 || $maskBits > 32) {
        return false;  // IPv4 only; IPv6 fallthrough returns false
    }
    if ($maskBits === 0) {
        return true;  // /0 matches everything (dev convenience: "0.0.0.0/0")
    }
    $maskLong = -1 << (32 - $maskBits);
    return ($ipLong & $maskLong) === ($subnetLong & $maskLong);
}

// ========================================================================
// LOGGING
// ========================================================================
//
// DOCI_LOG_LEVEL controls how chatty doci_log() is:
//
//   ERROR  - only ERROR-rank lines
//   WARN   - + WARN (default; safe for production)
//   INFO   - + per-request INFO traces (verbose; dev/short-window only)
//   DEBUG  - + DEBUG (verbose plus internal diagnostics)
//
// Security-relevant actions (auth.*, csrf.*, api.auth_*, *.delete.*,
// *.api_key_*) bypass the threshold entirely so the audit trail is
// never silenced by a low log level. See doci_log_is_always_on().

const DOCI_LOG_RANK = ['DEBUG' => 0, 'INFO' => 1, 'WARN' => 2, 'ERROR' => 3];

$_doci_log_raw = strtoupper((string) getenv('DOCI_LOG_LEVEL'));
define('DOCI_LOG_LEVEL', isset(DOCI_LOG_RANK[$_doci_log_raw]) ? $_doci_log_raw : 'WARN');
unset($_doci_log_raw);

define('DOCI_LOG_FILE', getenv('DOCI_LOG_FILE') ?: '/var/log/doci/app.log');

// Restrict DOCI_LOG_FILE to vetted directories so a hostile env-var
// value cannot redirect the log (and thereby the shell-exec'd
// `git push >> <log>` line in lib/git.php) at a writable path with
// embedded shell metacharacters. escapeshellarg() in the git push
// call already neutralises the metacharacter vector; this is the
// defense-in-depth assertion the audit asked for.
if (!preg_match('#^(/var/log/|/tmp/|/dev/stderr$|/dev/null$|/var/www/html/files/\.data/)#', DOCI_LOG_FILE)) {
    error_log('[DOCI] FATAL: DOCI_LOG_FILE must be under /var/log/, /tmp/, '
        . '/var/www/html/files/.data/, or be /dev/stderr|/dev/null. Got: ' . DOCI_LOG_FILE);
    http_response_code(500);
    die('Configuration error: DOCI_LOG_FILE outside allowed directories');
}

/**
 * Returns true for security-relevant actions that must always reach
 * the log regardless of DOCI_LOG_LEVEL: authentication events, CSRF
 * violations, delete operations, API-key usage.
 */
function doci_log_is_always_on(string $action): bool {
    return str_starts_with($action, 'auth.')
        || str_starts_with($action, 'csrf.')
        || str_starts_with($action, 'api.auth_')
        || strpos($action, '.delete') !== false
        || strpos($action, '.api_key') !== false;
}

/**
 * Log application events.
 *
 * @param string $action Action being performed (e.g. 'thread.create',
 *                       'documents.delete.start').
 * @param array  $data   Additional structured data.
 * @param string $level  'DEBUG' | 'INFO' | 'WARN' | 'ERROR'.
 *                       Lines below DOCI_LOG_LEVEL are dropped unless
 *                       doci_log_is_always_on($action) returns true.
 */
function doci_log(string $action, array $data = [], string $level = 'INFO'): void {
    $level = strtoupper($level);
    $msgRank = DOCI_LOG_RANK[$level] ?? DOCI_LOG_RANK['INFO'];
    $minRank = DOCI_LOG_RANK[DOCI_LOG_LEVEL];

    if ($msgRank < $minRank && !doci_log_is_always_on($action)) {
        return;
    }

    $timestamp = date('Y-m-d H:i:s.v');
    $username = $_SESSION['myusername'] ?? 'unknown';
    $userId = $_SESSION['userid'] ?? 0;
    $ip = $_SERVER['REMOTE_ADDR'] ?? 'unknown';
    $requestId = bin2hex(random_bytes(4));

    $logEntry = [
        'ts' => $timestamp,
        'rid' => $requestId,
        'level' => $level,
        'user' => $username,
        'uid' => $userId,
        'ip' => $ip,
        'action' => $action,
        'data' => $data
    ];

    $logLine = json_encode($logEntry, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES) . "\n";

    // Write to log file
    $logDir = dirname(DOCI_LOG_FILE);
    if (!is_dir($logDir)) {
        @mkdir($logDir, 0775, true);
    }
    @file_put_contents(DOCI_LOG_FILE, $logLine, FILE_APPEND | LOCK_EX);

    // Also write to PHP error log for immediate visibility
    error_log("[DOCI] [$level] [$username] $action: " . json_encode($data, JSON_UNESCAPED_UNICODE));
}


// ========================================================================
// HELPER FUNCTIONS
// ========================================================================

/**
 * Get the current authenticated username from headers
 */
function get_current_username(): ?string {
    return $_SESSION['myusername'] ?? null;
}

/**
 * Get the current authenticated user ID from headers
 */
function get_current_user_id(): ?int {
    return $_SESSION['userid'] ?? null;
}

/**
 * Check if user is currently authenticated
 * (Always true if request reached here - Traefik/Authum handles auth)
 */
function is_authenticated(): bool {
    return !empty($_SESSION['myusername']) && $_SESSION['myusername'] !== 'unknown';
}

/**
 * Require authentication
 * In production: handled by Traefik ForwardAuth middleware
 * In development: checks API key or basic session
 */
function require_authentication(): void {
    // If Remote-User header present (Traefik auth), user is authenticated
    if (!empty($_SERVER['HTTP_REMOTE_USER'])) {
        return;
    }

    // If API key is valid, user is authenticated
    $auth = validate_api_auth();
    if ($auth['authenticated']) {
        return;
    }

    // No authentication method succeeded
    http_response_code(401);
    header('WWW-Authenticate: Bearer realm="DOCI"');
    die('Authentication required');
}

/**
 * Generate logout URL - redirects to auth provider logout
 */
function get_logout_url(): string {
    return AUTH_URL . '/?action=logout';
}

/**
 * Get base URL for the papers system
 */
function get_base_url(): string {
    $protocol = (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off') ? 'https' : 'http';
    $host = $_SERVER['HTTP_HOST'] ?? 'localhost';
    return $protocol . '://' . $host;
}

// ========================================================================
// CSRF PROTECTION
// ========================================================================

/**
 * Generate or retrieve CSRF token for current session
 */
function get_csrf_token(): string {
    if (empty($_SESSION['csrf_token'])) {
        $_SESSION['csrf_token'] = bin2hex(random_bytes(32));
    }
    return $_SESSION['csrf_token'];
}

/**
 * Validate CSRF token from request
 * Checks both header (X-CSRF-Token) and POST parameter (csrf_token)
 */
function validate_csrf_token(): bool {
    $token = $_SERVER['HTTP_X_CSRF_TOKEN'] ?? $_POST['csrf_token'] ?? '';
    if (empty($token) || empty($_SESSION['csrf_token'])) {
        return false;
    }
    return hash_equals($_SESSION['csrf_token'], $token);
}

/**
 * Require valid CSRF token for state-changing requests
 * Call this at the start of POST/PUT/DELETE handlers
 */
function require_csrf_token(): void {
    // CSRF enforcement is ON unless explicitly disabled. Unset env
    // = enforce; the only opt-out is DOCI_CSRF_ENABLED=false for
    // API-key-only deployments that never expose the browser flow.
    if (strtolower((string) getenv('DOCI_CSRF_ENABLED')) === 'false') {
        return;
    }
    if ($_SERVER['REQUEST_METHOD'] === 'GET') {
        return;  // GET requests don't need CSRF protection
    }

    // API key auth doesn't need CSRF (no browser session to hijack)
    $auth = validate_api_auth();
    if ($auth['method'] === 'api-key' || $auth['method'] === 'api-key-file') {
        return;
    }

    // Authum-authenticated users (Remote-User header from trusted proxy) skip CSRF
    // The reverse proxy (Traefik) only sets this header for authenticated users
    if (!empty($_SERVER['HTTP_REMOTE_USER'])) {
        return;
    }

    if (!validate_csrf_token()) {
        doci_log('csrf.validation_failed', [
            'method' => $_SERVER['REQUEST_METHOD'],
            'uri' => $_SERVER['REQUEST_URI']
        ], 'WARN');
        http_response_code(403);
        header('Content-Type: application/json');
        echo json_encode(['success' => false, 'error' => 'Invalid CSRF token']);
        exit;
    }
}

// ========================================================================
// SECURE GUID GENERATION
// ========================================================================

/**
 * Generate a cryptographically secure UUID v4
 */
function generate_secure_guid(): string {
    $data = random_bytes(16);
    // Set version to 4 (random)
    $data[6] = chr(ord($data[6]) & 0x0f | 0x40);
    // Set variant to RFC 4122
    $data[8] = chr(ord($data[8]) & 0x3f | 0x80);

    return vsprintf('%s%s-%s-%s-%s-%s%s%s', str_split(bin2hex($data), 4));
}

/**
 * Generate short GUID (8 chars) for display purposes
 */
function generate_short_guid(): string {
    return bin2hex(random_bytes(4));
}

// ========================================================================
// API AUTHENTICATION
// ========================================================================

/**
 * Verify a candidate API key against a stored hash.
 *
 * Only bcrypt-formatted hashes are accepted ($2y$ / $2a$ / $2b$
 * prefix). Anything else -- including the v0.1 SHA-256 hex format --
 * returns false and logs a fatal-class WARN so the operator sees
 * the migration is overdue.
 *
 * Migrate a SHA-256 hash with:
 *   php scripts/hash-api-key.php "<your-existing-secret-key>"
 *
 * (You will need to know the original cleartext key. If it's been
 * lost, generate a fresh one with `php scripts/hash-api-key.php` and
 * distribute the new key to all clients.)
 */
function doci_verify_api_key(string $candidateKey, string $storedHash): bool {
    if (preg_match('/^\$2[ayb]\$/', $storedHash)) {
        return password_verify($candidateKey, $storedHash);
    }
    if (preg_match('/^[0-9a-f]{64}$/i', $storedHash)) {
        // Legacy v0.1 SHA-256 -- refuse + log fatal. The operator's
        // first failed request after upgrade tells them what to do.
        error_log('[DOCI] FATAL auth.api_key_sha256_unsupported '
            . 'DOCI_API_KEY_HASH is a legacy SHA-256 hash; bcrypt required since v0.2. '
            . 'Rotate with: php scripts/hash-api-key.php "<your-key>"');
    }
    return false;
}


/**
 * Validate API request authentication
 * Supports: Remote-User header (from Traefik/Authum) OR API key
 *
 * @return array ['authenticated' => bool, 'user' => string|null, 'method' => string]
 */
function validate_api_auth(): array {
    // Method 1: Remote-User header (from Traefik ForwardAuth)
    $remoteUser = $_SERVER['HTTP_REMOTE_USER'] ?? null;
    if ($remoteUser !== null && preg_match('/^[a-zA-Z0-9_@.+-]+$/', $remoteUser)) {
        return [
            'authenticated' => true,
            'user' => $remoteUser,
            'method' => 'traefik'
        ];
    }

    // Method 2: API Key header
    $apiKey = $_SERVER['HTTP_X_API_KEY'] ?? null;
    if ($apiKey !== null) {
        $storedKeyHash = (string) getenv('DOCI_API_KEY_HASH');
        if ($storedKeyHash !== '' && doci_verify_api_key($apiKey, $storedKeyHash)) {
            return [
                'authenticated' => true,
                'user' => 'api-key',
                'method' => 'api-key'
            ];
        }
        // File-based key (deprecated - use DOCI_API_KEY_HASH env var instead).
        // Lives at the project root, not src/.
        $keyFile = DOCI_ROOT . '/.api-key-hash';
        if (file_exists($keyFile)) {
            $storedKeyHash = trim(file_get_contents($keyFile));
            if ($storedKeyHash !== '' && doci_verify_api_key($apiKey, $storedKeyHash)) {
                doci_log('auth.api_key_file_deprecated', [], 'WARN');
                return [
                    'authenticated' => true,
                    'user' => 'api-key',
                    'method' => 'api-key-file'
                ];
            }
        }
    }

    return [
        'authenticated' => false,
        'user' => null,
        'method' => 'none'
    ];
}

/**
 * Require API authentication - returns 401 if not authenticated
 */
function require_api_auth(): array {
    $auth = validate_api_auth();
    if (!$auth['authenticated']) {
        doci_log('api.auth_failed', [
            'uri' => $_SERVER['REQUEST_URI'],
            'method' => $_SERVER['REQUEST_METHOD']
        ], 'WARN');
        http_response_code(401);
        header('Content-Type: application/json');
        header('WWW-Authenticate: Bearer realm="DOCI API"');
        echo json_encode([
            'success' => false,
            'error' => 'Authentication required',
            'hint' => 'Provide X-API-Key header or authenticate via Traefik'
        ]);
        exit;
    }
    return $auth;
}
