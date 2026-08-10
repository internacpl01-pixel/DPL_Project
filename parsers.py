"""
Generic PDF statement parser.
Uses pdfplumber table extraction + word-coordinate fallback.
No bank-specific logic — fieldmap table drives all column mapping.
"""
from __future__ import annotations

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


def _extract_pages_text(file_bytes: bytes) -> list:
    """Extract text per page. Returns a list of page-text strings."""
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
    return pages_text


def extract_text_from_pdf(file_bytes: bytes) -> str:
    return "\n".join(_extract_pages_text(file_bytes))


# ── Helpers ─────────────────────────────────────────────────────────────────

def _clean_amount(val) -> str:
    """Strip currency symbols, commas, spaces from an amount."""
    if val is None:
        return ""
    s = str(val).strip()
    if not s:
        return ""
    # Strip currency prefixes (full tokens, not char-by-char)
    s = re.sub(r"^(Rs\.?|INR|₹)\s*", "", s, flags=re.IGNORECASE)
    # Strip commas and whitespace
    s = re.sub(r"[,\s]", "", s)
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

def _extract_tables_from_pdf(file_bytes: bytes, table_settings: dict = None) -> list:
    """
    Extract tables from PDF using pdfplumber.
    Returns list of tables, each table is a list of rows (each row is a list of cell strings).

    table_settings is passed through to pdfplumber — e.g. {"horizontal_strategy": "text"}
    splits rows by text lines when the PDF's row-separator lines aren't detectable.
    """
    if not PDFPLUMBER_AVAILABLE:
        return []
    tables = []
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page in pdf.pages:
                page_tables = page.extract_tables(table_settings)
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


def _fieldname_category(fieldname: str) -> str | None:
    """Return the semantic category for a fieldname/alias, or None for custom fields.
    Categories are stable concept names — they don't depend on any specific fieldmap string.
    """
    n = fieldname.lower().strip()
    if n == "date" or n in ("value_date", "entry_date", "tran_date", "txn_date"):
        return "date"
    if n in ("description", "desc", "particulars", "narration", "remarks", "narrations"):
        return "description"
    if n in ("withdrawal", "debit", "dr", "amount_out"):
        return "withdrawal"
    if n in ("deposits", "deposit", "credit", "cr", "amount_in", "deposit amt"):
        return "deposits"
    if n in ("balance", "closing_balance", "available_balance"):
        return "balance"
    if n in ("reference_no", "ref_no", "chq_ref_no", "cheque_no", "reference", "instrument_no", "ref"):
        return "reference_no"
    return None  # custom field — passthrough


def _has_valid_date(row_cells: list, date_col_idx: int) -> bool:
    """Check if a row has a valid date in the date column."""
    if date_col_idx is None or date_col_idx >= len(row_cells):
        return False
    val = str(row_cells[date_col_idx]).strip()
    return bool(_parse_date_to_date(val))


