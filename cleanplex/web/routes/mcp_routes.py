"""MCP (Model Context Protocol) endpoint — exposes Cleanplex tools to AI agents.

Implements the Streamable HTTP transport (JSON-RPC 2.0 over POST /mcp).
Configure as an MCP server with: {"url": "http://localhost:7979/mcp"}
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from ...logger import get_logger
from ... import database as db
import cleanplex.scanner as scan_mod
from ... import segment_files

logger = get_logger(__name__)
router = APIRouter(tags=["mcp"])

MCP_PROTOCOL_VERSION = "2024-11-05"

# Tool definitions with JSON Schema for each input.
_TOOLS = [
    {
        "name": "list_libraries",
        "description": (
            "List all Plex libraries known to Cleanplex with their type and how many "
            "titles have been synced for scanning."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_titles",
        "description": (
            "List synced titles in a library. Returns title metadata, scan status "
            "(pending/scanning/done/failed), segment count, and whether the title is ignored."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "library_id": {
                    "type": "string",
                    "description": "Library section ID returned by list_libraries.",
                },
            },
            "required": ["library_id"],
        },
    },
    {
        "name": "get_segments",
        "description": (
            "Get all detected content segments for a specific title, ordered by start time. "
            "Each segment has start/end timestamps in milliseconds and a label string "
            "describing what was detected."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "plex_guid": {
                    "type": "string",
                    "description": "Plex GUID of the title (e.g. plex://movie/abc123).",
                },
            },
            "required": ["plex_guid"],
        },
    },
    {
        "name": "get_scanner_status",
        "description": (
            "Get the current state of the content scanner: whether it is paused, "
            "what title is currently being scanned, total job counts by status."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "queue_scan",
        "description": (
            "Queue a title for content scanning. The title must already be synced "
            "(visible in list_titles). Its previous segments are not deleted — use "
            "delete_segments first if you want a clean rescan."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "plex_guid": {
                    "type": "string",
                    "description": "Plex GUID of the title to scan.",
                },
            },
            "required": ["plex_guid"],
        },
    },
    {
        "name": "delete_segments",
        "description": "Delete all detected segments for a title. This cannot be undone.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "plex_guid": {
                    "type": "string",
                    "description": "Plex GUID of the title whose segments should be deleted.",
                },
            },
            "required": ["plex_guid"],
        },
    },
    {
        "name": "get_settings",
        "description": (
            "Get the current Cleanplex configuration. Sensitive values (Plex token, "
            "GitHub token) are redacted."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
]


# ── JSON-RPC helpers ──────────────────────────────────────────────────────────

def _ok(request_id, result: object) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _err(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _text(value: object) -> list[dict]:
    """Wrap a value in an MCP content array (single text item)."""
    text = json.dumps(value, indent=2) if not isinstance(value, str) else value
    return [{"type": "text", "text": text}]


# ── Tool implementations ──────────────────────────────────────────────────────

async def _call_tool(name: str, args: dict) -> object:
    if name == "list_libraries":
        import cleanplex.plex_client as plex_mod
        client = plex_mod.get_client()
        sections = await client.get_library_sections()
        result = []
        for s in sections:
            jobs = await db.get_scan_jobs_by_library(s.section_id)
            result.append({
                "id": s.section_id,
                "title": s.title,
                "type": s.section_type,
                "synced_title_count": len(jobs),
            })
        return result

    if name == "list_titles":
        library_id = args.get("library_id")
        if not library_id:
            raise ValueError("library_id is required")
        jobs = await db.get_scan_jobs_by_library(library_id)
        counts = await db.get_segment_counts_for_library(library_id)
        return [
            {
                "plex_guid": j["plex_guid"],
                "title": j["title"],
                "year": j.get("year"),
                "media_type": j.get("media_type", "movie"),
                "status": j.get("status"),
                "segment_count": counts.get(j["plex_guid"], 0),
                "ignored": bool(j.get("ignored")),
            }
            for j in jobs
        ]

    if name == "get_segments":
        plex_guid = args.get("plex_guid")
        if not plex_guid:
            raise ValueError("plex_guid is required")
        segs = await db.get_segments_for_guid(plex_guid)
        return [
            {
                "id": s["id"],
                "title": s.get("title"),
                "start_ms": s["start_ms"],
                "end_ms": s["end_ms"],
                "duration_ms": s["end_ms"] - s["start_ms"],
                "confidence": s.get("confidence"),
                "labels": s.get("labels", ""),
            }
            for s in segs
        ]

    if name == "get_scanner_status":
        by_status = await db.count_scan_jobs_by_status()
        return {
            "paused": scan_mod.is_paused(),
            "current_scan_guid": scan_mod.get_current_scan(),
            "queue_size": scan_mod.get_queue_size(),
            "total_titles": sum(by_status.values()),
            "by_status": by_status,
        }

    if name == "queue_scan":
        plex_guid = args.get("plex_guid")
        if not plex_guid:
            raise ValueError("plex_guid is required")
        job = await db.get_scan_job_by_guid(plex_guid)
        if not job:
            raise ValueError(f"Title not found in scan jobs: {plex_guid}")
        await db.reset_scan_job(plex_guid)
        await scan_mod.enqueue(plex_guid)
        return {"queued": True, "title": job["title"], "plex_guid": plex_guid}

    if name == "delete_segments":
        plex_guid = args.get("plex_guid")
        if not plex_guid:
            raise ValueError("plex_guid is required")
        deleted = await segment_files.delete_segments_for_guid(plex_guid)
        return {"deleted_count": deleted, "plex_guid": plex_guid}

    if name == "get_settings":
        settings = await db.get_all_settings()
        # Redact credentials — never expose tokens through MCP.
        for key in ("plex_token", "github_token"):
            if key in settings:
                settings[key] = "***redacted***"
        return settings

    raise ValueError(f"Unknown tool: {name}")


# ── JSON-RPC dispatch ─────────────────────────────────────────────────────────

async def _dispatch(method: str, params: dict, request_id) -> dict | None:
    """Route a single JSON-RPC message. Returns None for notifications."""

    if method == "initialize":
        return _ok(request_id, {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "cleanplex", "version": "0.1.0"},
        })

    if method == "ping":
        return _ok(request_id, {})

    if method == "tools/list":
        return _ok(request_id, {"tools": _TOOLS})

    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        try:
            result = await _call_tool(name, args)
            return _ok(request_id, {"content": _text(result)})
        except Exception as exc:
            logger.debug("MCP tool %s error: %s", name, exc)
            # MCP spec: tool errors go in result with isError=true, not in the RPC error field.
            return _ok(request_id, {"content": _text(f"Error: {exc}"), "isError": True})

    # Notifications have no id and require no response.
    if request_id is None:
        return None

    return _err(request_id, -32601, f"Method not found: {method}")


async def _process_message(msg: dict) -> dict | None:
    request_id = msg.get("id")  # absent on notifications
    method = msg.get("method", "")
    params = msg.get("params") or {}
    return await _dispatch(method, params, request_id)


# ── HTTP endpoint ─────────────────────────────────────────────────────────────

@router.post("/mcp")
async def mcp_endpoint(request: Request):
    """MCP Streamable HTTP transport entry point.

    Accepts a single JSON-RPC message or a batch array.
    Always returns HTTP 200 — errors are encoded in the JSON-RPC response body.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(_err(None, -32700, "Parse error"))

    if isinstance(body, list):
        # Batch request — process each message and collect non-notification responses.
        responses = [
            resp
            for msg in body
            if isinstance(msg, dict)
            for resp in [await _process_message(msg)]
            if resp is not None
        ]
        return JSONResponse(responses if responses else None)

    resp = await _process_message(body)
    if resp is None:
        # Notification — no body in response.
        return Response(status_code=202)
    return JSONResponse(resp)
