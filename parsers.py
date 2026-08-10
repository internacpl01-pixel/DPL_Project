"""
Generic PDF statement parser.
Uses pdfplumber table extraction + word-coordinate fallback.
No bank-specific logic — fieldmap table drives all column mapping.
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


# ── PDF text extraction ─────────────────────────────────────────────────────

def check_pdf_protected(file_bytes: bytes) -> bool:
    if not PYPDF_AVAILABLE:
        return False
    try:
        reader = PdfReader(io.BytesIO(file_bytes))
        return reader.is_encrypted
    except Exception:
        return False


def decrypt_pdf(file_bytes: bytes, password: str) -> bytes:
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

def _clean_amount(val) -> str:
    """Strip currency symbols, commas, spaces from an amount."""
    if val is None:
        return ""
    s = str(val).strip()
    if not s:
        return ""
    s = re.sub(r"[RsINR,\s]", "", s)
    # Handle Dr/Cr suffix
    if s.upper().endswith("DR") and not s.endswith("-"):
        s = "-" + s[:-2].strip()
    elif s.upper().endswith("CR"):
        s = s[:-2].strip()
    return s.strip()


def _parse_date(val) -> str:
    """Normalize a date string to ISO format."""
    if val is None:
        return ""
    s = str(val).strip()
    if not s:
        return ""
    # Already ISO
    if re.match(r"\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    # DD/MM/YYYY or DD-MM-YYYY
    m = re.match(r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})", s)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    # DD-Mon-YYYY or DD-Mon-YY
    m = re.match(r"(\d{1,2})-([A-Za-z]{3,})-(\d{2,4})", s)
    if m:
        month_map = {
            "jan": "01", "feb": "02", "mar": "03", "apr": "04",
            "may": "05", "jun": "06", "jul": "07", "aug": "08",
            "sep": "09", "oct": "10", "nov": "11", "dec": "12",
        }
        mon = month_map.get(m.group(2).lower()[:3])
        if mon:
            year = m.group(3)
            if len(year) == 2:
                year = "20" + year
            return f"{year}-{mon}-{int(m.group(1)):02d}"
    return s


def _parse_date_to_date(val):
    """Parse date string to datetime.date object for asyncpg."""
    s = _parse_date(val)
    if not s:
        return None
    try:
        from datetime import date
        parts = s.split("-")
        return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except Exception:
        return None


# ── Normalization ───────────────────────────────────────────────────────────

def _normalize_for_matching(s: str) -> str:
    """Normalize a string for alias matching: lowercase, strip punctuation/underscores, collapse spaces."""
    s = s.strip().lower()
    s = re.sub(r"[^\w\s]", "", s)  # remove punctuation
    s = re.sub(r"_", " ", s)       # underscores → spaces
    s = re.sub(r"\s+", " ", s)     # collapse spaces
    return s.strip()


def _build_alias_map(fieldmap_rows: list) -> dict:
    """Build {normalized_alias: fieldname} from fieldmap rows."""
    alias_map = {}
    for row in (fieldmap_rows or []):
        fieldname = row.get("fieldname", "")
        mapfields = row.get("mapfields", "")
        for alias in mapfields.split(","):
            alias = alias.strip()
            if alias:
                norm = _normalize_for_matching(alias)
                alias_map[norm] = fieldname
    return alias_map


def _match_alias(header: str, alias_map: dict) -> tuple:
    """
    Match a PDF column header against the alias map.
    Priority: exact > starts-with > contains. Longest alias first.
    Returns (fieldname, confidence) or (None, 0).
    """
    norm = _normalize_for_matching(header)
    if not norm:
        return None, 0

    # Sort aliases by length descending for longest-match-first
    sorted_aliases = sorted(alias_map.keys(), key=len, reverse=True)

    # 1. Exact match
    if norm in alias_map:
        return alias_map[norm], 3

    # 2. Starts-with
    for alias in sorted_aliases:
        if norm.startswith(alias) or alias.startswith(norm):
            return alias_map[alias], 2

    # 3. Contains (one contains the other)
    for alias in sorted_aliases:
        if norm in alias or alias in norm:
            return alias_map[alias], 1

    return None, 0


# ── Table extraction ────────────────────────────────────────────────────────

def _extract_tables_from_pdf(file_bytes: bytes) -> list:
    """
    Extract tables from PDF using pdfplumber.
    Returns list of tables, each table is a list of rows (each row is a list of cell strings).
    """
    if not PDFPLUMBER_AVAILABLE:
        return []
    tables = []
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page in pdf.pages:
                page_tables = page.extract_tables()
                if page_tables:
                    tables.extend(page_tables)
    except Exception as e:
        logger.warning(f"[Parser] Table extraction failed: {e}")
    return tables


def _extract_table_by_coordinates(file_bytes: bytes) -> list:
    """
    Fallback: extract table structure using word coordinates.
    Groups words into rows by y-position, columns by x-position.
    """
    if not PDFPLUMBER_AVAILABLE:
        return []
    tables = []
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page in pdf.pages:
                words = page.extract_words()
                if not words:
                    continue

                # Group words into rows by y-position (cluster within 3px)
                rows_by_y = defaultdict(list)
                for w in words:
                    y_key = round(float(w["top"]) / 3) * 3
                    rows_by_y[y_key].append(w)

                # Sort rows by y position (top to bottom)
                sorted_rows = sorted(rows_by_y.items(), key=lambda x: x[0])

                if len(sorted_rows) < 2:
                    continue

                # Get x-positions from first row (header candidates)
                header_words = sorted(sorted_rows[0][1], key=lambda w: float(w["x0"]))
                x_boundaries = []
                for w in header_words:
                    x_boundaries.append(float(w["x0"]))

                # Group each row into columns based on x_boundaries
                table_rows = []
                for y_key, row_words in sorted_rows:
                    row_words_sorted = sorted(row_words, key=lambda w: float(w["x0"]))
                    cells = []
                    for xb in x_boundaries:
                        cell_words = [w["text"] for w in row_words_sorted
                                     if abs(float(w["x0"]) - xb) < 30 or
                                     (cells and float(w["x0"]) < xb + 50)]
                        cells.append(" ".join(cell_words) if cell_words else "")
                    table_rows.append(cells)

                if table_rows:
                    tables.append(table_rows)
    except Exception as e:
        logger.warning(f"[Parser] Coordinate extraction failed: {e}")
    return tables


# ── Header detection ────────────────────────────────────────────────────────

def _detect_header_row(table_rows: list, alias_map: dict) -> tuple:
    """
    Find the header row in a table by matching ≥3 cells against fieldmap aliases.
    Returns (header_row_index, column_mapping) or (-1, None).

    column_mapping = {col_index: fieldname, ...}
    """
    best_score = 0
    best_idx = -1
    best_mapping = None

    for idx, row in enumerate(table_rows):
        if not row or len(row) < 2:
            continue

        mapping = {}
        score = 0
        for col_idx, cell in enumerate(row):
            cell = str(cell).strip()
            if not cell or len(cell) < 2:
                continue
            fieldname, confidence = _match_alias(cell, alias_map)
            if fieldname:
                mapping[col_idx] = fieldname
                score += confidence

        if score > best_score and len(mapping) >= 2:
            best_score = score
            best_idx = idx
            best_mapping = mapping

    return best_idx, best_mapping


# ── Row assembly ────────────────────────────────────────────────────────────

_FOOTER_KEYWORDS = {
    "total", "closing balance", "b/f", "c/f", "b/fwd", "c/fwd",
    "opening balance", "summary", "grand total", "page",
}

_DATE_COLS_CANONICAL = {"date", "value_date", "entry_date", "tran_date", "txn_date"}


def _is_footer_row(row_cells: list, col_mapping: dict) -> bool:
    """Check if a row is a footer/summary row."""
    for col_idx, cell in enumerate(row_cells):
        cell_lower = str(cell).strip().lower()
        if col_idx in col_mapping and col_mapping[col_idx] == "description":
            for kw in _FOOTER_KEYWORDS:
                if kw in cell_lower:
                    return True
    return False


def _has_valid_date(row_cells: list, col_mapping: dict, date_col_idx: int) -> bool:
    """Check if a row has a valid date in the date column."""
    if date_col_idx is None or date_col_idx >= len(row_cells):
        return False
    val = str(row_cells[date_col_idx]).strip()
    return bool(_parse_date_to_date(val))


def _assemble_rows(table_rows: list, header_idx: int, col_mapping: dict) -> list:
    """
    Assemble transaction rows from table data.
    Handles multi-line descriptions (date-less rows following a date row).
    Skips footer/summary rows.
    """
    # Find the date column
    date_col = None
    for col_idx, fieldname in col_mapping.items():
        if fieldname in _DATE_COLS_CANONICAL:
            date_col = col_idx
            break

    rows = []
    current_row = None

    for row_idx in range(header_idx + 1, len(table_rows)):
        row_cells = table_rows[row_idx]

        if not row_cells:
            continue

        # Clean empty cells
        row_cells = [str(c).strip() if c else "" for c in row_cells]

        # Skip footer/summary rows
        if _is_footer_row(row_cells, col_mapping):
            if current_row:
                rows.append(current_row)
                current_row = None
            continue

        # Check if this row has a valid date
        if date_col is not None and _has_valid_date(row_cells, date_col):
            # Save previous row
            if current_row and current_row.get("date"):
                rows.append(current_row)

            # Start new row
            current_row = {
                "date": _parse_date(row_cells[date_col]),
                "description": "",
                "withdrawal": "",
                "deposits": "",
                "balance": "",
                "reference_no": "",
            }
            # Fill other columns
            for col_idx, cell in enumerate(row_cells):
                if col_idx == date_col:
                    continue
                fieldname = col_mapping.get(col_idx)
                if not fieldname:
                    continue
                if fieldname == "description":
                    current_row["description"] = cell
                elif fieldname == "withdrawal":
                    current_row["withdrawal"] = cell
                elif fieldname == "deposits":
                    current_row["deposits"] = cell
                elif fieldname == "balance":
                    current_row["balance"] = cell
                elif fieldname == "reference_no":
                    current_row["reference_no"] = cell
        elif current_row and current_row.get("date"):
            # Continuation of previous row — append description
            desc_parts = [c for c in row_cells if c]
            if desc_parts:
                current_row["description"] += " " + " ".join(desc_parts)

    # Save last row
    if current_row and current_row.get("date"):
        rows.append(current_row)

    return rows


# ── Generic parser ──────────────────────────────────────────────────────────

def parse_pdf(file_bytes: bytes, password: str = "", fieldmap_rows: list = None) -> dict:
    """
    Parse a PDF bank statement and return normalized rows.

    Works for any bank — no per-bank logic.
    Uses pdfplumber table extraction → fieldmap-driven column mapping.

    Args:
        file_bytes: raw PDF bytes
        password: optional password for encrypted PDFs
        fieldmap_rows: list of fieldmap dicts from get_field_mappings()

    Returns:
        {
            "bank": str,
            "rows": [{"date", "description", "withdrawal", "deposits", "balance", "reference_no"}, ...],
            "row_count": int,
            "headers_detected": {col_name: fieldname, ...},
            "unmapped_headers": [header_text, ...],
            "raw_text": str
        }
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

    # Build alias map from fieldmap (for header matching)
    alias_map = _build_alias_map(fieldmap_rows or [])

    # Try table extraction first
    tables = _extract_tables_from_pdf(file_bytes)

    t2 = time.perf_counter()

    bank = "Unknown"
    rows = []
    headers_detected = {}
    unmapped_headers = []

    if tables:
        # Use the first meaningful table
        for table in tables:
            if not table or len(table) < 2:
                continue

            header_idx, col_mapping = _detect_header_row(table, alias_map)
            if header_idx >= 0 and len(col_mapping) >= 2:
                # Detect bank from header text
                header_text = " ".join(
                    str(c) for c in table[header_idx] if c
                ).upper()
                bank = _detect_bank_from_text(header_text)

                # Build headers_detected response
                for col_idx, fn in col_mapping.items():
                    if col_idx < len(table[header_idx]):
                        headers_detected[fn] = str(table[header_idx][col_idx])
                        # Track unmapped
                        if fn.startswith("field_") or fn in (c.lower() for c in table[header_idx] if _match_alias(str(c), alias_map)[0] is None):
                            pass

                # Collect unmapped headers
                for col_idx, cell in enumerate(table[header_idx]):
                    cell_str = str(cell).strip() if cell else ""
                    if cell_str and col_idx not in col_mapping:
                        unmapped_headers.append(cell_str)

                # Assemble rows
                rows = _assemble_rows(table, header_idx, col_mapping)

                if rows:
                    break

    # Fallback: text-based extraction if table extraction didn't find rows
    if not rows:
        logger.info("[Parser] Table extraction yielded no rows, falling back to text extraction")
        text = extract_text_from_pdf(file_bytes)
        bank = _detect_bank_from_text(text)
        rows = _parse_rows_fallback(text)
        headers_detected = {}
        unmapped_headers = []
    else:
        # Still extract text for bank detection and raw_text
        text = extract_text_from_pdf(file_bytes)
        bank = _detect_bank_from_text(text)

    t3 = time.perf_counter()

    # Normalize rows
    normalized = []
    for r in rows:
        normalized.append({
            "date": _parse_date(r.get("date", "")),
            "description": (r.get("description") or "").strip(),
            "withdrawal": _clean_amount(r.get("withdrawal", "")),
            "deposits": _clean_amount(r.get("deposits", "")),
            "balance": _clean_amount(r.get("balance", "")),
            "reference_no": (r.get("reference_no") or "").strip(),
        })

    t4 = time.perf_counter()
    logger.info(
        f"[Parser] table: {(t2-t1)*1000:.0f}ms, assemble: {(t3-t2)*1000:.0f}ms, "
        f"normalize: {(t4-t3)*1000:.0f}ms, TOTAL: {(t4-t0)*1000:.0f}ms, "
        f"bank={bank}, rows={len(normalized)}"
    )

    return {
        "bank": bank,
        "rows": normalized,
        "row_count": len(normalized),
        "headers_detected": headers_detected,
        "unmapped_headers": unmapped_headers,
        "raw_text": text[:2000] if 'text' in dir() else "",
    }


