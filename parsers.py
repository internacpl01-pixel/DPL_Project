
"""
Generic PDF statement parser.
Extracts text from any PDF, detects the bank (for reporting only),
and parses transaction rows using fieldmap aliases for column mapping.
No bank-specific logic needed — master table columns are filled based
on the fieldmap table configuration.
"""

import io
import re
import logging
import time

logger = logging.getLogger(__name__)

try:
    from pypdf import PdfReader, PdfWriter
    PYPDF_AVAILABLE = True
except ImportError:
    PYPDF_AVAILABLE = False

try:
    import pdfplumber
    PDFPLUMBER_AVAILABLE = True
except ImportError:
    PDFPLUMBER_AVAILABLE = False


# ── PDF text extraction ─────────────────────────────────────────────────────

def check_pdf_protected(file_bytes: bytes) -> bool:
    """Check if a PDF is password-protected using pypdf."""
    if not PYPDF_AVAILABLE:
        return False
    try:
        reader = PdfReader(io.BytesIO(file_bytes))
        return reader.is_encrypted
    except Exception:
        return False


def decrypt_pdf(file_bytes: bytes, password: str) -> bytes:
    """Decrypt a password-protected PDF and return decrypted bytes using pypdf."""
    if not password:
        raise RuntimeError(
            "ENCRYPTED: This PDF is password-protected. "
            "Please provide the password to proceed."
        )
    if not PYPDF_AVAILABLE:
        raise RuntimeError("pypdf is required for PDF decryption")
    try:
        reader = PdfReader(io.BytesIO(file_bytes))
        result = reader.decrypt(password)
        if result == 0:
            raise RuntimeError("Incorrect password. Please try again.")
        try:
            _ = reader.pages[0].extract_text()
        except Exception:
            raise RuntimeError("Incorrect password or corrupted PDF. Please try again.")
        out = io.BytesIO()
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        writer.write(out)
        return out.getvalue()
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Failed to decrypt PDF: {e}")


def extract_text_from_pdf(file_bytes: bytes) -> str:
    """Extract all text from a PDF using pdfplumber."""
    if not PDFPLUMBER_AVAILABLE:
        raise RuntimeError("pdfplumber is not installed")
    pages_text = []
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page in pdf.pages:
                txt = page.extract_text()
                if txt:
                    pages_text.append(txt)
    except Exception as e:
        err_msg = str(e).lower()
        if "password" in err_msg or "encrypt" in err_msg or "decrypt" in err_msg:
            raise RuntimeError("ENCRYPTED: This PDF is password-protected. Please provide the password.")
        raise
    return "\n".join(pages_text)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _clean_amount(val: str) -> str:
    """Strip currency symbols, commas, and whitespace from an amount string."""
    if not val:
        return ""
    return re.sub(r"[RsINR,\s]", "", val).strip()


def _parse_date(val: str) -> str:
    """Normalize a date string to ISO format (best-effort)."""
    val = val.strip()
    if not val:
        return ""
    if re.match(r"\d{4}-\d{2}-\d{2}", val):
        return val[:10]
    m = re.match(r"(\d{2})[/\-](\d{2})[/\-](\d{4})", val)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    m = re.match(r"(\d{2})-([A-Za-z]{3})-(\d{4})", val)
    if m:
        month_map = {
            "jan": "01", "feb": "02", "mar": "03", "apr": "04",
            "may": "05", "jun": "06", "jul": "07", "aug": "08",
            "sep": "09", "oct": "10", "nov": "11", "dec": "12",
        }
        mon = month_map.get(m.group(2).lower())
        if mon:
            return f"{m.group(3)}-{mon}-{m.group(1)}"
    return val


# ── Generic parser ──────────────────────────────────────────────────────────

def parse_pdf(file_bytes: bytes, password: str = "", fieldmap_rows: list = None) -> dict:
    """
    Parse a PDF bank statement and return normalized rows.

    Works for any bank — no per-bank logic. Extracts text, finds rows
    by date patterns, and returns dicts with keys: date, description,
    withdrawal, deposits, balance, reference_no.

    The fieldmap table is used downstream to map these to master columns.

    Args:
        file_bytes: raw PDF bytes
        password: optional password for encrypted PDFs
        fieldmap_rows: list of fieldmap dicts (from get_field_mappings)

    Returns:
        {"bank": str, "rows": [...], "raw_text": str}
    """
    if not PDFPLUMBER_AVAILABLE:
        raise RuntimeError("pdfplumber is not installed. Add it to requirements.txt.")

    t0 = time.perf_counter()

    # Decrypt if needed
    if check_pdf_protected(file_bytes):
        if not password:
            raise RuntimeError(
                "ENCRYPTED: This PDF is password-protected. "
                "Please provide the password to proceed."
            )
        file_bytes = decrypt_pdf(file_bytes, password)

    t1 = time.perf_counter()

    text = extract_text_from_pdf(file_bytes)

    t2 = time.perf_counter()
    logger.info(f"[Parser] text extraction: {(t2 - t1) * 1000:.0f}ms, chars={len(text)}")

    if not text.strip():
        raise RuntimeError(
            "PDF appears to be empty or could not be read. "
            "Please upload a valid text-based PDF."
        )

    bank = _detect_bank(text)

    t3 = time.perf_counter()
    logger.info(f"[Parser] bank detection: {(t3 - t2) * 1000:.0f}ms, bank={bank}")

    rows = _parse_rows(text)

    t4 = time.perf_counter()
    logger.info(f"[Parser] row extraction: {(t4 - t3) * 1000:.0f}ms, rows={len(rows)}")
    logger.info(f"[Parser] TOTAL: {(t4 - t0) * 1000:.0f}ms")

    return {
        "bank": bank,
        "rows": rows,
        "raw_text": text[:2000],
    }


