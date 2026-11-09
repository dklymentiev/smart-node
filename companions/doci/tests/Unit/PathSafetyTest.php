<?php

declare(strict_types=1);

namespace DOCI\Tests\Unit;

use PHPUnit\Framework\TestCase;

/**
 * Unit tests for validate_path_within() in lib/validation.php.
 *
 * Uses a real temporary directory so realpath() resolves; this is the
 * only realpath-dependent function in validation.php and warrants its
 * own file so the bootstrap doesn't have to fake the filesystem for the
 * pure-string tests in ValidationTest.
 */
class PathSafetyTest extends TestCase
{
    private string $baseDir;

    protected function setUp(): void
    {
        $this->baseDir = sys_get_temp_dir() . '/doci-pathtest-' . bin2hex(random_bytes(4));
        mkdir($this->baseDir);
        mkdir($this->baseDir . '/inside');
        file_put_contents($this->baseDir . '/inside/file.md', "x");
    }

    protected function tearDown(): void
    {
        @unlink($this->baseDir . '/inside/file.md');
        @rmdir($this->baseDir . '/inside');
        @rmdir($this->baseDir);
    }

    public function testAcceptsPathInsideBase(): void
    {
        $this->assertTrue(validate_path_within($this->baseDir . '/inside/file.md', $this->baseDir));
    }

    public function testAcceptsNewFileInsideExistingDir(): void
    {
        // realpath() returns false for non-existent files; the function then
        // resolves the parent directory.
        $this->assertTrue(validate_path_within($this->baseDir . '/inside/new.md', $this->baseDir));
    }

    public function testRejectsTraversalOutsideBase(): void
    {
        $outside = sys_get_temp_dir();
        $this->assertFalse(validate_path_within($outside . '/etc-passwd', $this->baseDir));
    }

    public function testRejectsParentDirEscape(): void
    {
        // ../ traversal that climbs above baseDir
        $escape = $this->baseDir . '/inside/../../escape.md';
        $this->assertFalse(validate_path_within($escape, $this->baseDir));
    }

    public function testRejectsNonExistentBaseDir(): void
    {
        $this->assertFalse(validate_path_within($this->baseDir . '/x.md', '/no/such/dir/anywhere'));
    }

    public function testRejectsNewFileWhenParentMissing(): void
    {
        $this->assertFalse(validate_path_within($this->baseDir . '/no/such/parent/file.md', $this->baseDir));
    }
}
