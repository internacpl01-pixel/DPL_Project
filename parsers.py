"""
PDF bank statement parser.
Detects the bank and extracts normalized transaction rows.
"""

import io
import re
import logging
import time
from collections import defaultdict

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


# ── Bank detection ──────────────────────────────────────────────────────────

BANK_HDFC = "HDFC"
BANK_ICICI = "ICICI"
BANK_AXIS = "Axis"
BANK_KOTAK = "Kotak"
BANK_SBI = "SBI"
BANK_YES = "Yes Bank"
BANK_UNKNOWN = "Unknown"


def detect_bank(text: str) -> str:
    """Detect bank from the first-page text content."""
    upper = text.upper()

    header = "\n".join(upper.splitlines()[:15])

    if "YESB" in upper:
        return BANK_YES
    if "YES BANK" in header or "YESBANK" in header:
        return BANK_YES

    if "HDFC BANK" in header or "HDFC" in header:
        return BANK_HDFC
    if "ICICI BANK" in header or "ICICI" in header:
        return BANK_ICICI
    if "AXIS BANK" in header or "AXIS" in header:
        return BANK_AXIS
    if "KOTAK BANK" in header or "KOTAK" in header or "KOTAK MAHINDRA" in header:
        return BANK_KOTAK
    if "STATE BANK OF INDIA" in header or "SBI" in header:
        return BANK_SBI

    return BANK_UNKNOWN


# ── PDF text extraction ─────────────────────────────────────────────────────

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


def check_pdf_protected(file_bytes: bytes) -> bool:
    """Check if a PDF is password-protected using pypdf."""
    if PYPDF_AVAILABLE:
        try:
            reader = PdfReader(io.BytesIO(file_bytes))
            return reader.is_encrypted
        except Exception:
            pass
    # Fallback: check for encryption in raw PDF header
    header = file_bytes[:20].lower()
    if b"encrypt" in header or b"/filter" in header:
        return True
    return False


def decrypt_pdf(file_bytes: bytes, password: str) -> bytes:
    """Decrypt a password-protected PDF and return decrypted bytes using pypdf."""
    if not password:
        raise ValueError("Password is required for encrypted PDF")

    if not PYPDF_AVAILABLE:
        raise RuntimeError("pypdf is required for PDF decryption")

    try:
        reader = PdfReader(io.BytesIO(file_bytes))
        result = reader.decrypt(password)
        if result == 0:
            raise RuntimeError("Incorrect password. Please try again.")
        # Verify we can actually read content
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

    skip_keywords = ["HDFC BANK", "PAGE", "Statement", "Customer", "Account",
                     "Branch", "IFSC", "MICR", "Customer ID", "Nomination",
                     "Account Type", "Address", "Date:", "Generated"]

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
            if current_date and current_desc_parts:
                desc = " ".join(current_desc_parts).strip()
                rows.append({
                    "date": current_date,
                    "description": desc,
                    "withdrawal": current_withdrawal,
                    "deposits": current_deposit,
                    "balance": current_balance,
                    "reference_no": current_ref,
                })

            current_date = _parse_date(date_match.group(1))
            rest = line[date_match.end():].strip()

            amt_match = amt_pattern.search(rest)
            if amt_match:
                current_withdrawal = amt_match.group(1)
                current_deposit = amt_match.group(2)
                current_balance = amt_match.group(3)
                desc_part = rest[:amt_match.start()].strip()
                narration = desc_part
                ref_match = re.search(r"(\d{10,})\s*$", narration)
                if ref_match:
                    current_ref = ref_match.group(1)
                    narration = narration[:ref_match.start()].strip()
                else:
                    current_ref = ""
                current_desc_parts = [narration] if narration else []
            else:
                current_desc_parts = [rest] if rest else []
                current_withdrawal = ""
                current_deposit = ""
                current_balance = ""
                current_ref = ""
        else:
            if current_date:
                amt_match = amt_pattern.search(line)
                if amt_match and not current_balance:
                    current_withdrawal = amt_match.group(1)
                    current_deposit = amt_match.group(2)
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
    """Parse Kotak Mahindra statement text."""
    rows = []
    lines = text.split("\n")

    skip_keywords = ["KOTAK", "PAGE", "Statement", "Customer", "Account",
                     "Branch", "IFSC", "MICR", "Customer ID", "Address",
                     "Account Type", "Date:", "Generated", "MAHINDRA"]

    single_amt_pattern = re.compile(r"(-?[\d,]+\.\d{2})\s+([\d,]+\.\d{2})")

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

            amt_match = single_amt_pattern.search(rest)
            if amt_match:
                raw_amt = amt_match.group(1)
                current_balance = amt_match.group(2)
                desc_part = rest[:amt_match.start()].strip()

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
    """Parse SBI bank statement text."""
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


