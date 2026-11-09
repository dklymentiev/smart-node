<?php
/**
 * PHPUnit Bootstrap
 */

// Set up minimal environment for testing
$_SESSION = [];
$_SERVER['REMOTE_ADDR'] = '127.0.0.1';

// Define constants that would normally come from config
if (!defined('DOCI_LOG_LEVEL')) {
    define('DOCI_LOG_LEVEL', 'ERROR');  // tests stay quiet
    if (!defined('DOCI_LOG_RANK')) {
        define('DOCI_LOG_RANK', ['DEBUG' => 0, 'INFO' => 1, 'WARN' => 2, 'ERROR' => 3]);
    }
}

// Load libraries with no side effects at top level
require_once __DIR__ . '/../lib/validation.php';
require_once __DIR__ . '/../lib/response.php';
require_once __DIR__ . '/../lib/mesh.php';
require_once __DIR__ . '/../lib/markdown.php';
