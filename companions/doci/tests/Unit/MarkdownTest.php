<?php

declare(strict_types=1);

namespace DOCI\Tests\Unit;

use PHPUnit\Framework\TestCase;

/**
 * Unit tests for lib/markdown.php
 *
 * Covers the pure helpers — no Parsedown, no DB. Functions that touch the
 * DB (rewrite_links_to_guids, path_to_guid_url, repair_dead_guid_link)
 * gracefully fall back when get_db() is undefined, so they're left to
 * integration tests.
 */
class MarkdownTest extends TestCase
{
    // ========================================================================
    // extract_title()
    // ========================================================================

    public function testExtractTitlePrefersFirstH1(): void
    {
        $md = "# My Title\n\nBody text\n## Subhead";
        $this->assertSame('My Title', extract_title($md, 'fallback'));
    }

    public function testExtractTitleSkipsLowerHeadingsForFallback(): void
    {
        $md = "## Only a sub-heading\n\nBody";
        $this->assertSame('My Doc', extract_title($md, 'my-doc'));
    }

    public function testExtractTitleUsesHtmlTitleWhenNoH1(): void
    {
        $html = "<!doctype html><html><head><title>Embedded Title</title></head><body>x</body></html>";
        $this->assertSame('Embedded Title', extract_title($html, 'fallback'));
    }

    public function testExtractTitleDecodesHtmlEntitiesInTitle(): void
    {
        $html = "<html><head><title>Q&amp;A &mdash; FAQ</title></head><body></body></html>";
        $this->assertSame('Q&A — FAQ', extract_title($html, 'fallback'));
    }

    public function testExtractTitleFallsBackToHumanizedFilename(): void
    {
        $md = "No headings here, just body text.";
        $this->assertSame('My File Name', extract_title($md, 'my-file_name'));
    }

    public function testExtractTitleTrimsWhitespaceInH1(): void
    {
        $md = "#   Spaced Title   \n\nbody";
        $this->assertSame('Spaced Title', extract_title($md, 'fallback'));
    }

    // ========================================================================
    // doci_is_html_document()
    // ========================================================================

    public function testIsHtmlDocumentDetectsDoctype(): void
    {
        $this->assertTrue(doci_is_html_document("<!doctype html><html><body>x</body></html>"));
        $this->assertTrue(doci_is_html_document("<!DOCTYPE html>\n<html>...</html>"));
    }

    public function testIsHtmlDocumentDetectsHtmlTag(): void
    {
        $this->assertTrue(doci_is_html_document("<html>\n<body>x</body>\n</html>"));
        $this->assertTrue(doci_is_html_document('<html lang="en">x</html>'));
    }

    public function testIsHtmlDocumentRejectsMarkdown(): void
    {
        $this->assertFalse(doci_is_html_document("# Heading\n\nbody"));
        $this->assertFalse(doci_is_html_document("Just text"));
        $this->assertFalse(doci_is_html_document("<p>inline html only</p>"));
    }

    public function testIsHtmlDocumentSkipsBomAndWhitespace(): void
    {
        $this->assertTrue(doci_is_html_document("\xEF\xBB\xBF<!doctype html><html></html>"));
        $this->assertTrue(doci_is_html_document("   \n  <!doctype html><html></html>"));
    }

    // ========================================================================
    // find_thread_quote_in_source()
    // ========================================================================

    public function testFindQuoteMatchesPlainText(): void
    {
        $source = "Here is a sentence about cats and dogs.";
        $hit = find_thread_quote_in_source($source, "cats and dogs");
        $this->assertNotNull($hit);
        $this->assertSame("cats and dogs", $hit['raw']);
        $this->assertSame(25, $hit['start']);
    }

    public function testFindQuoteSkipsMarkdownMarkersBetweenWords(): void
    {
        // The rendered text has "important note" but source has **important** note
        $source = "Here is an **important** note for readers.";
        $hit = find_thread_quote_in_source($source, "important note");
        $this->assertNotNull($hit);
        $this->assertSame("important** note", $hit['raw']);
    }

    public function testFindQuoteReturnsNullWhenNoMatch(): void
    {
        $source = "Some unrelated text.";
        $this->assertNull(find_thread_quote_in_source($source, "missing phrase"));
    }

    public function testFindQuotePicksOccurrenceByIndex(): void
    {
        $source = "alpha beta gamma alpha beta delta";
        $first = find_thread_quote_in_source($source, "alpha beta", 0);
        $second = find_thread_quote_in_source($source, "alpha beta", 1);
        $this->assertNotNull($first);
        $this->assertNotNull($second);
        $this->assertSame(0, $first['start']);
        $this->assertSame(17, $second['start']);
    }

    // ========================================================================
    // check_thread_wrap_target()
    // ========================================================================

    public function testCheckWrapAllowsParagraphSpan(): void
    {
        $source = "First paragraph here.\n\nSecond paragraph here.";
        $start = strpos($source, "First");
        $raw = "First paragraph here.";
        $result = check_thread_wrap_target($source, $start, $raw);
        $this->assertTrue($result['ok']);
    }

    public function testCheckWrapRejectsSpanInsideFencedCode(): void
    {
        $source = "Body text.\n\n```\ninside code\nmore code\n```\n\nMore body.";
        $start = strpos($source, "inside code");
        $raw = "inside code";
        $result = check_thread_wrap_target($source, $start, $raw);
        $this->assertFalse($result['ok']);
        $this->assertStringContainsString('code block', $result['reason']);
    }

    public function testCheckWrapRejectsTableCrossingSelection(): void
    {
        $source = "Intro.\n\n| col |\n| --- |\n| val |\n\nAfter.";
        $start = 0;
        $raw = "| col |\n| --- |";
        $result = check_thread_wrap_target($source, $start, $raw);
        $this->assertFalse($result['ok']);
        $this->assertStringContainsString('table', $result['reason']);
    }

    public function testCheckWrapRejectsSingleNewlineBlockBoundary(): void
    {
        $source = "# Heading\nFirst body line\nSecond body line";
        $start = strpos($source, "Heading");
        $raw = "Heading\nFirst body line";
        $result = check_thread_wrap_target($source, $start, $raw);
        $this->assertFalse($result['ok']);
        $this->assertStringContainsString('block boundary', $result['reason']);
    }

    // ========================================================================
    // process_footnotes()
    // ========================================================================

    public function testProcessFootnotesConvertsReferenceMarkers(): void
    {
        $html = "<p>Claim with citation[^1] needed.</p>";
        $out = process_footnotes($html);
        $this->assertStringContainsString('<sup class="footnote-ref">', $out);
        $this->assertStringContainsString('href="#fn-1"', $out);
        $this->assertStringContainsString('id="fnref-1"', $out);
    }

    public function testProcessFootnotesConvertsDefinitions(): void
    {
        $html = "<p>Body text.</p><p>[^1]: Citation source.</p>";
        $out = process_footnotes($html);
        $this->assertStringContainsString('id="fn-1"', $out);
        $this->assertStringContainsString('href="#fnref-1"', $out);
    }

    public function testProcessFootnotesLeavesUnreferencedTextAlone(): void
    {
        $html = "<p>No markers here, just normal text.</p>";
        $this->assertSame($html, process_footnotes($html));
    }
}