def _assemble_rows(table_rows: list, header_idx: int, col_mapping: dict,
                    live_col_types: dict = None) -> list:
    """
    Assemble transaction rows from table data using fieldmap + column types.

    Row keys = fieldmap fieldnames (not hardcoded names).
    Column roles come from information_schema data types:
      • DATE type     → anchor column (starts a new row)
      • TEXT type     → text column (continuation lines + footer check)
      • NUMERIC type  → numeric column (amounts, cleaned)

    Handles multi-line descriptions. Skips footer/summary rows.
    """
    # Determine column roles from live column types
    live_col_types = live_col_types or {}
    date_col = None       # anchor column — starts a new row
    text_cols = set()     # text columns — receive continuation lines
    numeric_cols = set()  # numeric columns — receive cleaned amounts

    for col_idx, fieldname in col_mapping.items():
        col_type = (live_col_types.get(fieldname) or "").lower()
        if col_type in ("date", "timestamp without time zone", "timestamp"):
            date_col = col_idx
        elif col_type in ("text", "character varying", "varchar"):
            text_cols.add(col_idx)
        elif col_type in ("real", "double precision", "numeric", "integer", "bigint"):
            numeric_cols.add(col_idx)

    # Fallback: if no date column found by type, use name heuristics
    if date_col is None:
        for col_idx, fieldname in col_mapping.items():
            fn_lower = fieldname.lower()
            if fn_lower in ("date", "value_date", "entry_date", "tran_date", "txn_date"):
                date_col = col_idx
                break

    rows = []
    current_row = None
    date_fieldname = None  # the fieldmap fieldname for the date column

    for row_idx in range(header_idx + 1, len(table_rows)):
        row_cells = table_rows[row_idx]
        if not row_cells:
            continue

        row_cells = [str(c).strip() if c else "" for c in row_cells]

        # A row with a valid date in the anchor column is always a transaction —
        # even if its text matches a footer keyword (e.g. "B/F" opening-balance rows).
        if date_col is not None and _has_valid_date(row_cells, date_col):
            if current_row and date_fieldname and current_row.get(date_fieldname):
                rows.append(current_row)

            current_row = {}
            for col_idx, cell in enumerate(row_cells):
                fieldname = col_mapping.get(col_idx)
                if not fieldname or not cell:
                    continue
                current_row[fieldname] = cell
                if col_idx == date_col:
                    date_fieldname = fieldname

        else:
            # Date-less rows: footer/summary rows (TOTAL, Page N, ...) end the
            # current transaction and must not leak into its description.
            is_footer = False
            for col_idx in text_cols:
                if col_idx < len(row_cells):
                    cell_lower = row_cells[col_idx].lower()
                    for kw in _FOOTER_KEYWORDS:
                        if kw == cell_lower or cell_lower.startswith(kw):
                            is_footer = True
                            break
                if is_footer:
                    break
            if is_footer:
                if current_row and date_fieldname and current_row.get(date_fieldname):
                    rows.append(current_row)
                    current_row = None
                continue

            if current_row and date_fieldname and current_row.get(date_fieldname):
                # Continuation row: append to text columns only
                for col_idx in text_cols:
                    if col_idx < len(row_cells) and row_cells[col_idx]:
                        fieldname = col_mapping.get(col_idx)
                        if fieldname:
                            if fieldname in current_row and current_row[fieldname]:
                                current_row[fieldname] += " " + row_cells[col_idx]
                            else:
                                current_row[fieldname] = row_cells[col_idx]

    # Save last row
    if current_row and date_fieldname and current_row.get(date_fieldname):
        rows.append(current_row)

    return rows


# ── Multi-table assembly ────────────────────────────────────────────────────

_DATE_TOKEN_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}|\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}|\d{1,2}-[A-Za-z]{3,9}-\d{2,4}"
)


def _has_merged_rows(rows: list) -> bool:
    """
    Detect pdfplumber's 'merged row' failure: when row-separator lines aren't
    found, all transactions collapse into one giant row whose cells hold many
    newline-joined values (e.g. the date cell contains every date in the table).
    """
    for r in rows:
        for v in r.values():
            s = str(v)
            if "\n" in s and len(_DATE_TOKEN_RE.findall(s)) >= 2:
                return True
    return False


def _assemble_from_tables(tables: list, alias_map: dict, live_col_types: dict) -> tuple:
    """
    Assemble rows from ALL tables (all pages), not just the first.
    Tables with a detectable header are parsed with their own column mapping;
    header-less tables (continuation pages) reuse the previous table's mapping
    when the column count matches.

    Returns (rows, headers_detected, unmapped_headers).
    """
    all_rows = []
    headers_detected = {}
    unmapped_headers = []
    last_mapping = None
    last_ncols = 0

    for table in tables:
        if not table:
            continue

        header_idx, col_mapping = _detect_header_row(table, alias_map)
        if header_idx >= 0 and col_mapping and len(col_mapping) >= 2:
            header_row = table[header_idx]
            for col_idx, fn in col_mapping.items():
                if col_idx < len(header_row) and fn not in headers_detected:
                    headers_detected[fn] = str(header_row[col_idx]).strip()
            for col_idx, cell in enumerate(header_row):
                cell_str = str(cell).strip() if cell else ""
                if cell_str and col_idx not in col_mapping and cell_str not in unmapped_headers:
                    unmapped_headers.append(cell_str)

            all_rows.extend(_assemble_rows(table, header_idx, col_mapping, live_col_types))
            last_mapping = col_mapping
            last_ncols = len(header_row)
        elif last_mapping and len(table[0]) == last_ncols:
            # Continuation page without a repeated header — reuse previous mapping
            all_rows.extend(_assemble_rows(table, -1, last_mapping, live_col_types))

    return all_rows, headers_detected, unmapped_headers


# ── Generic parser ──────────────────────────────────────────────────────────