def _detect_bank_from_text(text: str) -> str:
    """Detect bank name from text for reporting only."""
    upper = text.upper() if text else ""
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
            if kw in upper:
                return bank_name
    return "Unknown"


# ── Fallback text parser (no table structure) ───────────────────────────────

def _parse_rows_fallback(text: str) -> list:
    """
    Text-based fallback when table extraction fails.
    Finds lines starting with date patterns and accumulates fields.
    """
    lines = text.split("\n")

    date_patterns = [
        re.compile(r"^(\d{4}-\d{2}-\d{2})"),
        re.compile(r"^(\d{2}/\d{2}/\d{4})"),
        re.compile(r"^(\d{2}-\d{2}-\d{4})"),
        re.compile(r"^(\d{2}-[A-Za-z]{3}-\d{4})"),
    ]

    amt_pattern = re.compile(r"(-?[\d,]+\.\d{2})")
    amt_triple = re.compile(r"([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})")

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

        if len(line) < 50:
            if any(kw.lower() in line.lower() for kw in skip_keywords):
                continue

        date_val = None
        date_end = 0
        for pat in date_patterns:
            m = pat.match(line)
            if m:
                date_val = m.group(1)
                date_end = m.end()
                break

        if date_val:
            if current_row and current_row.get("date"):
                rows.append(current_row)
            rest = line[date_end:].strip()
            current_row = {
                "date": _parse_date(date_val),
                "description": rest,
                "withdrawal": "",
                "deposits": "",
                "balance": "",
                "reference_no": "",
            }
            _extract_amounts(current_row, rest, amt_pattern, amt_triple)
        elif current_row:
            _extract_amounts(current_row, line, amt_pattern, amt_triple)

    if current_row and current_row.get("date"):
        rows.append(current_row)

    # Clean descriptions
    for r in rows:
        r["description"] = re.sub(r"\s+", " ", r["description"]).strip()
        r["description"] = re.sub(r"\s*[\d,]+\.\d{2}.*$", "", r["description"]).strip()

    return rows


def _extract_amounts(row: dict, text: str, amt_pattern, amt_triple):
    """Extract amounts from text into row dict."""
    triple = amt_triple.search(text)
    if triple:
        row["withdrawal"] = triple.group(1)
        row["deposits"] = triple.group(2)
        row["balance"] = triple.group(3)
        desc_part = text[:triple.start()].strip()
        if desc_part and len(desc_part) < len(row.get("description", "")):
            row["description"] = desc_part
        return

    amounts = amt_pattern.findall(text)
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

    ref_match = re.search(r"(\d{10,})\s*$", text)
    if ref_match:
        row["reference_no"] = ref_match.group(1)


# ── Main entry point ────────────────────────────────────────────────────────

def _parse_sync(file_bytes: bytes, password: str = "", fieldmap_rows: list = None) -> dict:
    """Sync wrapper for use with run_in_executor."""
    return parse_pdf(file_bytes, password=password, fieldmap_rows=fieldmap_rows or [])
