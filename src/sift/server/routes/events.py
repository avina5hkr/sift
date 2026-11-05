from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from sift.server.deps import Ctx
from sift.server.sse import sse_stream

router = APIRouter()


@router.get("/events")
async def get_events(request: Request, ctx: Ctx) -> StreamingResponse:
    return StreamingResponse(
        sse_stream(ctx.bus, request.is_disconnected, ctx.version),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )
