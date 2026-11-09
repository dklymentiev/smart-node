<?php
/**
 * DOCI - Git Operations Library
 *
 * Secure git commit and push operations with author validation.
 */

require_once __DIR__ . '/../src/config.php';

// Git configuration. The content repo is /var/www/html/files (initialised
// by docker-entrypoint.sh on first start). All operations are relative to
// that working tree -- no parent app repo, no files/ prefix.
if (!defined('GIT_REPO_ROOT')) {
    define('GIT_REPO_ROOT', __DIR__ . '/../files');
}
if (!defined('GIT_REPO_PATH')) {
    define('GIT_REPO_PATH', __DIR__ . '/../files');
}
if (!defined('GIT_FILES_PREFIX')) {
    define('GIT_FILES_PREFIX', '');
}

/**
 * Validate author name - must be alphanumeric with underscores/hyphens only
 * This prevents command injection via git --author
 *
 * @param string $author Author name to validate
 * @return bool True if valid, false otherwise
 */
function validate_git_author(string $author): bool {
    return preg_match('/^[a-zA-Z0-9_-]{1,64}$/', $author) === 1;
}

/**
 * Commit and push to git (secure version)
 *
 * Uses environment variables for author instead of --author flag to prevent
 * command injection. Author name is strictly validated.
 *
 * @param string $path File path relative to repo
 * @param string $message Commit message
 * @param string $author Author name (will be validated)
 * @return array Result with 'success' or 'error' key
 */
function git_commit_and_push(string $path, string $message, string $author): array {
    $repoPath = GIT_REPO_PATH;

    // Strict validation of author to prevent command injection
    if (!validate_git_author($author)) {
        doci_log('git.invalid_author', ['author' => substr($author, 0, 20)], 'WARN');
        $author = 'unknown';
    }

    // Construct safe email. DOCI_DOMAIN is written by documents.sh into
    // the generated compose file. On nodes with a public domain it is
    // doci.<domain>; on no-domain installs documents.sh writes "localhost"
    // so the entrypoint and git commits remain functional.
    $domain = getenv('DOCI_DOMAIN');
    if (!$domain) {
        return ['error' => 'DOCI_DOMAIN env var is required'];
    }
    $email = $author . '@' . preg_replace('/[^a-zA-Z0-9.-]/', '', $domain);

    // Change to repo directory
    $oldCwd = getcwd();
    if (!chdir($repoPath)) {
        return ['error' => 'Cannot access repository directory'];
    }

    // Apache typically forks PHP with HOME unset, so git can't find the
    // safe.directory config at /var/www/.config/git/config and aborts with
    // "dubious ownership" on bind-mounted working trees. Force HOME for
    // the duration of the git invocations.
    $oldHome = getenv('HOME');
    putenv('HOME=/var/www');

    try {
        // Stage the file
        $output = [];
        $code = 0;
        exec("git add " . escapeshellarg($path) . " 2>&1", $output, $code);
        if ($code !== 0) {
            doci_log('git.add_failed', ['path' => $path, 'output' => implode("\n", $output)], 'ERROR');
            return ['error' => 'git add failed'];
        }

        // Check if there are changes to commit
        exec("git diff --cached --quiet 2>&1", $output, $code);
        if ($code === 0) {
            return ['success' => true, 'message' => 'No changes to commit'];
        }

        // Commit using environment variables for author (safer than --author flag).
        // Set them through putenv() rather than shell-prefix syntax: dash (the
        // default /bin/sh in many container images) does not accept quoted
        // assignments like 'VAR'=value, which exec() produces from
        // escapeshellarg().
        $env = [
            'GIT_AUTHOR_NAME' => $author,
            'GIT_AUTHOR_EMAIL' => $email,
            'GIT_COMMITTER_NAME' => $author,
            'GIT_COMMITTER_EMAIL' => $email,
        ];
        $previous = [];
        foreach ($env as $key => $value) {
            $previous[$key] = getenv($key);
            putenv("$key=$value");
        }
        try {
            $output = [];
            $commitCmd = 'git commit -m ' . escapeshellarg($message) . ' 2>&1';
            exec($commitCmd, $output, $code);
        } finally {
            foreach ($previous as $key => $value) {
                if ($value === false) {
                    putenv($key);
                } else {
                    putenv("$key=$value");
                }
            }
        }

        if ($code !== 0) {
            doci_log('git.commit_failed', ['output' => implode("\n", $output)], 'ERROR');
            return ['error' => 'git commit failed'];
        }

        // Get commit hash
        $hash = trim(shell_exec('git rev-parse --short HEAD 2>/dev/null') ?? '');

        // Log the commit
        doci_log('git.commit', ['hash' => $hash, 'author' => $author, 'path' => $path]);

        // Push to origin (async, log result to file). DOCI_LOG_FILE is
        // always defined by config.php at startup and constrained to an
        // allowlist of safe directories (see config.php DOCI_LOG_FILE
        // preflight); escapeshellarg() below is the per-call shield.
        $pushCmd = sprintf(
            '(git push origin main 2>&1 || echo "[DOCI] git push FAILED") >> %s &',
            escapeshellarg(DOCI_LOG_FILE)
        );
        exec($pushCmd);

        return [
            'success' => true,
            'hash' => $hash,
            'message' => $message
        ];

    } finally {
        chdir($oldCwd);
        if ($oldHome !== false) {
            putenv('HOME=' . $oldHome);
        } else {
            putenv('HOME');
        }
    }
}

