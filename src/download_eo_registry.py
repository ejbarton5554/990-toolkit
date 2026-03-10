#!/usr/bin/env python
"""Download IRS Exempt Organizations Registry (EO BMF) files.

Downloads state-by-state CSV files, regional files, and the international
file from the IRS SOI division.

Usage:
    python src/download_eo_registry.py
    python src/download_eo_registry.py --output-dir data/eo_registry --verbose
    python src/download_eo_registry.py --states-only
    python src/download_eo_registry.py --regions-only
"""
import argparse
import os
import sys

try:
    from urllib.request import urlopen, Request
    from urllib.error import HTTPError, URLError
except ImportError:
    from urllib2 import urlopen, Request, HTTPError, URLError


BASE_URL = "https://www.irs.gov/pub/irs-soi"

STATE_CODES = [
    "ak", "al", "ar", "az", "ca", "co", "ct", "dc", "de", "fl",
    "ga", "hi", "ia", "id", "il", "in", "ks", "ky", "la", "ma",
    "md", "me", "mi", "mn", "mo", "ms", "mt", "nc", "nd", "ne",
    "nh", "nj", "nm", "nv", "ny", "oh", "ok", "or", "pa", "pr",
    "ri", "sc", "sd", "tn", "tx", "ut", "va", "vt", "wa", "wi",
    "wv", "wy",
]

REGIONAL_FILES = ["eo1.csv", "eo2.csv", "eo3.csv", "eo4.csv"]
INTERNATIONAL_FILE = "eo_xx.csv"


def download_file(url, dest_path, verbose=False):
    # type: (str, str, bool) -> bool
    """Download a file with progress reporting. Returns True on success."""
    try:
        req = Request(url)
        resp = urlopen(req, timeout=60)
        total = resp.headers.get("Content-Length")
        total = int(total) if total else None

        downloaded = 0
        chunk_size = 1024 * 1024  # 1 MB

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
        if verbose and total:
            sys.stdout.write("\n")
        return True

    except (HTTPError, URLError) as e:
        if verbose:
            print("  Error: %s" % e)
        tmp_path = dest_path + ".partial"
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Download IRS Exempt Organizations Registry (EO BMF) files."
    )
    parser.add_argument(
        "--output-dir", default="data/eo_registry",
        help="Output directory (default: data/eo_registry)",
    )
    parser.add_argument(
        "--states-only", action="store_true",
        help="Download only state-by-state files",
    )
    parser.add_argument(
        "--regions-only", action="store_true",
        help="Download only regional rollup files",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-download files that already exist",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show download progress",
    )
    args = parser.parse_args()

    output_dir = args.output_dir
    if not os.path.isdir(output_dir):
        os.makedirs(output_dir)

    files_to_download = []  # (url, filename)

    if not args.regions_only:
        # State files
        for code in STATE_CODES:
            filename = "eo_%s.csv" % code
            url = "%s/%s" % (BASE_URL, filename)
            files_to_download.append((url, filename))
        # International
        url = "%s/%s" % (BASE_URL, INTERNATIONAL_FILE)
        files_to_download.append((url, INTERNATIONAL_FILE))

    if not args.states_only:
        # Regional files
        for filename in REGIONAL_FILES:
            url = "%s/%s" % (BASE_URL, filename)
            files_to_download.append((url, filename))

    print("Downloading %d files to %s\n" % (len(files_to_download), output_dir))

    downloaded = 0
    skipped = 0
    failed = 0

    for url, filename in files_to_download:
        dest_path = os.path.join(output_dir, filename)

        if os.path.exists(dest_path) and not args.force:
            skipped += 1
            if args.verbose:
                print("Already exists: %s" % filename)
            continue

        print("Downloading %s..." % filename)
        ok = download_file(url, dest_path, verbose=args.verbose)
        if ok:
            size_mb = os.path.getsize(dest_path) / (1024 * 1024.0)
            print("  OK (%.1f MB)" % size_mb)
            downloaded += 1
        else:
            print("  FAILED")
            failed += 1

    print("\nDone: %d downloaded, %d already existed, %d failed" % (
        downloaded, skipped, failed))


if __name__ == "__main__":
    main()
