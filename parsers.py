"""
PDF bank statement parser.
Detects the bank and extracts normalized transaction rows.
"""

import io
import re
import logging

logger = logging.getLogger(__name__)

try:
    import pdfplumber
    PDFPLUMBER_AVAILABLE = True
except ImportError:
    PDFPLUMBER_AVAILABLE = False

try:
    from pdf2image import convert_from_path
    from PIL import Image
    PDF2IMAGE_AVAILABLE = True
except ImportError:
    PDF2IMAGE_AVAILABLE = False

try:
    import pytesseract
    TESSERACT_AVAILABLE = True
except ImportError:
    TESSERACT_AVAILABLE = False


# ── Bank detection ──────────────────────────────────────────────────────────

BANK_HDFC = "HDFC"
BANK_ICICI = "ICICI"
BANK_AXIS = "Axis"
BANK_KOTAK = "Kotak"
BANK_SBI = "SBI"
BANK_UNKNOWN = "Unknown"


def detect_bank(text: str) -> str:
    """Detect bank from the first-page text content."""
    upper = text.upper()

    if "HDFC BANK" in upper or "HDFC" in upper:
        return BANK_HDFC
    if "ICICI BANK" in upper or "ICICI" in upper:
        return BANK_ICICI
    if "AXIS BANK" in upper or "AXIS" in upper:
        return BANK_AXIS
    if "KOTAK BANK" in upper or "KOTAK" in upper or "KOTAK MAHINDRA" in upper:
        return BANK_KOTAK
    if "STATE BANK OF INDIA" in upper or "SBI" in upper:
        return BANK_SBI

    return BANK_UNKNOWN


# ── PDF text extraction ─────────────────────────────────────────────────────

def extract_text_from_pdf(file_bytes: bytes) -> str:
    """Extract all text from a PDF using pdfplumber."""
    if not PDFPLUMBER_AVAILABLE:
        raise RuntimeError("pdfplumber is not installed")

    pages_text = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            txt = page.extract_text()
            if txt:
                pages_text.append(txt)

    return "\n".join(pages_text)


def has_sufficient_text(text: str, min_chars: int = 200) -> bool:
    """Return True if the PDF has enough text to be considered text-based."""
    cleaned = re.sub(r"\s+", "", text)
    return len(cleaned) >= min_chars


def extract_text_with_ocr(file_bytes: bytes) -> str:
    """Fallback: convert PDF pages to images and run OCR."""
    if not PDF2IMAGE_AVAILABLE or not TESSERACT_AVAILABLE:
        raise RuntimeError("OCR dependencies (pdf2image + pytesseract) are not installed")

    text_parts = []

    # Write to a temp file since pdf2image needs a file path
    import tempfile, os
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp.write(file_bytes)
    tmp.close()

    try:
        images = convert_from_path(tmp.name, dpi=200)
        for img in images:
            txt = pytesseract.image_to_string(img)
            text_parts.append(txt)
    finally:
        os.unlink(tmp.name)

    return "\n".join(text_parts)


# ── Row normalization helpers ───────────────────────────────────────────────

def _clean_amount(val: str) -> str:
    """Strip currency symbols, commas, and whitespace from an amount string."""
    if not val:
        return ""
    return re.sub(r"[₹,\s]", "", val).strip()


def _parse_date(val: str) -> str:
    """Normalize a date string to ISO format (best-effort)."""
    val = val.strip()
    if not val:
        return ""
    # Already looks like YYYY-MM-DD
    if re.match(r"\d{4}-\d{2}-\d{2}", val):
        return val[:10]
    # DD/MM/YYYY or DD-MM-YYYY
    m = re.match(r"(\d{2})[/\-](\d{2})[/\-](\d{4})", val)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    # DD-MMM-YYYY (e.g. 01-Aug-2026)
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


def _normalize_row(raw: dict) -> dict:
    """Normalize a raw parsed row into the canonical master format."""
    return {
        "date": _parse_date(raw.get("date", "")),
        "description": (raw.get("description") or "").strip(),
        "withdrawal": _clean_amount(raw.get("withdrawal", "")),
        "deposits": _clean_amount(raw.get("deposits", "")),
        "balance": _clean_amount(raw.get("balance", "")),
        "reference_no": (raw.get("reference_no") or "").strip(),
    }


# ── Per-bank parsers ────────────────────────────────────────────────────────

