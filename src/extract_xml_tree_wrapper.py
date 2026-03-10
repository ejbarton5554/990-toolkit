#!/usr/bin/env python
"""Extract the full field-path tree from an IRS 990 XML filing.

Usage:
    python src/extract_xml_tree_wrapper.py data/xmls/some_filing.xml
    python src/extract_xml_tree_wrapper.py data/xmls/some_filing.xml --output trees/
    python src/extract_xml_tree_wrapper.py data/xmls/some_filing.xml --indented
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from data_utilities.xml_audit import xml_tree_text


def main():
    parser = argparse.ArgumentParser(
        description="Extract full xpath tree from an IRS 990 XML filing."
    )
    parser.add_argument("xml_file", help="Path to the XML filing")
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="Output directory for the text file (default: same directory as XML)",
    )
    parser.add_argument(
        "--indented",
        action="store_true",
        help="Use indentation instead of full xpaths (default is full paths)",
    )
    args = parser.parse_args()

    xml_path = args.xml_file
    if not os.path.isfile(xml_path):
        print("Error: file not found: %s" % xml_path)
        sys.exit(1)

    base = os.path.splitext(os.path.basename(xml_path))[0]
    out_dir = args.output if args.output else os.path.dirname(xml_path)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    save_path = os.path.join(out_dir, base + ".txt") if out_dir else base + ".txt"

    full_path = not args.indented
    xml_tree_text(xml_path, save_path=save_path, full_path=full_path)
    print("Saved: %s" % save_path)


if __name__ == "__main__":
    main()
