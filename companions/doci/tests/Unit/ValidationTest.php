<?php

declare(strict_types=1);

namespace DOCI\Tests\Unit;

use PHPUnit\Framework\TestCase;

/**
 * Unit tests for lib/validation.php
 */
class ValidationTest extends TestCase
{
    // ========================================================================
    // sanitize_path() tests
    // ========================================================================

    public function testSanitizePathRemovesParentDirectoryReferences(): void
    {
        // .. segments are removed, but valid segments before/after them are kept
        // Note: this is sanitization, not path resolution
        $this->assertSame('etc/passwd', sanitize_path('../../../etc/passwd'));
        $this->assertSame('file.md', sanitize_path('../file.md'));
        $this->assertSame('docs/docs/file.md', sanitize_path('docs/../docs/file.md'));
    }

    public function testSanitizePathRemovesNullBytes(): void
    {
        $this->assertSame('file.md', sanitize_path("file\0.md"));
        $this->assertSame('docs/file.md', sanitize_path("docs/\0file.md"));
    }

    public function testSanitizePathNormalizesSlashes(): void
    {
        $this->assertSame('docs/file.md', sanitize_path('docs\\file.md'));
        $this->assertSame('docs/sub/file.md', sanitize_path('docs\\sub\\file.md'));
    }

    public function testSanitizePathRemovesLeadingTrailingSlashes(): void
    {
        $this->assertSame('docs/file.md', sanitize_path('/docs/file.md'));
        $this->assertSame('docs/file.md', sanitize_path('docs/file.md/'));
        $this->assertSame('docs/file.md', sanitize_path('/docs/file.md/'));
    }

    public function testSanitizePathAllowsValidCharacters(): void
    {
        $this->assertSame('docs/my-file_v2.md', sanitize_path('docs/my-file_v2.md'));
        $this->assertSame('2024/01/notes.md', sanitize_path('2024/01/notes.md'));
    }

    public function testSanitizePathRejectsInvalidCharacters(): void
    {
        // Entire segment with invalid chars is rejected
        $this->assertSame('docs', sanitize_path('docs/file<script>.md'));
        $this->assertSame('', sanitize_path('file with spaces.md'));
    }

    public function testSanitizePathHandlesEmptyInput(): void
    {
        $this->assertSame('', sanitize_path(''));
        $this->assertSame('', sanitize_path('/'));
        $this->assertSame('', sanitize_path('..'));
    }

    // ========================================================================
    // is_valid_guid() tests
    // ========================================================================

    public function testIsValidGuidAcceptsValidUuids(): void
    {
        $this->assertTrue(is_valid_guid('550e8400-e29b-41d4-a716-446655440000'));
        $this->assertTrue(is_valid_guid('6ba7b810-9dad-11d1-80b4-00c04fd430c8'));
        $this->assertTrue(is_valid_guid('f47ac10b-58cc-4372-a567-0e02b2c3d479'));
    }

    public function testIsValidGuidAcceptsUppercaseUuids(): void
    {
        $this->assertTrue(is_valid_guid('550E8400-E29B-41D4-A716-446655440000'));
        $this->assertTrue(is_valid_guid('F47AC10B-58CC-4372-A567-0E02B2C3D479'));
    }

    public function testIsValidGuidRejectsInvalidFormats(): void
    {
        $this->assertFalse(is_valid_guid('not-a-uuid'));
        $this->assertFalse(is_valid_guid('550e8400e29b41d4a716446655440000')); // No dashes
        $this->assertFalse(is_valid_guid('550e8400-e29b-41d4-a716')); // Too short
        $this->assertFalse(is_valid_guid('550e8400-e29b-41d4-a716-446655440000-extra')); // Too long
        $this->assertFalse(is_valid_guid('')); // Empty
        $this->assertFalse(is_valid_guid('gggggggg-gggg-gggg-gggg-gggggggggggg')); // Invalid hex
    }

