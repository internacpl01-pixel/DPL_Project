"""PDF and Excel import router."""
import logging
from fastapi import APIRouter, Depends, File, Form, UploadFile, HTTPException, status

from dependencies import get_current_user
from services.pdf_import import process_pdf_import
from services.excel_import import process_excel_import

router = APIRouter(prefix="/api", tags=["import"])
logger = logging.getLogger(__name__)


@router.post("/import/pdf")
async def import_pdf(
    file: UploadFile = File(...),
    password: str = Form(""),
    save: bool = Form(False),
    current_user: dict = Depends(get_current_user),
):
    logger.info("[Import] PDF request, file=%s, save=%s", file.filename, save)
    if not file or not file.filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No file provided")

    filename = file.filename.lower()
    if not filename.endswith(".pdf"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only PDF files are supported")

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty")

    if len(file_bytes) > 25 * 1024 * 1024:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="File too large (max 25 MB)")

    try:
        result = await process_pdf_import(file_bytes, save=save, password=password)
    except RuntimeError as e:
        logger.error(f"PDF parse failed: {e}")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    except Exception as e:
        logger.exception("Unexpected error parsing PDF")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to parse PDF: {e}")

    return result


@router.post("/import/excel")
async def import_excel(
    file: UploadFile = File(...),
    save: bool = Form(False),
    current_user: dict = Depends(get_current_user),
):
    logger.info("[Import] Excel request, file=%s, save=%s", file.filename, save)
    if not file or not file.filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No file provided")

    filename = file.filename.lower()
    if not filename.endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                           detail="Only Excel files (.xlsx, .xls) are supported")

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty")

    if len(file_bytes) > 25 * 1024 * 1024:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                           detail="File too large (max 25 MB)")

    try:
        result = await process_excel_import(file_bytes, save=save)
    except RuntimeError as e:
        logger.error(f"Excel parse failed: {e}")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    except Exception as e:
        logger.exception("Unexpected error parsing Excel")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                           detail=f"Failed to parse Excel: {e}")

    if result.get("error"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=result["error"])

    return result
