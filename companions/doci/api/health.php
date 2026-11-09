<?php
/**
 * Health Check Endpoint
 *
 * Returns 200 OK if service is healthy, 503 if not.
 * Used by Docker health checks and load balancers.
 */

header('Content-Type: application/json');

$status = [
    'status' => 'ok',
    'timestamp' => date('c'),
    'checks' => []
];

$healthy = true;

// Check database connection
try {
    require_once __DIR__ . '/../src/config.php';
    $pdo = get_db();
    $pdo->query('SELECT 1');
    $status['checks']['database'] = 'ok';
} catch (Exception $e) {
    $status['checks']['database'] = 'error';
    $healthy = false;
}

// Check files directory is writable
$filesPath = __DIR__ . '/../files';
if (is_dir($filesPath) && is_writable($filesPath)) {
    $status['checks']['files'] = 'ok';
} else {
    $status['checks']['files'] = 'error';
    $healthy = false;
}

// Set response code based on health
if (!$healthy) {
    http_response_code(503);
    $status['status'] = 'unhealthy';
}

echo json_encode($status, JSON_PRETTY_PRINT);
