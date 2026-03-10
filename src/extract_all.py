#!/usr/bin/env python
"""Extract every field from every IRS 990 XML filing into SQLite.

Single-pass extraction — no concordance dependency. Every leaf element
in every filing gets stored as a row in the database.

Usage:
    python src/extract_all.py --xml-dir data/xmls --db data/extracted/all_fields.db
    python src/extract_all.py --xml-dir data/xmls --db data/extracted/all_fields.db -w 4
    python src/extract_all.py --xml-dir data/xmls --db data/extracted/all_fields.db --limit 1000
"""
import argparse
import multiprocessing
import os
import re
import sqlite3
import sys
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from data_utilities.xml_audit import parse_xml


# ---------------------------------------------------------------------------
# Extraction (concordance-free)
# ---------------------------------------------------------------------------

def _strip_ns(tag):
    # type: (str) -> str
    if "}" in tag:
        return tag.split("}")[-1]
    return tag


def extract_filing(xml_path):
    # type: (str) -> Optional[Tuple[Dict[str, str], List[Tuple[str, str, str, str, int]]]]
    """Extract all leaf fields from one filing.

    Returns (header_dict, rows) where each row is
    (xpath, schedule, field_path, value, instance).

    Instance numbers are tracked at the container level: when a parent has
    multiple children with the same tag (e.g. multiple RecipientTable elements),
    each child gets an incrementing instance number. All descendants of that
    child inherit the same instance number so fields within a repeating group
    are correctly associated.

    Returns None on parse error.
    """
    info = parse_xml(xml_path)
    if info is None:
        return None

    return_data = info["return_data"]
    if return_data is None:
        return None

    header = {
        "ein": info["ein"],
        "tax_period": info["tax_period"],
        "org_name": info["org_name"],
        "return_version": info["return_version"],
        "form_type": info["form_type"],
    }

    rows = []  # type: List[Tuple[str, str, str, str, int]]

    def _walk(elem, path_parts, instance):
        # type: (Any, List[str], int) -> None
        tag = _strip_ns(elem.tag)
        current_parts = path_parts + [tag]
        xpath = "/" + "/".join(current_parts)

        children = list(elem)
        if children:
            # Count children by tag to detect repeating groups
            tag_counts = {}  # type: Dict[str, int]
            for child in children:
                ctag = _strip_ns(child.tag)
                tag_counts[ctag] = tag_counts.get(ctag, 0) + 1

            # Walk children, assigning instance numbers to repeating siblings
            tag_seen = {}  # type: Dict[str, int]
            for child in children:
                ctag = _strip_ns(child.tag)
                tag_seen[ctag] = tag_seen.get(ctag, 0) + 1
                if tag_counts[ctag] > 1:
                    # This tag repeats — use sibling instance number
                    _walk(child, current_parts, tag_seen[ctag])
                else:
                    # Non-repeating child — inherit parent's instance
                    _walk(child, current_parts, instance)
        else:
            text = elem.text.strip() if elem.text else ""
            if text:
                # Split into schedule + field_path
                parts = xpath.strip("/").split("/", 1)
                schedule = parts[0] if parts else ""
                field_path = parts[1] if len(parts) > 1 else ""

                rows.append((xpath, schedule, field_path, text, instance))

    for child in return_data:
        _walk(child, [], 1)

    return header, rows


# ---------------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------------

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS filings (
    filing_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ein TEXT NOT NULL,
    tax_period TEXT,
    org_name TEXT,
    return_version TEXT,
    form_type TEXT,
    filename TEXT
);

CREATE TABLE IF NOT EXISTS xpaths (
    xpath_id INTEGER PRIMARY KEY AUTOINCREMENT,
    xpath TEXT NOT NULL UNIQUE,
    schedule TEXT,
    field_path TEXT
);

CREATE TABLE IF NOT EXISTS fields (
    filing_id INTEGER NOT NULL,
    xpath_id INTEGER NOT NULL,
    instance INTEGER NOT NULL DEFAULT 1,
    value TEXT,
    FOREIGN KEY (filing_id) REFERENCES filings(filing_id),
    FOREIGN KEY (xpath_id) REFERENCES xpaths(xpath_id)
);