# ── YES Bank parser ────────────────────────────────────────────────────────

def _parse_yesbank(text: str) -> list:
    """Parse YES Bank statement text."""
    rows = _parse_yesbank_coordinate_from_text(text)
    if not rows:
        rows = _parse_yesbank_text_fallback(text)
    return rows


def _parse_yesbank_text_fallback(text: str) -> list:
    """Text-based YES Bank fallback parser for when coordinate extraction fails."""
    rows = []
    lines = text.split("\n")

    skip_keywords = ["YES BANK", "PAGE", "Statement", "Customer", "Account",
                     "Branch", "IFSC", "MICR", "Customer ID", "Nomination",
                     "Account Type", "Address", "Date:", "Generated",
                     "transaction", "date", "value", "description",
                     "reference", "number", "withdrawals", "deposits",
                     "running", "balance", "particulars", "chq",
                     "srno", "no", "type", "instrument"]

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
            if len(line) < 30:
                i += 1
                continue

        date_match = re.match(r"^(\d{4}-\d{2}-\d{2})", line)
        if not date_match:
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
                    current_withdrawal = amt_match.group(1)
                    current_deposit = amt_match.group(2)
                    current_balance = amt_match.group(3)
                    desc_extra = line[:amt_match.start()].strip()
                    if desc_extra:
                        current_desc_parts.append(desc_extra)
                else:
                    if not any(kw.lower() in line.lower() for kw in skip_keywords):
                        current_desc_parts.append(line)

        i += 1

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


def _classify_col(x0: float) -> str:
    """Classify a word into a YES Bank table column based on x position."""
    if x0 < 80:
        return "txn_date"
    elif x0 < 125:
        return "value_date"
    elif x0 < 257:
        return "description"
    elif x0 < 335:
        return "reference"
    elif x0 < 420:
        return "withdrawal"
    elif x0 < 490:
        return "deposits"
    else:
        return "balance"


def _parse_yesbank_coords(file_bytes: bytes) -> list:
    """Coordinate-based YES Bank parser using pdfplumber word coordinates."""
    if not PDFPLUMBER_AVAILABLE:
        raise RuntimeError("pdfplumber is required for YES Bank coordinate parsing")

    import pdfplumber

    _TX_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
    _Y_TOL = 5  # points tolerance for grouping words into rows

    rows = []
    current_txn = None

    def flush():
        nonlocal current_txn
        if current_txn and current_txn.get("date"):
            rows.append(current_txn)
        current_txn = None

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            words = page.extract_words() or []
            if not words:
                continue

            # Sort by top (y), then x0
            words.sort(key=lambda w: (round(float(w["top"]), 1), float(w["x0"])))

            # Cluster into rows by y-position
            line_groups = []
            for w in words:
                y = round(float(w["top"]), 1)
                placed = False
                for grp in line_groups:
                    if abs(grp[0] - y) <= _Y_TOL:
                        grp[1].append(w)
                        placed = True
                        break
                if not placed:
                    line_groups.append([y, [w]])

            # Sort each group's words by x0
            for grp in line_groups:
                grp[1].sort(key=lambda w: float(w["x0"]))

            for _, line_words in line_groups:
                col_words = defaultdict(list)
                for w in line_words:
                    col = _classify_col(float(w["x0"]))
                    col_words[col].append(w["text"])
                col_texts = {k: " ".join(v) for k, v in col_words.items()}

                raw_dates = col_texts.get("txn_date", "")
                date_parts = raw_dates.split()
                txn_date = date_parts[0] if date_parts else ""
                val_date = date_parts[1] if len(date_parts) > 1 else col_texts.get("value_date", "")

                is_txn_start = _TX_DATE_RE.match(txn_date) and _TX_DATE_RE.match(val_date)

                if is_txn_start:
                    flush()
                    current_txn = {
                        "date": txn_date,
                        "value_date": val_date.strip(),
                        "description": col_texts.get("description", "").strip(),
                        "reference_no": col_texts.get("reference", "").strip(),
                        "withdrawal": col_texts.get("withdrawal", "").strip(),
                        "deposits": col_texts.get("deposits", "").strip(),
                        "balance": col_texts.get("balance", "").strip(),
                    }
                elif current_txn:
                    desc = col_texts.get("description", "").strip()
                    if desc:
                        if current_txn["description"]:
                            current_txn["description"] += " " + desc
                        else:
                            current_txn["description"] = desc
                    for ck, tk in (
                        ("reference", "reference_no"),
                        ("withdrawal", "withdrawal"),
                        ("deposits", "deposits"),
                        ("balance", "balance"),
                    ):
                        if not current_txn[tk] and col_texts.get(ck):
                            current_txn[tk] = col_texts[ck].strip()

    flush()
    return rows