def _parse_hdfc(text: str) -> list:
    """Parse HDFC bank statement text."""
    rows = []
    lines = text.split("\n")

    # HDFC header keywords to skip
    skip_keywords = ["HDFC BANK", "PAGE", "Statement", "Customer", "Account",
                     "Branch", "IFSC", "MICR", "Customer ID", "Nomination",
                     "Account Type", "Address", "Date:", "Generated"]

    # Amount extraction pattern
    amt_pattern = re.compile(r"([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})")

    current_date = ""
    current_desc_parts = []
    current_balance = ""
    current_ref = ""

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        # Skip known non-data lines
        if any(kw.lower() in line.lower() for kw in skip_keywords):
            i += 1
            continue

        # Try to match a data row: date + narration + amounts
        # Pattern: DD/MM/YYYY ... narration ... withdrawal deposit balance
        date_match = re.match(r"^(\d{2}/\d{2}/\d{4})", line)
        if date_match:
            # Save previous transaction if any
            if current_date and current_desc_parts:
                desc = " ".join(current_desc_parts).strip()
                rows.append({
                    "date": current_date,
                    "description": desc,
                    "withdrawal": "",
                    "deposits": "",
                    "balance": current_balance,
                    "reference_no": current_ref,
                })

            current_date = _parse_date(date_match.group(1))
            rest = line[date_match.end():].strip()

            # Try to extract amounts from the right side
            amt_match = amt_pattern.search(rest)
            if amt_match:
                # Extract amounts
                w = amt_match.group(1)
                d = amt_match.group(2)
                b = amt_match.group(3)
                desc_part = rest[:amt_match.start()].strip()
                # Determine withdrawal vs deposit by position
                # HDFC: withdrawal is on the left of deposit
                narration = desc_part
                # Check for ref no before amounts
                ref_match = re.search(r"(\d{10,})\s*$", narration)
                if ref_match:
                    current_ref = ref_match.group(1)
                    narration = narration[:ref_match.start()].strip()
                else:
                    current_ref = ""
                current_desc_parts = [narration] if narration else []
                current_balance = b
            else:
                current_desc_parts = [rest] if rest else []
                current_balance = ""
                current_ref = ""
        else:
            # Continuation line — append to description
            if current_date:
                # Check if this line has amounts (could be a continuation with amounts)
                amt_match = amt_pattern.search(line)
                if amt_match and not current_balance:
                    # This line has amounts, treat as end of current row
                    current_balance = amt_match.group(3)
                    desc_extra = line[:amt_match.start()].strip()
                    if desc_extra:
                        current_desc_parts.append(desc_extra)
                else:
                    current_desc_parts.append(line)

    # Don't forget the last row
    if current_date and current_desc_parts:
        rows.append({
            "date": current_date,
            "description": " ".join(current_desc_parts).strip(),
            "withdrawal": "",
            "deposits": "",
            "balance": current_balance,
            "reference_no": current_ref,
        })

    return rows


def _parse_icici(text: str) -> list:
    """Parse ICICI bank statement text."""
    rows = []
    lines = text.split("\n")

    skip_keywords = ["ICICI BANK", "PAGE", "Statement", "Customer", "Account",
                     "Branch", "IFSC", "MICR", "Customer ID", "Address",
                     "Account Type", "Date:", "Generated", "Nomination"]

    amt_pattern = re.compile(r"([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})")

    current_date = ""
    current_desc_parts = []
    current_withdrawal = ""
    current_deposit = ""
    current_balance = ""
    current_ref = ""

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        if any(kw.lower() in line.lower() for kw in skip_keywords):
            i += 1
            continue

        date_match = re.match(r"^(\d{2}/\d{2}/\d{4})", line)
        if date_match:
            # Save previous
            if current_date and current_desc_parts:
                rows.append({
                    "date": current_date,
                    "description": " ".join(current_desc_parts).strip(),
                    "withdrawal": current_withdrawal,
                    "deposits": current_deposit,
                    "balance": current_balance,
                    "reference_no": current_ref,
                })

            current_date = _parse_date(date_match.group(1))
            rest = line[date_match.end():].strip()
            current_desc_parts = []
            current_withdrawal = ""
            current_deposit = ""
            current_balance = ""
            current_ref = ""

            amt_match = amt_pattern.search(rest)
            if amt_match:
                current_withdrawal = amt_match.group(1)
                current_deposit = amt_match.group(2)
                current_balance = amt_match.group(3)
                desc_part = rest[:amt_match.start()].strip()
                current_desc_parts = [desc_part] if desc_part else []
                ref_match = re.search(r"(\d{10,})\s*$", desc_part)
                if ref_match:
                    current_ref = ref_match.group(1)
            else:
                current_desc_parts = [rest] if rest else []
        else:
            # Continuation
            if current_date:
                amt_match = amt_pattern.search(line)
                if amt_match and not current_balance:
                    current_balance = amt_match.group(3)
                    desc_extra = line[:amt_match.start()].strip()
                    if desc_extra:
                        current_desc_parts.append(desc_extra)
                else:
                    current_desc_parts.append(line)

    if current_date and current_desc_parts:
        rows.append({
            "date": current_date,
            "description": " ".join(current_desc_parts).strip(),
            "withdrawal": current_withdrawal,
            "deposits": current_deposit,
            "balance": current_balance,
            "reference_no": current_ref,
        })

    return rows