/**
 * Rename a file inside the git tree and commit + push.
 *
 * Runs `git mv old new` so the history follows the file rather than
 * showing a delete+add. Any prior `git add` staged by the caller (e.g.
 * a content update on the OLD path in the same request) is folded into
 * the same commit, keeping content-change + rename atomic for readers.
 *
 * @param string $oldPath Repo-relative path (before rename)
 * @param string $newPath Repo-relative path (after rename)
 * @param string $message Commit message
 * @param string $author  Author name (will be validated)
 * @return array Result with 'success'+'hash' or 'error' key
 */
function git_move_and_push(string $oldPath, string $newPath, string $message, string $author): array {
    $repoPath = GIT_REPO_PATH;

    if (!validate_git_author($author)) {
        doci_log('git.invalid_author', ['author' => substr($author, 0, 20)], 'WARN');
        $author = 'unknown';
    }

    $domain = getenv('DOCI_DOMAIN');
    if (!$domain) {
        return ['error' => 'DOCI_DOMAIN env var is required'];
    }
    $email = $author . '@' . preg_replace('/[^a-zA-Z0-9.-]/', '', $domain);

    $oldCwd = getcwd();
    if (!chdir($repoPath)) {
        return ['error' => 'Cannot access repository directory'];
    }

    $oldHome = getenv('HOME');
    putenv('HOME=/var/www');

    try {
        // Ensure destination directory exists inside the working tree so
        // `git mv` does not fail on a fresh subdirectory.
        $destDir = dirname($newPath);
        if ($destDir !== '' && $destDir !== '.' && !is_dir($destDir)) {
            if (!mkdir($destDir, 0775, true)) {
                return ['error' => 'Failed to create destination directory'];
            }
        }

        // `-k` skips the mv silently if the source is already gone --
        // makes the operation idempotent when a retry lands after the
        // rename already happened on a previous partial run.
        $output = [];
        $code = 0;
        exec(
            'git mv -k -- ' . escapeshellarg($oldPath) . ' ' . escapeshellarg($newPath) . ' 2>&1',
            $output,
            $code
        );
        if ($code !== 0) {
            doci_log('git.mv_failed', [
                'from' => $oldPath,
                'to' => $newPath,
                'output' => implode("\n", $output),
            ], 'ERROR');
            return ['error' => 'git mv failed: ' . implode("\n", $output)];
        }

        // Fold any pending content change on the destination into the same
        // commit (the caller may have just written to the OLD path before
        // the mv; `git mv` moves the working tree file too, so `add` on
        // the new path is what stages the fresh bytes).
        exec('git add ' . escapeshellarg($newPath) . ' 2>&1', $output, $code);

        exec('git diff --cached --quiet 2>&1', $output, $code);
        if ($code === 0) {
            // Nothing to commit -- source and target already matched.
            return ['success' => true, 'message' => 'No changes to commit'];
        }

        $env = [
            'GIT_AUTHOR_NAME' => $author,
            'GIT_AUTHOR_EMAIL' => $email,
            'GIT_COMMITTER_NAME' => $author,
            'GIT_COMMITTER_EMAIL' => $email,
        ];
        $previous = [];
        foreach ($env as $key => $value) {
            $previous[$key] = getenv($key);
            putenv("$key=$value");
        }
        try {
            $output = [];
            exec('git commit -m ' . escapeshellarg($message) . ' 2>&1', $output, $code);
        } finally {
            foreach ($previous as $key => $value) {
                if ($value === false) {
                    putenv($key);
                } else {
                    putenv("$key=$value");
                }
            }
        }

        if ($code !== 0) {
            doci_log('git.commit_failed', ['output' => implode("\n", $output)], 'ERROR');
            return ['error' => 'git commit failed'];
        }

        $hash = trim(shell_exec('git rev-parse --short HEAD 2>/dev/null') ?? '');
        doci_log('git.mv', ['hash' => $hash, 'author' => $author, 'from' => $oldPath, 'to' => $newPath]);

        // Fire-and-forget push, same as git_commit_and_push.
        $pushCmd = sprintf(
            '(git push origin main 2>&1 || echo "[DOCI] git push FAILED") >> %s &',
            escapeshellarg(DOCI_LOG_FILE)
        );
        exec($pushCmd);

        return [
            'success' => true,
            'hash' => $hash,
            'message' => $message
        ];
    } finally {
        chdir($oldCwd);
        if ($oldHome !== false) {
            putenv('HOME=' . $oldHome);
        } else {
            putenv('HOME');
        }
    }
}