_AMOUNT_RE = re.compile(r'^[\d,]+(?:\.\d+)?$')
_REF_RE = re.compile(r'^(?:YES\w{1,3}\d+|\d{3,5})$', re.IGNORECASE)


def _parse_yesbank_coordinate_from_text(text: str) -> list:
    """Coordinate-based YES Bank parser using word positions from pdfplumber text."""
    rows = []

    # Header words to skip (from the transaction table header row)
    header_keywords = {
        "transaction", "date", "value", "description",
        "reference", "number", "withdrawals", "deposits",
        "running", "balance", "date", " particulars", "chq",
        "page", "srno", "no", "type", "instrument"
    }

    # Metadata keywords to skip (account info, etc.)
    metadata_keywords = {
        "statement of account", "statement type", "customer name",
        "branch name", "address line", "mobile no", "email", "cust id",
        "ifsc code", "micr code", "transaction details", "primary holder",
        "nominee details", "account status", "joint holder", "product name",
        "currency", "private limited", "in cirp", "your branch details",
        "account number", "for your account", "dwarkadhis", "projects",
    }

    def is_metadata_line(line_lower: str) -> bool:
        for kw in metadata_keywords:
            if kw in line_lower:
                return True
        return False

    def is_amount(val: str) -> bool:
        return bool(_AMOUNT_RE.match(val.strip()))

    def is_ref(val: str) -> bool:
        return bool(_REF_RE.match(val.strip()))

    # Group words by line (y-position clustering)
    lines_raw = text.split("\n")
    word_groups = []  # list of (y_center, [words])

    for line in lines_raw:
        line_stripped = line.strip()
        if not line_stripped:
            continue

        line_lower = line_stripped.lower()

        # Skip metadata lines
        if is_metadata_line(line_lower):
            continue

        # Skip header lines
        if any(kw in line_lower for kw in header_keywords):
            # But only if the line IS just the header (single-line header words)
            # pdfplumber puts each header word on its own line sometimes
            # Check if this looks like a header row word
            if len(line_stripped) < 25:
                continue

        # Split line into tab-separated words
        parts = [p.strip() for p in line_stripped.split("\t") if p.strip()]
        if not parts:
            continue

        # Calculate y-center from first word's context
        # Since we're working from plain text, we'll group by line content
        # Check if the line starts with a date pattern
        date_match = re.match(r'^(\d{4}-\d{2}-\d{2})\s+(\d{4}-\d{2}-\d{2})', line_stripped)

        if date_match:
            # This is a transaction start line
            word_groups.append((line_stripped, "transaction", date_match.group(1)))
        elif re.match(r'^\d{4}-\d{2}-\d{2}$', line_stripped):
            # Bare date line (second line of a transaction)
            word_groups.append((line_stripped, "date_only", line_stripped))
        else:
            # Check if this looks like a continuation line
            # (description continuation or amounts line)
            # Heuristic: if it has tabs, it's likely amounts
            if "\t" in line_stripped:
                parts = [p.strip() for p in line_stripped.split("\t") if p.strip()]
                if len(parts) >= 2 and all(is_amount(p) for p in parts):
                    word_groups.append((line_stripped, "amounts", ""))
                else:
                    word_groups.append((line_stripped, "continuation", ""))
            else:
                # Single word or short line — could be description continuation
                # or a reference/amount in the table
                word_groups.append((line_stripped, "continuation", ""))

    # Now process word groups into transactions
    transactions = []
    current_txn = None

    def flush_txn():
        nonlocal current_txn
        if current_txn and current_txn.get("date"):
            transactions.append(current_txn)
        current_txn = None

    for group_text, group_type, date_val in word_groups:
        if group_type == "transaction":
            # Start a new transaction
            flush_txn()
            parts = [p.strip() for p in group_text.split("\t") if p.strip()]

            # Parse the line: date1[tab]date2[tab]desc_parts...[tab]ref_or_amounts
            date1 = date_val  # already extracted
            rest = group_text[group_text.index(date1) + len(date1):].strip()
            parts = [p.strip() for p in rest.split("\t") if p.strip()]

            # Find value date (second date)
            value_date = ""
            desc_start_idx = 0
            for i, p in enumerate(parts):
                if re.match(r'^\d{4}-\d{2}-\d{2}$', p):
                    value_date = p
                    desc_start_idx = i + 1
                else:
                    break

            # From remaining parts, classify by position
            remaining = parts[desc_start_idx:] if desc_start_idx < len(parts) else []

            description_parts = []
            reference_no = ""
            withdrawal = ""
            deposit = ""
            balance = ""

            # Walk through remaining parts and assign to columns
            # Based on the PDF layout: desc (left), ref (mid-left), withdrawal, deposit, balance
            col_idx = 0
            for part in remaining:
                if col_idx == 0:
                    # Description column (can span multiple parts with tabs)
                    # Keep adding until we hit a ref or amount
                    if is_ref(part):
                        reference_no = part
                        col_idx = 1
                    elif is_amount(part):
                        # This is an amount in what should be the description area
                        # It's likely a withdrawal amount
                        if not withdrawal:
                            withdrawal = part
                        col_idx = 1
                    else:
                        description_parts.append(part)
                elif col_idx == 1:
                    # After ref — next is withdrawal or deposit
                    if is_amount(part):
                        if not withdrawal:
                            withdrawal = part
                        elif not deposit:
                            deposit = part
                        elif not balance:
                            balance = part
                        col_idx = 2
                    elif is_ref(part):
                        # Another ref (unlikely but handle it)
                        reference_no = part
                    else:
                        description_parts.append(part)
                        col_idx = 0
                elif col_idx == 2:
                    if is_amount(part):
                        if not deposit:
                            deposit = part
                        elif not balance:
                            balance = part

            description = " ".join(description_parts).strip()
            current_txn = {
                "date": date1,
                "description": description,
                "reference_no": reference_no,
                "withdrawal": withdrawal,
                "deposits": deposit,
                "balance": balance,
            }

        elif group_type == "amounts":
            # Amount line — fill into current transaction
            if current_txn:
                parts = [p.strip() for p in group_text.split("\t") if p.strip()]
                # Filter out pure amounts
                amts = [p for p in parts if is_amount(p)]
                if len(amts) >= 3:
                    current_txn["withdrawal"] = amts[0]
                    current_txn["deposits"] = amts[1]
                    current_txn["balance"] = amts[2]
                elif len(amts) == 2:
                    current_txn["deposits"] = amts[0]
                    current_txn["balance"] = amts[1]
                elif len(amts) == 1:
                    current_txn["balance"] = amts[0]

        elif group_type == "continuation":
            # Description continuation
            if current_txn:
                line_clean = group_text.strip()
                if line_clean and not is_amount(line_clean):
                    # Check if this is a ref/amount in the reference column area
                    if is_ref(line_clean):
                        if not current_txn["reference_no"]:
                            current_txn["reference_no"] = line_clean
                    elif is_amount(line_clean) and not current_txn["withdrawal"]:
                        current_txn["withdrawal"] = line_clean
                    else:
                        # Append to description
                        current_txn["description"] = (
                            (current_txn["description"] + " " + line_clean).strip()
                        )

        elif group_type == "date_only":
            # A bare date on its own line — this is the second date (value date)
            # of the current transaction, or start of new one
            if current_txn and not current_txn.get("date2"):
                current_txn["date2"] = group_text.strip()
            else:
                # New transaction with just a date
                flush_txn()
                current_txn = {
                    "date": group_text.strip(),
                    "description": "",
                    "reference_no": "",
                    "withdrawal": "",
                    "deposits": "",
                    "balance": "",
                }

    flush_txn()
    return rows if rows else []


