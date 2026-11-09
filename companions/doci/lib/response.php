<?php
/**
 * DOCI - API Response Library
 *
 * Consistent JSON response formatting for all API endpoints.
 */

/**
 * Send successful JSON response
 *
 * @param array $data Response data (will include success: true)
 * @param int $code HTTP status code (default 200)
 */
function json_success(array $data = [], int $code = 200): void {
    http_response_code($code);
    header('Content-Type: application/json');
    echo json_encode(array_merge(['success' => true], $data), JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
}

/**
 * Send error JSON response
 *
 * @param string $message Error message
 * @param int $code HTTP status code (default 400)
 * @param array $extra Additional data to include
 */
function json_error(string $message, int $code = 400, array $extra = []): void {
    http_response_code($code);
    header('Content-Type: application/json');
    echo json_encode(array_merge([
        'success' => false,
        'error' => $message
    ], $extra), JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
}

/**
 * Send error and exit
 *
 * @param string $message Error message
 * @param int $code HTTP status code
 * @param array $extra Additional data
 */
function json_error_exit(string $message, int $code = 400, array $extra = []): never {
    json_error($message, $code, $extra);
    exit;
}

/**
 * Send 404 Not Found response
 *
 * @param string $message Error message (default "Not found")
 */
function json_not_found(string $message = 'Not found'): never {
    json_error_exit($message, 404);
}

/**
 * Send 401 Unauthorized response
 *
 * @param string $message Error message
 * @param string $realm WWW-Authenticate realm
 */
function json_unauthorized(string $message = 'Authentication required', string $realm = 'DOCI API'): never {
    header('WWW-Authenticate: Bearer realm="' . $realm . '"');
    json_error_exit($message, 401, ['hint' => 'Provide X-API-Key header or authenticate via Traefik']);
}

/**
 * Send 403 Forbidden response
 *
 * @param string $message Error message
 */
function json_forbidden(string $message = 'Access denied'): never {
    json_error_exit($message, 403);
}

/**
 * Send 405 Method Not Allowed response
 *
 * @param array $allowed Allowed methods
 */
function json_method_not_allowed(array $allowed = ['GET', 'POST']): never {
    header('Allow: ' . implode(', ', $allowed));
    json_error_exit('Method not allowed', 405);
}

/**
 * Send 500 Internal Server Error response
 *
 * @param string $message Error message (keep generic for security)
 */
function json_server_error(string $message = 'Internal server error'): never {
    json_error_exit($message, 500);
}

/**
 * Send an exception as a JSON error response.
 *
 * Always logs the full exception (class + message + file:line + trace)
 * to doci_log() at ERROR with a request_id correlator. The response
 * body shape depends on DOCI_ENV:
 *
 *   production (default): {success: false, error: "Internal error",
 *                          request_id: "<8 hex>"}
 *     -- DB schema, table names, file paths, and any other exception
 *        text never reach the client.
 *
 *   development:         {success: false, error: "<full message>",
 *                          request_id: "<8 hex>", debug: {...}}
 *     -- full detail in the response for fast iteration.
 *
 * Status code defaults to 500. Pass 400/404/etc. for known
 * client errors; the exception is still logged but the client
 * sees the supplied code.
 */
function json_exception(\Throwable $e, int $code = 500, string $action = 'api.exception'): void {
    $requestId = bin2hex(random_bytes(4));

    doci_log($action, [
        'request_id' => $requestId,
        'class' => get_class($e),
        'message' => $e->getMessage(),
        'file' => $e->getFile(),
        'line' => $e->getLine(),
        'uri' => $_SERVER['REQUEST_URI'] ?? null,
        'method' => $_SERVER['REQUEST_METHOD'] ?? null,
        'trace' => $e->getTraceAsString(),
    ], 'ERROR');

    http_response_code($code);
    header('Content-Type: application/json');

    $isDev = strtolower((string) getenv('DOCI_ENV')) === 'development';
    if ($isDev) {
        echo json_encode([
            'success' => false,
            'error' => $e->getMessage(),
            'request_id' => $requestId,
            'debug' => [
                'class' => get_class($e),
                'file' => $e->getFile(),
                'line' => $e->getLine(),
            ],
        ], JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    } else {
        echo json_encode([
            'success' => false,
            'error' => 'Internal error',
            'request_id' => $requestId,
        ], JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    }
}