/**
 * Get git commit history for a file
 *
 * @param string $path File path relative to files dir (e.g., "docs/api.md")
 * @param int $limit Maximum number of commits to return
 * @return array List of commits with hash, message, author, date
 */
function git_get_file_history(string $path, int $limit = 3): array {
    $repoRoot = GIT_REPO_ROOT;

    // Sanitize path
    $path = preg_replace('/[^a-zA-Z0-9_\-\.\/]/', '', $path);
    if (empty($path)) {
        return [];
    }

    // Add files prefix for git path
    $gitPath = GIT_FILES_PREFIX . $path;

    $oldCwd = getcwd();
    if (!chdir($repoRoot)) {
        return [];
    }

    // Set HOME for www-data to find git config
    $oldHome = getenv('HOME');
    putenv('HOME=/var/www');

    try {
        $output = [];
        $cmd = sprintf(
            'git log --oneline -n %d --format="%%h|%%s|%%an|%%ar" -- %s 2>/dev/null',
            min($limit, 10),
            escapeshellarg($gitPath)
        );
        exec($cmd, $output);

        $commits = [];
        foreach ($output as $line) {
            $parts = explode('|', $line, 4);
            if (count($parts) >= 2) {
                $commits[] = [
                    'hash' => $parts[0],
                    'message' => $parts[1],
                    'author' => $parts[2] ?? '',
                    'date' => $parts[3] ?? '',
                ];
            }
        }

        return $commits;
    } finally {
        chdir($oldCwd);
        // Restore HOME
        if ($oldHome !== false) {
            putenv('HOME=' . $oldHome);
        } else {
            putenv('HOME');
        }
    }
}

/**
 * Get Gitea commit URL base
 *
 * @return string|null Base URL for commits or null if not configured
 */
function git_get_commit_url_base(): ?string {
    // Parse from git remote
    $repoRoot = GIT_REPO_ROOT;
    $oldCwd = getcwd();

    if (!chdir($repoRoot)) {
        return null;
    }

    // Set HOME for www-data to find git config
    $oldHome = getenv('HOME');
    putenv('HOME=/var/www');

    try {
        $remote = trim(shell_exec('git remote get-url origin 2>/dev/null') ?? '');

        // Parse: https://user:token@git.example.com/owner/repo.git
        if (preg_match('#https?://[^@]+@([^/]+)/([^/]+)/([^/]+?)(?:\.git)?$#', $remote, $m)) {
            return "https://{$m[1]}/{$m[2]}/{$m[3]}/commit";
        }

        // Parse: https://git.example.com/owner/repo.git
        if (preg_match('#https?://([^/]+)/([^/]+)/([^/]+?)(?:\.git)?$#', $remote, $m)) {
            return "https://{$m[1]}/{$m[2]}/{$m[3]}/commit";
        }

        return null;
    } finally {
        chdir($oldCwd);
        // Restore HOME
        if ($oldHome !== false) {
            putenv('HOME=' . $oldHome);
        } else {
            putenv('HOME');
        }
    }
}