def _parse_axis(text: str) -> list:
    """Parse Axis Bank statement text."""
    rows = []
    lines = text.split("\n")

    skip_keywords = ["AXIS BANK", "PAGE", "Statement", "Customer", "Account",
                     "Branch", "IFSC", "MICR", "Customer ID", "Address",
                     "Account Type", "Date:", "Generated"]

    amt_pattern = re.compile(r"([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})")

    current_date = ""
    current_desc_parts = []
    current_withdrawal = ""
    current_deposit = ""
    current_balance = ""
    current_ref = ""

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        if any(kw.lower() in line.lower() for kw in skip_keywords):
            i += 1
            continue

        date_match = re.match(r"^(\d{2}-\d{2}-\d{4})", line)
        if not date_match:
            date_match = re.match(r"^(\d{2}/\d{2}/\d{4})", line)

        if date_match:
            if current_date and current_desc_parts:
                rows.append({
                    "date": current_date,
                    "description": " ".join(current_desc_parts).strip(),
                    "withdrawal": current_withdrawal,
                    "deposits": current_deposit,
                    "balance": current_balance,
                    "reference_no": current_ref,
                })

            current_date = _parse_date(date_match.group(1))
            rest = line[date_match.end():].strip()
            current_desc_parts = []
            current_withdrawal = ""
            current_deposit = ""
            current_balance = ""
            current_ref = ""

            amt_match = amt_pattern.search(rest)
            if amt_match:
                current_withdrawal = amt_match.group(1)
                current_deposit = amt_match.group(2)
                current_balance = amt_match.group(3)
                desc_part = rest[:amt_match.start()].strip()
                current_desc_parts = [desc_part] if desc_part else []
            else:
                current_desc_parts = [rest] if rest else []
        else:
            if current_date:
                amt_match = amt_pattern.search(line)
                if amt_match and not current_balance:
                    current_balance = amt_match.group(3)
                    desc_extra = line[:amt_match.start()].strip()
                    if desc_extra:
                        current_desc_parts.append(desc_extra)
                else:
                    current_desc_parts.append(line)

    if current_date and current_desc_parts:
        rows.append({
            "date": current_date,
            "description": " ".join(current_desc_parts).strip(),
            "withdrawal": current_withdrawal,
            "deposits": current_deposit,
            "balance": current_balance,
            "reference_no": current_ref,
        })

    return rows


