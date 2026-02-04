from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from pathlib import Path
from urllib.parse import quote

from app.config.db import close_mongo_connection, connect_to_mongo
from app.config.settings import get_settings
from app.routers import whatsapp, admin, admin_ui, paystack
from app.routers.paystack import paystack_webhook
from app.routers.admin_ui import AuthRedirectException


def create_app() -> FastAPI:
    app = FastAPI(title="PNP Lite WhatsApp Bot", version="0.1.0")

    # Exception handler for authentication redirects
    @app.exception_handler(AuthRedirectException)
    async def auth_redirect_handler(request: Request, exc: AuthRedirectException):
        """Redirect unauthenticated users to the login page."""
        next_url = quote(exc.next_url, safe="")
        return RedirectResponse(url=f"/ui/admin/login?next={next_url}", status_code=303)

    # Dependency-injected settings are reusable across routers
    settings = get_settings()
    app.state.settings = settings

    # Ensure uploads directory exists for admin-shared media
    uploads_path = Path("uploads")
    uploads_path.mkdir(exist_ok=True)
    app.mount("/uploads", StaticFiles(directory=str(uploads_path)), name="uploads")
    
    # Mount static assets
    static_path = Path("static")
    static_path.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_path)), name="static")

    @app.on_event("startup")
    async def startup_event():
        await connect_to_mongo(app, settings)

    @app.on_event("shutdown")
    async def shutdown_event():
        await close_mongo_connection(app)

    # Routers
    app.include_router(whatsapp.router, prefix="/whatsapp", tags=["whatsapp"])
    app.include_router(admin.router)
    app.include_router(admin_ui.router)
    app.include_router(paystack.router, prefix="/paystack", tags=["payments"])
    # Legacy/alternate webhook path used by Paystack dashboard
    app.add_api_route(
        "/webhook/paystack",
        paystack_webhook,
        methods=["POST"],
        tags=["payments"],
    )

    @app.get("/healthz")
    async def healthcheck():
        return {"status": "ok"}

    return app


app = create_app()