def parse_pdf(file_bytes: bytes, password: str = "", fieldmap_rows: list = None,
              live_col_types: dict = None) -> dict:
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
            "rows": [{<fieldmap fieldname>: value, ...}, ...],
            "row_count": int,
            "headers_detected": {fieldname: header_text, ...},
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

    # Live column types (passed from pdf_import, used for data-type-driven roles)
    live_col_types = live_col_types or {}

    # 1) Ruled-lines table extraction — processes ALL tables/pages
    rows = []
    headers_detected = {}
    unmapped_headers = []

    tables = _extract_tables_from_pdf(file_bytes)
    if tables:
        rows, headers_detected, unmapped_headers = _assemble_from_tables(
            tables, alias_map, live_col_types)

    # 2) Retry with text-line rows when the lines strategy merged multiple
    #    transactions into one row (row separators not detectable) or found nothing.
    if not rows or _has_merged_rows(rows):
        text_tables = _extract_tables_from_pdf(
            file_bytes, {"horizontal_strategy": "text"})
        if text_tables:
            rows2, hd2, um2 = _assemble_from_tables(
                text_tables, alias_map, live_col_types)
            if len(rows2) > len(rows):
                logger.info(f"[Parser] text-strategy retry: {len(rows)} -> {len(rows2)} rows")
                rows, headers_detected, unmapped_headers = rows2, hd2, um2

    # 3) Word-coordinate clustering fallback
    if not rows:
        coord_tables = _extract_table_by_coordinates(file_bytes)
        if coord_tables:
            rows, headers_detected, unmapped_headers = _assemble_from_tables(
                coord_tables, alias_map, live_col_types)

    t2 = time.perf_counter()

    text = extract_text_from_pdf(file_bytes)

    # 4) Last resort: text-based row parsing
    if not rows:
        logger.info("[Parser] Table extraction yielded no rows, falling back to text extraction")
        rows = _parse_rows_fallback(text)
        headers_detected = {}
        unmapped_headers = []

    t3 = time.perf_counter()

    # Resolve parser concept-keys to master column names via fieldmap.
    # Fallback rows use concept names ("description", "withdrawal" etc.).
    # Table rows already have fieldmap fieldnames as keys — those pass through unchanged.
    # Build category→fieldname mapping from fieldmap's display names.
    _category_map = {}
    for alias, fieldname in (alias_map or {}).items():
        cat = _fieldname_category(fieldname)
        if cat and cat not in _category_map:
            _category_map[cat] = fieldname

    canonical_to_master = {}
    for parser_key in ("date", "description", "withdrawal", "deposits", "balance", "reference_no"):
        cat = _fieldname_category(parser_key)
        master_col = _category_map.get(cat)
        if master_col:
            canonical_to_master[parser_key] = master_col
        else:
            # No fieldmap entry for this category — use the key as-is
            # (works for default columns like "date" which exists in schema directly)
            canonical_to_master[parser_key] = parser_key

    # Normalize rows: coerce types from live_col_types (not hardcoded names).
    # Fallback-parser rows use concept keys ("description", ...) — rename them
    # to master column names first; table rows already carry fieldmap fieldnames.
    normalized = []
    for r in rows:
        new_row = {}
        for key, val in r.items():
            if val is None or val == "":
                continue
            if key not in live_col_types:
                key = canonical_to_master.get(key, key)
            col_type = (live_col_types.get(key) or "").lower()
            if col_type in ("date", "timestamp without time zone", "timestamp"):
                new_row[key] = _parse_date(val)
            elif col_type in ("real", "double precision", "numeric", "integer", "bigint"):
                new_row[key] = _clean_amount(val)
            else:
                # Collapse newlines from multi-line PDF cells into single spaces
                new_row[key] = re.sub(r"\s+", " ", str(val)).strip()
        if new_row:
            normalized.append(new_row)

    t4 = time.perf_counter()
    logger.info(
        f"[Parser] table: {(t2-t1)*1000:.0f}ms, assemble: {(t3-t2)*1000:.0f}ms, "
        f"normalize: {(t4-t3)*1000:.0f}ms, TOTAL: {(t4-t0)*1000:.0f}ms, "
        f"rows={len(normalized)}"
    )

    return {
        "rows": normalized,
        "row_count": len(normalized),
        "headers_detected": headers_detected,
        "unmapped_headers": unmapped_headers,
        "raw_text": text[:2000],
    }




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
        # First amount = transaction amount (withdrawal if Dr sign present)
        if not row.get("withdrawal"):
            row["withdrawal"] = amounts[0]
        # Second amount = running balance (NOT deposits)
        if not row.get("balance"):
            row["balance"] = amounts[1]

    ref_match = re.search(r"(\d{10,})\s*$", text)
    if ref_match:
        row["reference_no"] = ref_match.group(1)


# ── Main entry point ────────────────────────────────────────────────────────

def _parse_sync(file_bytes: bytes, password: str = "", fieldmap_rows: list = None, live_col_types: dict = None) -> dict:
    """Sync wrapper for use with run_in_executor."""
    return parse_pdf(file_bytes, password=password, fieldmap_rows=fieldmap_rows or [],
                     live_col_types=live_col_types or {})