    // ========================================================================
    // validate_tags() tests
    // ========================================================================

    public function testValidateTagsAcceptsValidTags(): void
    {
        $this->assertSame(['tag1', 'tag2'], validate_tags('tag1,tag2'));
        $this->assertSame(['my-tag', 'my_tag'], validate_tags('my-tag,my_tag'));
        $this->assertSame(['tag123'], validate_tags('tag123'));
    }

    public function testValidateTagsTrimsWhitespace(): void
    {
        $this->assertSame(['tag1', 'tag2'], validate_tags(' tag1 , tag2 '));
    }

    public function testValidateTagsRejectsInvalidTags(): void
    {
        $result = validate_tags('valid,invalid tag,another');
        // array_filter preserves keys, use array_values for comparison
        $this->assertSame(['valid', 'another'], array_values($result));
    }

    public function testValidateTagsRejectsLongTags(): void
    {
        $longTag = str_repeat('a', 51);
        $result = validate_tags($longTag);
        $this->assertSame([], $result);
    }

    public function testValidateTagsAcceptsArrayInput(): void
    {
        $this->assertSame(['tag1', 'tag2'], validate_tags(['tag1', 'tag2']));
    }

    public function testValidateTagsHandlesEmptyInput(): void
    {
        $this->assertSame([], validate_tags(''));
        $this->assertSame([], validate_tags([]));
    }

    // ========================================================================
    // parse_pg_array() tests
    // ========================================================================

    public function testParsePgArrayHandlesValidArrays(): void
    {
        $this->assertSame(['a', 'b', 'c'], parse_pg_array('{a,b,c}'));
        $this->assertSame(['tag1', 'tag2'], parse_pg_array('{tag1,tag2}'));
    }

    public function testParsePgArrayHandlesQuotedValues(): void
    {
        $this->assertSame(['a', 'b'], parse_pg_array('{"a","b"}'));
    }

    public function testParsePgArrayHandlesEmptyArray(): void
    {
        $this->assertSame([], parse_pg_array('{}'));
        $this->assertSame([], parse_pg_array(''));
        $this->assertSame([], parse_pg_array(null));
    }

    // ========================================================================
    // to_pg_array() tests
    // ========================================================================

    public function testToPgArrayConvertsArray(): void
    {
        $this->assertSame('{a,b,c}', to_pg_array(['a', 'b', 'c']));
        $this->assertSame('{}', to_pg_array([]));
    }

    // ========================================================================
    // path_extension() tests
    // ========================================================================

    public function testPathExtensionReturnsLowercase(): void
    {
        $this->assertSame('md', path_extension('foo.MD'));
        $this->assertSame('html', path_extension('a/b/c.HTML'));
        $this->assertSame('svg', path_extension('diagram.svg'));
    }

    public function testPathExtensionHandlesNoExtension(): void
    {
        $this->assertNull(path_extension('README'));
        $this->assertNull(path_extension('.hidden')); // leading dot only, no ext
        $this->assertNull(path_extension('folder/name'));
    }

    // ========================================================================
    // path_extension_allowed() tests
    // ========================================================================

    public function testPathExtensionAllowedDefaultsMdHtmlSvg(): void
    {
        $this->assertTrue(path_extension_allowed('foo.md'));
        $this->assertTrue(path_extension_allowed('foo.html'));
        $this->assertTrue(path_extension_allowed('foo.svg'));
        $this->assertTrue(path_extension_allowed('a/b/c.HTML'));
    }

    public function testPathExtensionAllowedRejectsUnknown(): void
    {
        $this->assertFalse(path_extension_allowed('foo.txt'));
        $this->assertFalse(path_extension_allowed('foo.pdf'));
        $this->assertFalse(path_extension_allowed('foo.exe'));
        $this->assertFalse(path_extension_allowed('README'));       // no ext
        $this->assertFalse(path_extension_allowed('folder/name'));  // no ext
    }
}