# ── Generic / fallback parser ───────────────────────────────────────────────

def _parse_generic(text: str) -> list:
    """Best-effort generic parser for unknown bank formats."""
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

        i += 1

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
    BANK_YES: _parse_yesbank,
}


def parse_pdf(file_bytes: bytes, password: str = "") -> dict:
    """
    Parse a PDF bank statement and return normalized rows.

    Args:
        file_bytes: raw PDF bytes
        password: optional password for encrypted PDFs

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

    t0 = time.perf_counter()

    # Check if password-protected and decrypt if needed
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
    logger.info(f"[Parser] text extraction: {(t2-t1)*1000:.0f}ms, chars={len(text)}")

    if not text.strip():
        raise RuntimeError(
            "PDF appears to be empty or could not be read. "
            "Please upload a valid text-based PDF."
        )

    bank = detect_bank(text)

    t3 = time.perf_counter()
    logger.info(f"[Parser] bank detection: {(t3-t2)*1000:.0f}ms, bank={bank}")

    rows = []
    try:
        if bank == BANK_YES:
            rows = _parse_yesbank(text)
        else:
            parser_fn = _PARSERS.get(bank, _parse_generic)
            rows = parser_fn(text)
    except Exception as e:
        logger.error(f"Parser for {bank} failed: {e}", exc_info=True)
        rows = _parse_generic(text)
        bank = BANK_UNKNOWN

    t4 = time.perf_counter()
    logger.info(f"[Parser] row extraction: {(t4-t3)*1000:.0f}ms, rows={len(rows)}")

    rows = [_normalize_row(r) for r in rows]

    t5 = time.perf_counter()
    logger.info(f"[Parser] normalization: {(t5-t4)*1000:.0f}ms, TOTAL parse: {(t5-t0)*1000:.0f}ms")

    return {
        "bank": bank,
        "rows": rows,
        "raw_text": text[:2000],
    }
