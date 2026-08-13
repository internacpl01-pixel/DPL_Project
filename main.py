"""DPL Data Bank API — async FastAPI entry point."""
import logging
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from database import Database
from services.schema_init import create_tables, create_default_admin

from routers import auth, users, mappings, data, imports, export

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await Database.connect()
    await create_tables()
    try:
        await create_default_admin()
    except Exception:
        pass
    yield
    await Database.disconnect()


app = FastAPI(title="DPL Data Bank API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://dpl-f.vercel.app", "https://dpl-project.onrender.com"],
    # Match both production (dpl-f.vercel.app) and any Vercel preview
    # subdomain for this project (dpl-{hash}-dpl-project-{hash}.vercel.app).
    allow_origin_regex=r"^https://([a-zA-Z0-9-]+\.)*vercel\.app$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # CORS hides all but a few safelisted response headers from JS. The export
    # download reads Content-Disposition to get the server-side filename.
    expose_headers=["Content-Disposition"],
)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
if FRONTEND_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="frontend-static")


app.include_router(auth.router)
app.include_router(users.router)
app.include_router(mappings.router)
app.include_router(data.router)
app.include_router(imports.router)
app.include_router(export.router)


@app.get("/", include_in_schema=False)
async def serve_index():
    index = FRONTEND_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return {"message": "DPL Data Bank API is running."}


@app.get("/api/health", include_in_schema=False)
async def health_check():
    return {"status": "ok"}


@app.get("/frontend/", include_in_schema=False)
async def serve_frontend_index():
    index = FRONTEND_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    raise HTTPException(status_code=404, detail="Frontend not found")


@app.get("/frontend/{path:path}", include_in_schema=False)
async def serve_frontend_assets(path: str):
    file_path = (FRONTEND_DIR / path).resolve()
    try:
        file_path.relative_to(FRONTEND_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=404, detail="Not found")
    if file_path.is_file():
        return FileResponse(str(file_path))
    index = FRONTEND_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    raise HTTPException(status_code=404, detail="Not found")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=5000, reload=True)