def _parse_kotak(text: str) -> list:
    """Parse Kotak Mahindra statement text.
    Kotak uses a single 'Amount' column with +/- sign instead of separate
    withdrawal/deposit columns.
    """
    rows = []
    lines = text.split("\n")

    skip_keywords = ["KOTAK", "PAGE", "Statement", "Customer", "Account",
                     "Branch", "IFSC", "MICR", "Customer ID", "Address",
                     "Account Type", "Date:", "Generated", "MAHINDRA"]

    # Pattern: date + narration + single amount + balance
    single_amt_pattern = re.compile(r"([\d,]+\.\d{2})\s+([\d,]+\.\d{2})")

    current_date = ""
    current_desc_parts = []
    current_withdrawal = ""
    current_deposit = ""
    current_balance = ""
    current_ref = ""

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        if any(kw.lower() in line.lower() for kw in skip_keywords):
            i += 1
            continue

        date_match = re.match(r"^(\d{2}-\d{2}-\d{4})", line)
        if not date_match:
            date_match = re.match(r"^(\d{2}/\d{2}/\d{4})", line)

        if date_match:
            if current_date and current_desc_parts:
                rows.append({
                    "date": current_date,
                    "description": " ".join(current_desc_parts).strip(),
                    "withdrawal": current_withdrawal,
                    "deposits": current_deposit,
                    "balance": current_balance,
                    "reference_no": current_ref,
                })

            current_date = _parse_date(date_match.group(1))
            rest = line[date_match.end():].strip()
            current_desc_parts = []
            current_withdrawal = ""
            current_deposit = ""
            current_balance = ""
            current_ref = ""

            # Single amount column + balance
            amt_match = single_amt_pattern.search(rest)
            if amt_match:
                raw_amt = amt_match.group(1)
                current_balance = amt_match.group(2)
                desc_part = rest[:amt_match.start()].strip()

                # Determine if withdrawal or deposit based on +/- or Dr/Cr
                if raw_amt.startswith("-") or "dr" in desc_part.lower():
                    current_withdrawal = raw_amt.lstrip("-")
                else:
                    current_deposit = raw_amt

                current_desc_parts = [desc_part] if desc_part else []
            else:
                current_desc_parts = [rest] if rest else []
        else:
            if current_date:
                amt_match = single_amt_pattern.search(line)
                if amt_match and not current_balance:
                    current_balance = amt_match.group(2)
                    desc_extra = line[:amt_match.start()].strip()
                    if desc_extra:
                        current_desc_parts.append(desc_extra)
                else:
                    current_desc_parts.append(line)

    if current_date and current_desc_parts:
        rows.append({
            "date": current_date,
            "description": " ".join(current_desc_parts).strip(),
            "withdrawal": current_withdrawal,
            "deposits": current_deposit,
            "balance": current_balance,
            "reference_no": current_ref,
        })

    return rows


def _parse_sbi(text: str) -> list:
    """Parse SBI bank statement text.
    SBI has multiple formats; handle the common e-statement format.
    """
    rows = []
    lines = text.split("\n")

    skip_keywords = ["STATE BANK OF INDIA", "SBI", "PAGE", "Statement",
                     "Customer", "Account", "Branch", "IFSC", "MICR",
                     "Customer ID", "Address", "Account Type", "Date:",
                     "Generated", "Subject to", "joint account",
                     "Nomination", "Registered"]

    amt_pattern = re.compile(r"([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})")

    current_date = ""
    current_desc_parts = []
    current_withdrawal = ""
    current_deposit = ""
    current_balance = ""
    current_ref = ""

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        if any(kw.lower() in line.lower() for kw in skip_keywords):
            i += 1
            continue

        date_match = re.match(r"^(\d{2}-\d{2}-\d{4})", line)
        if not date_match:
            date_match = re.match(r"^(\d{2}/\d{2}/\d{4})", line)

        if date_match:
            if current_date and current_desc_parts:
                rows.append({
                    "date": current_date,
                    "description": " ".join(current_desc_parts).strip(),
                    "withdrawal": current_withdrawal,
                    "deposits": current_deposit,
                    "balance": current_balance,
                    "reference_no": current_ref,
                })

            current_date = _parse_date(date_match.group(1))
            rest = line[date_match.end():].strip()
            current_desc_parts = []
            current_withdrawal = ""
            current_deposit = ""
            current_balance = ""
            current_ref = ""

            amt_match = amt_pattern.search(rest)
            if amt_match:
                current_withdrawal = amt_match.group(1)
                current_deposit = amt_match.group(2)
                current_balance = amt_match.group(3)
                desc_part = rest[:amt_match.start()].strip()
                current_desc_parts = [desc_part] if desc_part else []
            else:
                current_desc_parts = [rest] if rest else []
        else:
            if current_date:
                amt_match = amt_pattern.search(line)
                if amt_match and not current_balance:
                    current_balance = amt_match.group(3)
                    desc_extra = line[:amt_match.start()].strip()
                    if desc_extra:
                        current_desc_parts.append(desc_extra)
                else:
                    current_desc_parts.append(line)

    if current_date and current_desc_parts:
        rows.append({
            "date": current_date,
            "description": " ".join(current_desc_parts).strip(),
            "withdrawal": current_withdrawal,
            "deposits": current_deposit,
            "balance": current_balance,
            "reference_no": current_ref,
        })

    return rows


# ── Generic / fallback parser ───────────────────────────────────────────────