def _detect_bank(text: str) -> str:
    """Detect bank name from text for reporting only."""
    upper = text.upper()
    header = "\n".join(upper.splitlines()[:20])

    bank_keywords = {
        "Yes Bank": ["YES BANK", "YESBANK"],
        "HDFC": ["HDFC BANK"],
        "ICICI": ["ICICI BANK"],
        "Axis": ["AXIS BANK"],
        "Kotak": ["KOTAK BANK", "KOTAK MAHINDRA"],
        "SBI": ["STATE BANK OF INDIA"],
        "Bank of Maharashtra": ["BANK OF MAHARASHTRA", "MAHARASHTRA BANK"],
    }
    for bank_name, keywords in bank_keywords.items():
        for kw in keywords:
            if kw in header:
                return bank_name
    return "Unknown"


def _parse_rows(text: str) -> list:
    """
    Generic row extractor. Finds lines that start with a date pattern,
    extracts date + description + amounts from each row.
    No bank-specific logic.
    """
    lines = text.split("\n")

    # Date patterns to detect transaction rows
    date_patterns = [
        re.compile(r"^(\d{4}-\d{2}-\d{2})"),
        re.compile(r"^(\d{2}/\d{2}/\d{4})"),
        re.compile(r"^(\d{2}-\d{2}-\d{4})"),
        re.compile(r"^(\d{2}-[A-Za-z]{3}-\d{4})"),
    ]

    # Amount patterns
    amt_single = re.compile(r"(-?[\d,]+\.\d{2})")
    amt_triple = re.compile(r"([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})")

    # Keywords to skip (header/footer lines)
    skip_keywords = [
        "PAGE", "Statement", "Customer", "Account", "Branch",
        "IFSC", "MICR", "Customer ID", "Nomination", "Address",
        "Date:", "Generated", "Registered", "Subject to",
    ]

    rows = []
    current_row = None

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Skip header/footer keywords (short lines only)
        if len(line) < 50:
            if any(kw.lower() in line.lower() for kw in skip_keywords):
                continue

        # Check if this line starts with a date
        date_val = None
        date_end = 0
        for pat in date_patterns:
            m = pat.match(line)
            if m:
                date_val = m.group(1)
                date_end = m.end()
                break

        if date_val:
            # Save previous row
            if current_row and current_row.get("date"):
                rows.append(current_row)
            # Start new row
            rest = line[date_end:].strip()
            current_row = {
                "date": _parse_date(date_val),
                "description": rest,
                "withdrawal": "",
                "deposits": "",
                "balance": "",
                "reference_no": "",
            }
            # Try to extract amounts from the rest of the line
            _extract_amounts(current_row, rest, amt_single, amt_triple)
        elif current_row:
            # Continuation of previous row
            _extract_amounts(current_row, line, amt_single, amt_triple)

    # Save last row
    if current_row and current_row.get("date"):
        rows.append(current_row)

    # Clean up descriptions
    for r in rows:
        r["description"] = re.sub(r"\s+", " ", r["description"]).strip()
        # Remove the trailing amounts from description if they were captured
        r["description"] = re.sub(r"\s*[\d,]+\.\d{2}.*$", "", r["description"]).strip()

    return rows


def _extract_amounts(row: dict, text: str, amt_single, amt_triple):
    """Extract withdrawal/deposit/balance amounts from a text string."""
    # Try triple amount pattern first (withdrawal + deposit + balance on same line)
    triple = amt_triple.search(text)
    if triple:
        row["withdrawal"] = triple.group(1)
        row["deposits"] = triple.group(2)
        row["balance"] = triple.group(3)
        # Update description without the amounts
        desc_part = text[:triple.start()].strip()
        if desc_part and len(desc_part) < len(row.get("description", "")):
            row["description"] = desc_part
        return

    # Try single/double amounts
    amounts = amt_single.findall(text)
    if len(amounts) == 1:
        if not row.get("balance"):
            row["balance"] = amounts[0]
    elif len(amounts) >= 2:
        if not row.get("withdrawal"):
            row["withdrawal"] = amounts[0]
        if not row.get("deposits"):
            row["deposits"] = amounts[1]
        if len(amounts) >= 3 and not row.get("balance"):
            row["balance"] = amounts[2]

    # Extract reference number (10+ digits)
    ref_match = re.search(r"(\d{10,})\s*$", text)
    if ref_match:
        row["reference_no"] = ref_match.group(1)


# ── Main entry point (called by pdf_import.py) ──────────────────────────────

async def process_pdf_import(file_bytes: bytes, save: bool = False, password: str = "", fieldmap_rows: list = None):
    """
    Main entry point for PDF import. Extracts rows from a PDF.
    If save=True, rows are inserted into master table via append_rows_to_master.
    """
    from import_helpers import append_rows_to_master

    t_start = time.perf_counter()

    try:
        result = parse_pdf(file_bytes, password=password)
    except RuntimeError:
        raise
    except Exception as e:
        logger.error(f"[PDF] Parser failed: {e}")
        raise RuntimeError(f"PDF parsing failed: {e}")

    rows = result.get("rows", [])
    bank = result.get("bank", "Unknown")
    logger.info(f"[PDF] parse: bank={bank}, rows={len(rows)}")

    inserted_count = 0
    if save and rows:
        from database import Database
        async with Database.acquire() as conn:
            fm = fieldmap_rows or []
            inserted_count = await append_rows_to_master(conn, rows, fm)

    t_total = (time.perf_counter() - t_start) * 1000
    logger.info(f"[PDF] TOTAL: {t_total:.0f}ms")
    return {"bank": bank, "rows": rows, "row_count": len(rows), "inserted": inserted_count}
