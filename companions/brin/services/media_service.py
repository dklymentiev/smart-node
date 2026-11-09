"""
Media Service -- thin wrapper around brin-client-tg AI tool scripts.

Calls vision.py, transcribe.py, generate.py, download.py via subprocess,
using the brin-client-tg virtualenv for heavy dependencies.
"""

import asyncio
import json
import logging
import os
from typing import Optional

from models.media import (
    VisionRequest, VisionResult,
    TranscribeRequest, TranscribeResult,
    GenerateRequest, GenerateResult,
    DownloadRequest, DownloadResult,
)

logger = logging.getLogger(__name__)

MGMT_BOT_TOOLS_DIR = os.environ.get(
    "MGMT_BOT_TOOLS_DIR", "/opt/smart-node/brin-client-tg/tools/ai"
)
MGMT_BOT_PYTHON = os.environ.get(
    "MGMT_BOT_PYTHON", "/usr/bin/python3"
)
STORAGE_DIR = os.environ.get("STORAGE_DIR", "/srv/storage")


class MediaService:
    """Delegates to brin-client-tg CLI scripts for media processing."""

    async def recognize_image(self, req: VisionRequest) -> VisionResult:
        """Analyze image via vision.py."""
        args = [req.image_path]
        if req.model:
            args += ["--model", req.model]
        if req.prompt:
            args += ["--prompt", req.prompt]

        data = await self._run_tool("vision", args, timeout=120)

        if "error" in data:
            return VisionResult(error=data["error"])

        return VisionResult(
            description=data.get("description", ""),
            text=data.get("text", ""),
            model=data.get("model", ""),
            cost=data.get("cost"),
            time_sec=data.get("time_sec", 0),
            file_size_kb=data.get("file_size_kb", 0),
        )

    async def transcribe_audio(self, req: TranscribeRequest) -> TranscribeResult:
        """Transcribe audio via transcribe.py."""
        args = [req.audio_path]
        if req.model:
            args += ["--model", req.model]

        data = await self._run_tool("transcribe", args, timeout=180)

        if "error" in data:
            return TranscribeResult(error=data["error"])

        return TranscribeResult(
            text=data.get("text", ""),
            model=data.get("model", ""),
            time_sec=data.get("time_sec", 0),
            file_size_kb=data.get("file_size_kb", 0),
        )

    async def generate_image(self, req: GenerateRequest) -> GenerateResult:
        """Generate image via generate.py."""
        args = [req.prompt]
        if req.model:
            args += ["--model", req.model]
        args += ["--width", str(req.width), "--height", str(req.height)]
        if req.steps is not None:
            args += ["--steps", str(req.steps)]
        # Output to channel-agnostic storage
        output_dir = os.path.join(STORAGE_DIR, "generated")
        args += ["--output", output_dir]

        data = await self._run_tool("generate", args, timeout=180)

        if "error" in data:
            return GenerateResult(error=data["error"], prompt=req.prompt)

        return GenerateResult(
            image_path=data.get("image_path", ""),
            prompt=data.get("prompt", req.prompt),
            model=data.get("model", ""),
            width=data.get("width", req.width),
            height=data.get("height", req.height),
            steps=data.get("steps", 0),
            time_sec=data.get("time_sec", 0),
            cost=data.get("cost"),
        )

    async def download_video(self, req: DownloadRequest) -> DownloadResult:
        """Download video via download.py."""
        args = [req.url]
        # Output to channel-agnostic storage
        output_dir = os.path.join(STORAGE_DIR, "downloads")
        args += ["--output", output_dir]
        args += ["--max-duration", str(req.max_duration)]
        args += ["--max-size", str(req.max_filesize_mb)]

        data = await self._run_tool("download", args, timeout=300)

        if "error" in data:
            return DownloadResult(
                error=data["error"],
                url=req.url,
                platform=data.get("platform", ""),
            )

        return DownloadResult(
            file_path=data.get("file_path", ""),
            url=data.get("url", req.url),
            platform=data.get("platform", ""),
            video_id=data.get("video_id"),
            title=data.get("title"),
            duration=data.get("duration"),
            file_size_mb=data.get("file_size_mb"),
        )

    async def _run_tool(
        self, script_name: str, args: list[str], timeout: int = 120
    ) -> dict:
        """Run a brin-client-tg tool script and return parsed JSON output."""
        script_path = os.path.join(MGMT_BOT_TOOLS_DIR, f"{script_name}.py")
        cmd = [MGMT_BOT_PYTHON, script_path] + args

        # Inherit env + inject STORAGE_DIR
        env = os.environ.copy()
        env["STORAGE_DIR"] = STORAGE_DIR

        logger.info(
            f"Running tool: {script_name}",
            extra={"action": f"media_{script_name}", "status": "start"},
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )

            if proc.returncode != 0:
                err_msg = stderr.decode().strip() or f"{script_name} exited with code {proc.returncode}"
                logger.error(
                    f"Tool {script_name} failed: {err_msg}",
                    extra={"action": f"media_{script_name}", "status": "error"},
                )
                # Try to parse stdout as JSON error
                try:
                    return json.loads(stdout.decode())
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return {"error": err_msg[:500]}

            result = json.loads(stdout.decode())
            logger.info(
                f"Tool {script_name} completed",
                extra={"action": f"media_{script_name}", "status": "ok"},
            )
            return result

        except asyncio.TimeoutError:
            logger.error(
                f"Tool {script_name} timed out after {timeout}s",
                extra={"action": f"media_{script_name}", "status": "timeout"},
            )
            return {"error": f"{script_name} timed out after {timeout}s"}
        except json.JSONDecodeError as e:
            logger.error(
                f"Tool {script_name} returned invalid JSON: {e}",
                extra={"action": f"media_{script_name}", "status": "error"},
            )
            return {"error": f"Invalid JSON from {script_name}: {str(e)[:200]}"}
        except Exception as e:
            logger.error(
                f"Tool {script_name} error: {e}",
                extra={"action": f"media_{script_name}", "status": "error"},
            )
            return {"error": f"{script_name}: {str(e)[:300]}"}