def _parse_generic(text: str) -> list:
    """Best-effort generic parser for unknown bank formats.
    Looks for lines that start with a date and contain amount patterns.
    """
    rows = []
    lines = text.split("\n")

    amt_pattern = re.compile(
        r"([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})"
    )

    current_date = ""
    current_desc_parts = []
    current_withdrawal = ""
    current_deposit = ""
    current_balance = ""
    current_ref = ""

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        # Try multiple date formats
        date_match = (
            re.match(r"^(\d{2}/\d{2}/\d{4})", line) or
            re.match(r"^(\d{2}-\d{2}-\d{4})", line) or
            re.match(r"^(\d{4}-\d{2}-\d{2})", line) or
            re.match(r"^(\d{2}-[A-Za-z]{3}-\d{4})", line)
        )

        if date_match:
            if current_date and current_desc_parts:
                rows.append({
                    "date": current_date,
                    "description": " ".join(current_desc_parts).strip(),
                    "withdrawal": current_withdrawal,
                    "deposits": current_deposit,
                    "balance": current_balance,
                    "reference_no": current_ref,
                })

            current_date = _parse_date(date_match.group(1))
            rest = line[date_match.end():].strip()
            current_desc_parts = []
            current_withdrawal = ""
            current_deposit = ""
            current_balance = ""
            current_ref = ""

            amt_match = amt_pattern.search(rest)
            if amt_match:
                current_withdrawal = amt_match.group(1)
                current_deposit = amt_match.group(2)
                current_balance = amt_match.group(3)
                desc_part = rest[:amt_match.start()].strip()
                current_desc_parts = [desc_part] if desc_part else []
            else:
                current_desc_parts = [rest] if rest else []
        else:
            if current_date:
                amt_match = amt_pattern.search(line)
                if amt_match and not current_balance:
                    current_balance = amt_match.group(3)
                    desc_extra = line[:amt_match.start()].strip()
                    if desc_extra:
                        current_desc_parts.append(desc_extra)
                else:
                    current_desc_parts.append(line)

    if current_date and current_desc_parts:
        rows.append({
            "date": current_date,
            "description": " ".join(current_desc_parts).strip(),
            "withdrawal": current_withdrawal,
            "deposits": current_deposit,
            "balance": current_balance,
            "reference_no": current_ref,
        })

    return rows


# ── Main entry point ────────────────────────────────────────────────────────

_PARSERS = {
    BANK_HDFC: _parse_hdfc,
    BANK_ICICI: _parse_icici,
    BANK_AXIS: _parse_axis,
    BANK_KOTAK: _parse_kotak,
    BANK_SBI: _parse_sbi,
}


def parse_pdf(file_bytes: bytes) -> dict:
    """
    Parse a PDF bank statement and return normalized rows.

    Returns:
        {
            "bank": str,
            "rows": [{"date", "description", "withdrawal", "deposits", "balance", "reference_no"}, ...],
            "raw_text": str  (first 2000 chars for debugging)
        }
    """
    if not PDFPLUMBER_AVAILABLE:
        raise RuntimeError(
            "pdfplumber is not installed. Add it to requirements.txt."
        )

    # Extract text
    text = ""
    use_ocr = False

    try:
        text = extract_text_from_pdf(file_bytes)
    except Exception as e:
        logger.warning(f"pdfplumber extraction failed: {e}")
        use_ocr = True

    if not has_sufficient_text(text):
        logger.info("Insufficient text extracted, attempting OCR...")
        use_ocr = True

    if use_ocr:
        try:
            text = extract_text_with_ocr(file_bytes)
        except Exception as e:
            raise RuntimeError(
                f"Could not extract text from PDF via pdfplumber or OCR: {e}"
            )

    if not text.strip():
        raise RuntimeError(
            "PDF appears to be empty or could not be read. "
            "If it is a scanned document, ensure pytesseract and tesseract-ocr are installed."
        )

    # Detect bank
    bank = detect_bank(text)

    # Select parser
    parser_fn = _PARSERS.get(bank, _parse_generic)

    try:
        rows = parser_fn(text)
    except Exception as e:
        logger.error(f"Parser for {bank} failed: {e}", exc_info=True)
        # Fall back to generic parser
        rows = _parse_generic(text)
        bank = BANK_UNKNOWN

    # Normalize every row
    rows = [_normalize_row(r) for r in rows]

    return {
        "bank": bank,
        "rows": rows,
        "raw_text": text[:2000],
    }
