from fastapi import FastAPI

from sift.core.context import AppContext
from sift.server.errors import install_error_handlers
from sift.server.routes import status
from sift.server.security import SecurityMiddleware


def create_app(ctx: AppContext) -> FastAPI:
    # No docs/openapi pages and no CORS middleware: only the Sift window talks to this API.
    app = FastAPI(title="Sift", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx
    app.add_middleware(SecurityMiddleware, ctx=ctx)
    install_error_handlers(app)
    app.include_router(status.router, prefix="/api")
    return app
