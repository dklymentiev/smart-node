<?php

declare(strict_types=1);

namespace DOCI\Tests\Unit;

use PHPUnit\Framework\TestCase;

/**
 * Unit tests for lib/mesh.php
 *
 * Only the pure helpers — `syncDocumentToMesh` and `searchMesh` are HTTP
 * clients and belong in integration tests, not here.
 */
class MeshTest extends TestCase
{
    // ========================================================================
    // getDOCIMeshGuid()
    // ========================================================================

    public function testMeshGuidIsDeterministic(): void
    {
        $g1 = getDOCIMeshGuid('projects/readme.md');
        $g2 = getDOCIMeshGuid('projects/readme.md');
        $this->assertSame($g1, $g2);
    }

    public function testMeshGuidFormat(): void
    {
        $g = getDOCIMeshGuid('any/path.md');
        $this->assertMatchesRegularExpression('/^doc_[0-9a-f]{8}$/', $g);
    }

    public function testMeshGuidDiffersByPath(): void
    {
        $a = getDOCIMeshGuid('foo/a.md');
        $b = getDOCIMeshGuid('foo/b.md');
        $this->assertNotSame($a, $b);
    }

    public function testMeshGuidUsesMd5Prefix(): void
    {
        $path = 'docs/01-vision.md';
        $expected = 'doc_' . substr(md5($path), 0, 8);
        $this->assertSame($expected, getDOCIMeshGuid($path));
    }

    // ========================================================================
    // getMeshSkipFolders()
    // ========================================================================

    public function testSkipFoldersDefaultIsTopMonitor(): void
    {
        // Ensure env is unset for this assertion
        putenv('DOCI_MESH_SKIP_FOLDERS');
        unset($_ENV['DOCI_MESH_SKIP_FOLDERS']);
        $this->assertSame(['top-monitor'], getMeshSkipFolders());
    }

    public function testSkipFoldersReadsEnvCsv(): void
    {
        putenv('DOCI_MESH_SKIP_FOLDERS=alpha,beta,gamma');
        try {
            $this->assertSame(['alpha', 'beta', 'gamma'], getMeshSkipFolders());
        } finally {
            putenv('DOCI_MESH_SKIP_FOLDERS');
        }
    }

    public function testSkipFoldersTrimsWhitespace(): void
    {
        putenv('DOCI_MESH_SKIP_FOLDERS= one , two ,three ');
        try {
            $this->assertSame(['one', 'two', 'three'], array_values(getMeshSkipFolders()));
        } finally {
            putenv('DOCI_MESH_SKIP_FOLDERS');
        }
    }

    public function testSkipFoldersEmptyEnvUsesDefault(): void
    {
        putenv('DOCI_MESH_SKIP_FOLDERS=');
        try {
            $this->assertSame(['top-monitor'], getMeshSkipFolders());
        } finally {
            putenv('DOCI_MESH_SKIP_FOLDERS');
        }
    }
}
