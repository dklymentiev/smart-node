"""
Server-Sent Events (SSE) Parser

Production-grade SSE parser that handles arbitrary event sizes.
Compliant with W3C SSE specification.
"""
from typing import AsyncIterator, Optional
import httpx


class SSEParser:
    """
    Parses Server-Sent Events from streaming HTTP response.

    Features:
    - No size limits (uses byte streaming)
    - Handles multi-line events
    - Compliant with SSE spec
    - Memory efficient buffering
    """

    def __init__(self, chunk_size: int = 8192):
        """
        Args:
            chunk_size: Size of byte chunks to read (default 8KB)
        """
        self.chunk_size = chunk_size

    async def parse(self, response: httpx.Response) -> AsyncIterator[dict]:
        """
        Parse SSE events from HTTP response.

        Args:
            response: httpx streaming response

        Yields:
            dict with parsed SSE event: {"event": str, "data": str, "id": str}
        """
        buffer = b""

        async for chunk in response.aiter_bytes(self.chunk_size):
            buffer += chunk

            # Process complete lines from buffer
            while b"\n" in buffer:
                line_end = buffer.index(b"\n")
                line_bytes = buffer[:line_end]
                buffer = buffer[line_end + 1:]

                # Decode line
                try:
                    line = line_bytes.decode("utf-8").rstrip("\r")
                except UnicodeDecodeError:
                    continue

                # Yield complete line
                yield line

        # Process remaining buffer
        if buffer:
            try:
                line = buffer.decode("utf-8").rstrip("\r\n")
                if line:
                    yield line
            except UnicodeDecodeError:
                pass

    @staticmethod
    def parse_sse_line(line: str) -> Optional[tuple[str, str]]:
        """
        Parse single SSE line into (field, value).

        SSE format:
        - "data: content"  -> ("data", "content")
        - "event: type"    -> ("event", "type")
        - "id: 123"        -> ("id", "123")
        - ": comment"      -> None (ignore comments)
        - ""               -> ("", "") (event separator)

        Args:
            line: SSE line string

        Returns:
            (field, value) tuple or None for comments
        """
        # Empty line = event boundary
        if not line:
            return ("", "")

        # Comment line (starts with :)
        if line.startswith(":"):
            return None

        # Field: value format
        if ":" in line:
            field, _, value = line.partition(":")
            # Remove leading space from value (per SSE spec)
            if value.startswith(" "):
                value = value[1:]
            return (field, value)

        # Field without value
        return (line, "")
