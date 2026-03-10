#!/usr/bin/env python
"""Download IRS 990 XML bulk data for specified years.

Downloads TEOS XML zip files from the IRS and optionally extracts them.

Usage:
    python src/download_990_xml.py --years 2023 2024 2025
    python src/download_990_xml.py --years 2024 --no-extract
    python src/download_990_xml.py --years 2023 2024 2025 --output-dir data/xmls
"""
import argparse
import os
import sys
import zipfile
import time

try:
    from urllib.request import urlopen, Request
    from urllib.error import HTTPError, URLError
except ImportError:
    from urllib2 import urlopen, Request, HTTPError, URLError


BASE_URL = "https://apps.irs.gov/pub/epostcard/990/xml"

# Month chunks per year. Most months have just "A", but some have multiple.
# We probe A-D for each month and stop when we get a 404.
MONTHS = ["01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "11", "12"]
SUFFIXES = ["A", "B", "C", "D", "E"]


def build_zip_url(year, month, suffix):
    # type: (int, str, str) -> str
    return "%s/%d/%d_TEOS_XML_%s%s.zip" % (BASE_URL, year, year, month, suffix)


def index_url(year):
    # type: (int,) -> str
    return "%s/%d/index_%d.csv" % (BASE_URL, year, year)


def file_exists_remote(url):
    # type: (str,) -> bool
    """Check if a URL exists via HEAD request."""
    try:
        req = Request(url, method="HEAD")
        resp = urlopen(req, timeout=15)
        resp.close()
        return True
    except (HTTPError, URLError, AttributeError):
        # AttributeError: Python 2 Request doesn't have method kwarg
        # Fall back to trying GET with range header
        try:
            req = Request(url)
            req.add_header("Range", "bytes=0-0")
            resp = urlopen(req, timeout=15)
            resp.close()
            return True
        except (HTTPError, URLError):
            return False


def download_file(url, dest_path, verbose=False):
    # type: (str, str, bool) -> bool
    """Download a file with progress reporting. Returns True on success."""
    try:
        req = Request(url)
        resp = urlopen(req, timeout=60)
        total = resp.headers.get("Content-Length")
        total = int(total) if total else None

        downloaded = 0
        chunk_size = 1024 * 1024  # 1 MB chunks

        tmp_path = dest_path + ".partial"
        with open(tmp_path, "wb") as f:
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if verbose and total:
                    pct = downloaded * 100.0 / total
                    mb = downloaded / (1024 * 1024.0)
                    total_mb = total / (1024 * 1024.0)
                    sys.stdout.write(
                        "\r  %.1f / %.1f MB (%.0f%%)" % (mb, total_mb, pct)
                    )
                    sys.stdout.flush()

        resp.close()
        os.rename(tmp_path, dest_path)
        if verbose:
            sys.stdout.write("\n")
        return True

    except (HTTPError, URLError) as e:
        if verbose:
            print("  Error: %s" % e)
        # Clean up partial file
        tmp_path = dest_path + ".partial"
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return False


def extract_zip(zip_path, extract_dir, verbose=False):
    # type: (str, str, bool) -> int
    """Extract a zip file. Returns number of files extracted."""
    if not os.path.isdir(extract_dir):
        os.makedirs(extract_dir)
    with zipfile.ZipFile(zip_path, "r") as zf:
        members = zf.namelist()
        existing = set(os.listdir(extract_dir))
        to_extract = [m for m in members if os.path.basename(m) not in existing]
        if not to_extract:
            if verbose:
                print("  Already extracted (%d files)" % len(members))
            return 0
        for m in to_extract:
            zf.extract(m, extract_dir)
        if verbose:
            print("  Extracted %d new files (%d already existed)" % (
                len(to_extract), len(members) - len(to_extract)
            ))
        return len(to_extract)


def discover_files_for_year(year, verbose=False):
    # type: (int, bool) -> list
    """Discover all available zip files for a given year."""
    files = []
    for month in MONTHS:
        for suffix in SUFFIXES:
            url = build_zip_url(year, month, suffix)
            if file_exists_remote(url):
                filename = "%d_TEOS_XML_%s%s.zip" % (year, month, suffix)
                files.append((url, filename))
                if verbose:
                    print("  Found: %s" % filename)
            else:
                # No more suffixes for this month
                break
        time.sleep(0.1)  # Be polite to IRS servers
    return files


def main():
    parser = argparse.ArgumentParser(
        description="Download IRS 990 XML bulk data from TEOS."
    )
    parser.add_argument(
        "--years", nargs="+", type=int, required=True,
        help="Years to download (e.g., 2023 2024 2025)",
    )
    parser.add_argument(
        "--output-dir", default="data/xmls",
        help="Base output directory (default: data/xmls)",
    )
    parser.add_argument(
        "--no-extract", action="store_true",
        help="Download zips only, do not extract",
    )
    parser.add_argument(
        "--index", action="store_true",
        help="Also download the index CSV for each year",
    )
    parser.add_argument(
        "--index-only", action="store_true",
        help="Download only the index CSVs (no XML zips)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show progress details",
    )
    args = parser.parse_args()

    output_dir = args.output_dir
    if not os.path.isdir(output_dir):
        os.makedirs(output_dir)

    for year in sorted(args.years):
        print("\n=== %d ===" % year)

        # Download index if requested
        if args.index or args.index_only:
            idx_url = index_url(year)
            idx_path = os.path.join(output_dir, "index_%d.csv" % year)
            if os.path.exists(idx_path):
                print("Index already exists: %s" % idx_path)
            else:
                print("Downloading index_%d.csv..." % year)
                download_file(idx_url, idx_path, verbose=args.verbose)

        if args.index_only:
            continue

        # Discover available files
        print("Discovering available files...")
        files = discover_files_for_year(year, verbose=args.verbose)
        if not files:
            print("No files found for %d" % year)
            continue
        print("Found %d zip files for %d" % (len(files), year))

        # Download and extract each zip
        extract_dir = os.path.join(output_dir, "Forms%d" % year)
        total_new = 0

        for url, filename in files:
            zip_path = os.path.join(output_dir, filename)

            if os.path.exists(zip_path):
                print("Already downloaded: %s" % filename)
            else:
                print("Downloading %s..." % filename)
                ok = download_file(url, zip_path, verbose=args.verbose)
                if not ok:
                    print("  FAILED — skipping")
                    continue

            if not args.no_extract:
                n = extract_zip(zip_path, extract_dir, verbose=args.verbose)
                total_new += n

        if not args.no_extract:
            n_total = len(os.listdir(extract_dir)) if os.path.isdir(extract_dir) else 0
            print("Total filings for %d: %d (%d newly extracted)" % (
                year, n_total, total_new
            ))

    print("\nDone.")


if __name__ == "__main__":
    main()
