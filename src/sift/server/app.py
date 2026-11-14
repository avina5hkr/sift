from fastapi import FastAPI

from sift.core import testhooks
from sift.core.context import AppContext
from sift.server.errors import install_error_handlers
from sift.server.routes import (
    apps,
    backup,
    browse,
    cleanup,
    delete,
    devices,
    events,
    junk,
    media,
    setup,
    slate,
    status,
    teach,
    testphone,
    vault,
)
from sift.server.security import SecurityMiddleware
from sift.server.static import mount_web


def create_app(ctx: AppContext) -> FastAPI:
    # No docs/openapi pages and no CORS middleware: only the Sift window talks to this API.
    app = FastAPI(title="Sift", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx
    app.add_middleware(SecurityMiddleware, ctx=ctx)
    install_error_handlers(app)
    app.include_router(status.router, prefix="/api")
    app.include_router(events.router, prefix="/api")
    app.include_router(devices.router, prefix="/api")
    app.include_router(backup.router, prefix="/api")
    app.include_router(setup.router, prefix="/api")
    app.include_router(vault.router, prefix="/api")
    app.include_router(cleanup.router, prefix="/api")
    app.include_router(media.router, prefix="/api")
    app.include_router(teach.router, prefix="/api")
    app.include_router(junk.router, prefix="/api")
    app.include_router(delete.router, prefix="/api")
    app.include_router(browse.router, prefix="/api")
    app.include_router(slate.router, prefix="/api")
    app.include_router(apps.router, prefix="/api")
    if testhooks.flag("SIFT_TEST_PHONE_CONTROL"):
        app.include_router(testphone.router, prefix="/api")  # the browser tests only
    mount_web(app, ctx.web_dist)
    return app