CREATE INDEX IF NOT EXISTS idx_filings_ein ON filings(ein);
CREATE INDEX IF NOT EXISTS idx_filings_form ON filings(form_type);
CREATE INDEX IF NOT EXISTS idx_fields_filing ON fields(filing_id);
CREATE INDEX IF NOT EXISTS idx_fields_xpath ON fields(xpath_id);
CREATE INDEX IF NOT EXISTS idx_fields_instance ON fields(filing_id, instance);
CREATE INDEX IF NOT EXISTS idx_xpaths_schedule ON xpaths(schedule);

CREATE VIEW IF NOT EXISTS fields_view AS
SELECT f.ein, f.tax_period, f.org_name, f.form_type, f.return_version,
       x.xpath, x.schedule, x.field_path, d.instance, d.value
FROM fields d
JOIN filings f ON d.filing_id = f.filing_id
JOIN xpaths x ON d.xpath_id = x.xpath_id;
"""


# In-memory xpath -> xpath_id cache (populated during extraction)
_xpath_id_cache = {}  # type: Dict[str, int]


def init_db(db_path):
    # type: (str) -> sqlite3.Connection
    """Create database and tables if needed."""
    db_dir = os.path.dirname(db_path)
    if db_dir and not os.path.isdir(db_dir):
        os.makedirs(db_dir)
    conn = sqlite3.connect(db_path)
    conn.executescript(CREATE_SQL)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-64000")  # 64MB cache
    conn.commit()
    return conn


def get_existing_filenames(conn):
    # type: (sqlite3.Connection) -> set
    """Get set of filenames already in the database."""
    cursor = conn.execute("SELECT DISTINCT filename FROM filings")
    return set(row[0] for row in cursor)


# ---------------------------------------------------------------------------
# XML file discovery
# ---------------------------------------------------------------------------

def find_xml_files(xml_dir):
    # type: (str) -> List[str]
    """Recursively find all .xml files."""
    xml_files = []
    for dirpath, dirnames, filenames in os.walk(xml_dir):
        for fn in filenames:
            if fn.lower().endswith(".xml"):
                xml_files.append(os.path.join(dirpath, fn))
    xml_files.sort()
    return xml_files


# ---------------------------------------------------------------------------
# Multiprocessing
# ---------------------------------------------------------------------------

def _worker_func(xml_path):
    # type: (str) -> Optional[Tuple[str, Dict[str, str], List[Tuple[str, str, str, str, int]]]]
    """Worker: extract one filing. Returns (filename, header, rows) or None."""
    result = extract_filing(xml_path)
    if result is None:
        return None
    header, rows = result
    return (os.path.basename(xml_path), header, rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Extract every field from IRS 990 XML filings into SQLite.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --xml-dir data/xmls --db data/extracted/all_fields.db
  %(prog)s --xml-dir data/xmls --db data/extracted/all_fields.db -w 4 --verbose
  %(prog)s --xml-dir data/xmls --db data/extracted/all_fields.db --limit 1000
        """,
    )
    parser.add_argument("--xml-dir", default="./data/xmls",
                        help="Directory of XML filings (default: ./data/xmls)")
    parser.add_argument("--db", default="./data/extracted/all_fields.db",
                        help="SQLite database path (default: ./data/extracted/all_fields.db)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max filings to process (0 = all)")
    parser.add_argument("--workers", "-w", type=int, default=1,
                        help="Parallel workers (0 = all CPUs, default: 1)")
    parser.add_argument("--batch-size", type=int, default=500,
                        help="Filings per database commit (default: 500)")
    parser.add_argument("--resume", action="store_true",
                        help="Skip files already in the database")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show progress")
    args = parser.parse_args()

    if not os.path.isdir(args.xml_dir):
        print("ERROR: XML directory not found: %s" % args.xml_dir)
        sys.exit(1)

    # Find XML files
    print("Scanning for XML files in %s..." % args.xml_dir)
    xml_files = find_xml_files(args.xml_dir)
    print("Found %d XML files" % len(xml_files))

    # Initialize database
    conn = init_db(args.db)

    # Resume: skip already-processed files
    if args.resume:
        existing = get_existing_filenames(conn)
        before = len(xml_files)
        xml_files = [f for f in xml_files if os.path.basename(f) not in existing]
        print("Resume mode: %d already processed, %d remaining" % (
            before - len(xml_files), len(xml_files)))

    if args.limit > 0:
        xml_files = xml_files[:args.limit]
        print("Processing first %d filings (--limit)" % args.limit)

    if not xml_files:
        print("Nothing to process.")
        conn.close()
        return

    # Resolve worker count
    n_workers = args.workers
    if n_workers == 0:
        n_workers = multiprocessing.cpu_count()
    use_parallel = n_workers > 1 and len(xml_files) > 100

    if use_parallel:
        print("Using %d parallel workers" % n_workers)

    print("\nExtracting fields...")
    processed = 0
    skipped = 0
    total_fields = 0
    batch_filings = []  # type: List[Tuple[str, Dict[str, str], List[Tuple[str, str, str, str, int]]]]
    t_start = time.time()

    def _get_xpath_id(xpath, schedule, field_path):
        # type: (str, str, str) -> int
        """Get or create xpath_id for a given xpath string."""
        if xpath in _xpath_id_cache:
            return _xpath_id_cache[xpath]
        cursor = conn.execute(
            "INSERT OR IGNORE INTO xpaths (xpath, schedule, field_path) VALUES (?, ?, ?)",
            (xpath, schedule, field_path),
        )
        if cursor.lastrowid:
            _xpath_id_cache[xpath] = cursor.lastrowid
        else:
            row = conn.execute(
                "SELECT xpath_id FROM xpaths WHERE xpath = ?", (xpath,)
            ).fetchone()
            _xpath_id_cache[xpath] = row[0]
        return _xpath_id_cache[xpath]

    def _flush_batch():
        # type: () -> None
        """Write accumulated batch to database."""
        nonlocal total_fields
        if not batch_filings:
            return
        for filename, header, rows in batch_filings:
            cursor = conn.execute(
                "INSERT INTO filings (ein, tax_period, org_name, return_version, form_type, filename) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (header["ein"], header["tax_period"], header["org_name"],
                 header["return_version"], header["form_type"], filename),
            )
            filing_id = cursor.lastrowid
            if rows:
                field_rows = []
                for xpath, schedule, field_path, value, instance in rows:
                    xpath_id = _get_xpath_id(xpath, schedule, field_path)
                    field_rows.append((filing_id, xpath_id, instance, value))
                conn.executemany(
                    "INSERT INTO fields (filing_id, xpath_id, instance, value) VALUES (?, ?, ?, ?)",
                    field_rows,
                )
                total_fields += len(rows)
        conn.commit()
        batch_filings.clear()

    def _handle_result(result):
        # type: (Any) -> None
        nonlocal processed, skipped
        if result is None:
            skipped += 1
            return
        filename, header, rows = result
        batch_filings.append((filename, header, rows))
        processed += 1

        if len(batch_filings) >= args.batch_size:
            _flush_batch()

        if args.verbose and (processed % 2000 == 0 or processed == 1):
            elapsed = time.time() - t_start
            rate = processed / elapsed if elapsed > 0 else 0
            print("  %d/%d filings (%.0f/sec, %d skipped, %d fields)" % (
                processed, len(xml_files), rate, skipped, total_fields))

    if use_parallel:
        pool = multiprocessing.Pool(processes=n_workers)
        try:
            for result in pool.imap_unordered(_worker_func, xml_files, chunksize=64):
                _handle_result(result)
        finally:
            pool.close()
            pool.join()
    else:
        for xml_path in xml_files:
            result = _worker_func(xml_path)
            _handle_result(result)

    # Final flush
    _flush_batch()

    elapsed = time.time() - t_start
    rate = processed / elapsed if elapsed > 0 else 0

    # Get final counts from database
    filing_count = conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0]
    field_count = conn.execute("SELECT COUNT(*) FROM fields").fetchone()[0]
    db_size = os.path.getsize(args.db) / (1024 * 1024.0)

    conn.close()

    print("\nProcessed %d filings in %.1f seconds (%.0f/sec, %d skipped)" % (
        processed, elapsed, rate, skipped))
    print("Database: %s (%.1f MB)" % (args.db, db_size))
    print("  Total filings: %d" % filing_count)
    print("  Total fields: %d" % field_count)
    print("\nDone!")


if __name__ == "__main__":
    main()
