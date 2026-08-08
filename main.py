"""DPL Data Bank API — async FastAPI entry point."""
import os
import logging
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from database import Database
from services.schema_init import create_tables, insert_default_mappings, create_default_admin
from services.auth import register_user

from routers import auth, users, mappings, data, imports

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await Database.connect()
    await create_tables()
    await insert_default_mappings()
    try:
        await create_default_admin()
    except Exception:
        pass
    yield
    await Database.disconnect()


app = FastAPI(title="DPL Data Bank API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
if FRONTEND_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="frontend-static")


app.include_router(auth.router)
app.include_router(users.router)
app.include_router(mappings.router)
app.include_router(data.router)
app.include_router(imports.router)


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
