"""Thin MCP tools that call the existing Reading House Action API."""

from __future__ import annotations

import contextlib
import os
from typing import Annotated, Any, Literal
from uuid import UUID

import httpx
from a2wsgi import WSGIMiddleware
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, ConfigDict, Field
from starlette.applications import Starlette
from starlette.routing import Mount

from app import app as reading_app


class ThoughtInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    book_id: UUID
    chapter_id: UUID
    scope: Literal["range", "block", "chapter"]
    mark_type: str = "thought"
    content: Annotated[str, Field(max_length=50_000)]
    block_id: str | None = None
    selected_text: str | None = None
    start_block_id: str | None = None
    start_offset: Annotated[int, Field(ge=0)] | None = None
    end_block_id: str | None = None
    end_offset: Annotated[int, Field(ge=0)] | None = None


class ThoughtUpdateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: Annotated[str, Field(max_length=50_000)] | None = None
    mark_type: str | None = None
    scope: Literal["range", "block", "chapter"] | None = None
    block_id: str | None = None
    selected_text: str | None = None
    start_block_id: str | None = None
    start_offset: Annotated[int, Field(ge=0)] | None = None
    end_block_id: str | None = None
    end_offset: Annotated[int, Field(ge=0)] | None = None


class AiProgressInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    book_id: UUID
    chapter_id: UUID | None = None
    chunk_index: Annotated[int, Field(ge=0)] | None = None
    chunk_id: str | None = None
    last_block_id: str | None = None
    last_annotation_seen: UUID | None = None
    chapter_completed: bool | None = None


reading_mcp = MCPServer("Xiaxia Reading House")


def _request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call one existing Reading API route using its Action bearer token."""
    port = os.environ.get("PORT", "10000")
    token = os.environ["ACTION_API_TOKEN"]
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=120.0) as client:
        response = client.request(
            method,
            path,
            params={key: value for key, value in (params or {}).items() if value is not None},
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
    return response.json()


def _payload(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json", by_alias=True, exclude_unset=True, exclude_none=True)


@reading_mcp.tool()
def getReadingState() -> dict[str, Any]:
    """Return the current book, chapter, and saved reading position."""
    return _request("GET", "/api/reading/state")


@reading_mcp.tool()
def listBooks(
    query: Annotated[str, Field(max_length=200)] | None = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> dict[str, Any]:
    """Search or page through lightweight book records."""
    return _request("GET", "/api/books", params={"query": query, "limit": limit, "offset": offset})


@reading_mcp.tool()
def listBookChapters(book_id: UUID) -> dict[str, Any]:
    """List stable chapter IDs and chapter order for one book."""
    return _request("GET", f"/api/books/{book_id}/chapters")


@reading_mcp.tool()
def listBookAnnotations(
    book_id: UUID,
    filter: Literal["all", "highlights", "comments", "replies"] = "all",
) -> dict[str, Any]:
    """List a book's annotations and Xiaxia Thoughts with stable anchors."""
    return _request("GET", f"/api/books/{book_id}/annotations", params={"filter": filter})


@reading_mcp.tool()
def getReadingContext(
    chapter_id: UUID | None = None,
    annotation_id: UUID | None = None,
    chunk_index: Annotated[int, Field(ge=0)] | None = None,
    include_adjacent: bool = False,
) -> dict[str, Any]:
    """Read a chapter chunk with its stable blocks and associated traces."""
    return _request(
        "GET",
        "/api/reading/context",
        params={
            "chapter_id": chapter_id,
            "annotation_id": annotation_id,
            "chunk_index": chunk_index,
            "include_adjacent": str(include_adjacent).lower(),
        },
    )


@reading_mcp.tool()
def listPendingAnnotations(
    book_id: UUID | None = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    """List pending user annotations, optionally for one book."""
    return _request("GET", "/api/annotations/pending", params={"book_id": book_id, "limit": limit})


@reading_mcp.tool()
def markAnnotationSeen(annotation_id: UUID) -> dict[str, Any]:
    """Mark a user annotation as seen and persist its reading checkpoint."""
    return _request("POST", f"/api/annotations/{annotation_id}/seen")


@reading_mcp.tool()
def replyToAnnotation(
    annotation_id: UUID,
    response: Annotated[str, Field(min_length=1, max_length=50_000)],
) -> dict[str, Any]:
    """Create or replace Xiaxia's reply to one user annotation."""
    return _request("POST", f"/api/annotations/{annotation_id}/reply", body={"response": response})


@reading_mcp.tool()
def updateAnnotationReply(
    annotation_id: UUID,
    response: Annotated[str, Field(min_length=1, max_length=50_000)],
) -> dict[str, Any]:
    """Update Xiaxia's existing reply to one user annotation."""
    return _request("PATCH", f"/api/annotations/{annotation_id}/reply", body={"response": response})


@reading_mcp.tool()
def deleteAnnotationReply(annotation_id: UUID) -> dict[str, Any]:
    """Delete Xiaxia's reply while retaining the user annotation."""
    return _request("DELETE", f"/api/annotations/{annotation_id}/reply")


@reading_mcp.tool()
def listXiaxiaThoughts(
    book_id: UUID,
    chapter_id: UUID | None = None,
    page: Annotated[int, Field(ge=1)] = 1,
    page_size: Annotated[int, Field(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    """List paginated Xiaxia Thoughts for one book and optional chapter."""
    return _request(
        "GET",
        "/api/xiaxia/thoughts",
        params={"book_id": book_id, "chapter_id": chapter_id, "page": page, "page_size": page_size},
    )


@reading_mcp.tool()
def createXiaxiaThought(
    book_id: UUID,
    chapter_id: UUID,
    scope: Literal["range", "block", "chapter"],
    content: Annotated[str, Field(max_length=50_000)],
    mark_type: str = "thought",
    block_id: str | None = None,
    selected_text: str | None = None,
    start_block_id: str | None = None,
    start_offset: Annotated[int, Field(ge=0)] | None = None,
    end_block_id: str | None = None,
    end_offset: Annotated[int, Field(ge=0)] | None = None,
) -> dict[str, Any]:
    """Create a Xiaxia Thought with the supplied reading anchor."""
    body = _payload(
        ThoughtInput(
            book_id=book_id,
            chapter_id=chapter_id,
            scope=scope,
            content=content,
            mark_type=mark_type,
            block_id=block_id,
            selected_text=selected_text,
            start_block_id=start_block_id,
            start_offset=start_offset,
            end_block_id=end_block_id,
            end_offset=end_offset,
        )
    )
    return _request("POST", "/api/xiaxia/thoughts", body=body)


@reading_mcp.tool()
def updateXiaxiaThought(
    thought_id: UUID,
    content: Annotated[str, Field(max_length=50_000)] | None = None,
    mark_type: str | None = None,
    scope: Literal["range", "block", "chapter"] | None = None,
    block_id: str | None = None,
    selected_text: str | None = None,
    start_block_id: str | None = None,
    start_offset: Annotated[int, Field(ge=0)] | None = None,
    end_block_id: str | None = None,
    end_offset: Annotated[int, Field(ge=0)] | None = None,
) -> dict[str, Any]:
    """Update supplied Xiaxia Thought fields and optionally its anchor."""
    body = _payload(
        ThoughtUpdateInput(
            content=content,
            mark_type=mark_type,
            scope=scope,
            block_id=block_id,
            selected_text=selected_text,
            start_block_id=start_block_id,
            start_offset=start_offset,
            end_block_id=end_block_id,
            end_offset=end_offset,
        )
    )
    return _request("PATCH", f"/api/xiaxia/thoughts/{thought_id}", body=body)


@reading_mcp.tool()
def deleteXiaxiaThought(thought_id: UUID) -> dict[str, Any]:
    """Delete one Xiaxia-owned Thought using the Reading API."""
    return _request("DELETE", f"/api/xiaxia/thoughts/{thought_id}")


@reading_mcp.tool()
def deleteXiaxiaThoughts(book_id: UUID, chapter_id: UUID | None = None) -> dict[str, Any]:
    """Delete Xiaxia Thoughts for a book and optional chapter."""
    body = {"book_id": str(book_id)}
    if chapter_id is not None:
        body["chapter_id"] = str(chapter_id)
    return _request("POST", "/api/xiaxia/thoughts/batch-delete", body=body)


@reading_mcp.tool()
def previewXiaxiaThoughts(
    candidates: Annotated[list[ThoughtInput], Field(min_length=1, max_length=50)],
) -> dict[str, Any]:
    """Validate Thought candidates and return their candidate IDs and anchors."""
    return _request(
        "POST",
        "/api/xiaxia/thoughts/preview",
        body={"candidates": [_payload(candidate) for candidate in candidates]},
    )


@reading_mcp.tool()
def commitXiaxiaThoughts(
    candidate_ids: Annotated[list[UUID], Field(min_length=1, max_length=50)],
) -> dict[str, Any]:
    """Commit previously validated Thought candidates by ID."""
    return _request(
        "POST",
        "/api/xiaxia/thoughts/commit",
        body={"candidate_ids": [str(candidate_id) for candidate_id in candidate_ids]},
    )


@reading_mcp.tool()
def saveAiProgress(
    book_id: UUID,
    chapter_id: UUID | None = None,
    chunk_index: Annotated[int, Field(ge=0)] | None = None,
    chunk_id: str | None = None,
    last_block_id: str | None = None,
    last_annotation_seen: UUID | None = None,
    chapter_completed: bool | None = None,
) -> dict[str, Any]:
    """Save an AI reading checkpoint using the existing validation rules."""
    body = _payload(
        AiProgressInput(
            book_id=book_id,
            chapter_id=chapter_id,
            chunk_index=chunk_index,
            chunk_id=chunk_id,
            last_block_id=last_block_id,
            last_annotation_seen=last_annotation_seen,
            chapter_completed=chapter_completed,
        )
    )
    return _request("POST", "/api/ai/progress", body=body)


@reading_mcp.tool()
def undoLastReadingAction() -> dict[str, Any]:
    """Undo Xiaxia's latest trace operation when the API permits it."""
    return _request("POST", "/api/actions/undo")


@contextlib.asynccontextmanager
async def lifespan(_app: Starlette):
    async with reading_mcp.session_manager.run():
        yield


host = os.environ.get("READING_PUBLIC_HOST", "xiaxia-reading-house.onrender.com")
transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[host, f"{host}:*", "localhost:*", "127.0.0.1:*"],
)
mcp_http_app = reading_mcp.streamable_http_app(transport_security=transport_security)
reading_wsgi_app = WSGIMiddleware(reading_app, workers=2)


async def dispatch_http(scope, receive, send):
    """Send /mcp to MCP and preserve existing Flask HTTP routes."""
    if scope["path"] == "/mcp":
        await mcp_http_app(scope, receive, send)
    else:
        await reading_wsgi_app(scope, receive, send)


app = Starlette(routes=[Mount("/", app=dispatch_http)], lifespan=lifespan)
