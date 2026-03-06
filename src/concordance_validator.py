#!/usr/bin/env python3
"""
Concordance Coverage Validator
================================
Brute-force extracts every xs:element[@name] from the schema .xsd files,
then checks which ones are missing from the concordance builder output.

For each missing element, reports the XSD structural context (what pattern
surrounds it) so we can identify which parser patterns need fixing.

Usage:
    python concordance_validator.py \
        --schema-dir ./UnzipSchemas \
        --concordance ./concordance_output/field_lookup.json \
        --version 2020v4.2
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

try:
    from lxml import etree
except ImportError:
    sys.exit("pip install lxml")

XS = "http://www.w3.org/2001/XMLSchema"
XS_PREFIX = f"{{{XS}}}"

FORM_PREFIXES = ("IRS990", "IRS990EZ", "IRS990PF", "ReturnHeader", "Return")


def classify_pattern(elem) -> str:
    """Classify the XSD structural pattern of an xs:element."""
    inline_ct = elem.find(f"{XS_PREFIX}complexType")
    has_type_attr = bool(elem.get("type", ""))

    if inline_ct is None and has_type_attr:
        return "simple_typed"  # e.g., type="USAmountType"

    if inline_ct is None and not has_type_attr:
        return "untyped"

    # Has inline complexType — what's inside?
    sc = inline_ct.find(f"{XS_PREFIX}simpleContent")
    if sc is not None:
        ext = sc.find(f"{XS_PREFIX}extension")
        rst = sc.find(f"{XS_PREFIX}restriction")
        if ext is not None:
            return f"simpleContent/extension[base={ext.get('base', '?')}]"
        if rst is not None:
            return f"simpleContent/restriction[base={rst.get('base', '?')}]"
        return "simpleContent/other"

    cc = inline_ct.find(f"{XS_PREFIX}complexContent")
    if cc is not None:
        ext = cc.find(f"{XS_PREFIX}extension")
        rst = cc.find(f"{XS_PREFIX}restriction")
        if ext is not None:
            return f"complexContent/extension[base={ext.get('base', '?')}]"
        if rst is not None:
            return f"complexContent/restriction[base={rst.get('base', '?')}]"
        return "complexContent/other"

    # Direct children: sequence, all, choice, group?
    children = []
    for child in inline_ct:
        tag = etree.QName(child.tag).localname if isinstance(child.tag, str) else ""
        if tag:
            children.append(tag)

    if children:
        return f"complexType[{'+'.join(children)}]"
    return "complexType[empty]"


def build_xpath_context(elem, ns_prefix: str) -> str:
    """Walk up the tree to build the xpath context of an element."""
    parts = []
    current = elem
    while current is not None:
        if not isinstance(current.tag, str):
            current = current.getparent()
            continue
        local = etree.QName(current.tag).localname
        name = current.get("name", "")
        if local == "element" and name:
            parts.insert(0, name)
        elif local == "complexType" and name:
            parts.insert(0, f"[type:{name}]")
        elif local == "group" and name:
            parts.insert(0, f"[group:{name}]")
        elif local == "schema":
            break
        current = current.getparent()
    return "/".join(parts) if parts else "(root)"


def extract_all_elements(xsd_path: str) -> list[dict]:
    """Brute-force extract every xs:element with a name attribute."""
    try:
        tree = etree.parse(xsd_path)
    except etree.XMLSyntaxError:
        return []

    root = tree.getroot()
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    results = []
    for elem in root.iter(f"{XS_PREFIX}element"):
        name = elem.get("name", "")
        if not name:
            continue

        pattern = classify_pattern(elem)
        context = build_xpath_context(elem, ns)

        # Determine which form/schedule this belongs to
        schedule = ""
        for part in context.split("/"):
            clean = part.strip("[]").replace("type:", "").replace("group:", "")
            for prefix in FORM_PREFIXES:
                if clean.startswith(prefix):
                    # Take just the schedule portion
                    schedule = clean.split("[")[0] if "[" in clean else clean
                    break
            if schedule:
                break

        results.append({
            "name": name,
            "file": os.path.basename(xsd_path),
            "pattern": pattern,
            "context": context,
            "schedule": schedule,
            "line": elem.sourceline,
        })
    return results


def run_validation(schema_dir: str, concordance_path: str, version: str = None,
                   verbose: bool = False):
    """Main validation loop."""

    # Load concordance
    with open(concordance_path) as f:
        conc = json.load(f)
    conc_fields = conc.get("fields", {})
    xpath_index = conc.get("xpath_index", {})

    # Build set of all element names in concordance
    conc_element_names = set()
    conc_xpaths = set(xpath_index.keys())
    for name, info in conc_fields.items():
        # Add the element_name and also the canonical name
        en = info.get("element_name", "")
        if en:
            conc_element_names.add(en)
        # Also extract leaf names from all xpaths
        for v, xp in info.get("xpaths", {}).items():
            parts = xp.strip("/").split("/")
            if parts:
                conc_element_names.add(parts[-1])

    # Find all .xsd files
    schema_path = Path(schema_dir)
    if version:
        search_root = schema_path / version
        if not search_root.exists():
            # Try finding it
            candidates = [d for d in schema_path.iterdir()
                          if d.is_dir() and version in d.name]
            if candidates:
                search_root = candidates[0]
            else:
                sys.exit(f"Version directory not found: {version}")
    else:
        search_root = schema_path

    xsd_files = sorted(search_root.rglob("*.xsd"))
    print(f"Scanning {len(xsd_files)} .xsd files in {search_root}...")

    # Extract all elements from all schema files
    all_elements = []
    for xsd_file in xsd_files:
        elements = extract_all_elements(str(xsd_file))
        all_elements.extend(elements)
        if verbose and elements:
            print(f"  {xsd_file.name}: {len(elements)} elements")

    print(f"Total xs:element definitions found: {len(all_elements)}")

    # Deduplicate by name (same element defined in multiple versions)
    unique_by_name = {}
    for elem in all_elements:
        name = elem["name"]
        if name not in unique_by_name:
            unique_by_name[name] = elem
        else:
            # Keep the one with more context
            if len(elem["context"]) > len(unique_by_name[name]["context"]):
                unique_by_name[name] = elem

    print(f"Unique element names: {len(unique_by_name)}")

    # Filter to only form/schedule elements (skip shared type definitions
    # that are container-only like USAddressType, BusinessNameType — unless
    # they appear under a form context)
    form_elements = {}
    type_only_elements = {}
    for name, elem in unique_by_name.items():
        if elem["schedule"]:
            form_elements[name] = elem
        else:
            type_only_elements[name] = elem

    print(f"Elements under form/schedule context: {len(form_elements)}")
    print(f"Elements in shared type definitions: {len(type_only_elements)}")

    # Compare: which form elements are NOT in the concordance?
    # An element is "covered" if:
    #   (a) its name appears as a leaf in the concordance, OR
    #   (b) it's a group/container whose children are in the concordance
    #       (detected by checking if any concordance xpath contains this name
    #        as an intermediate path segment)
    conc_path_segments = set()
    for xp in conc_xpaths:
        parts = xp.strip("/").split("/")
        for part in parts:
            conc_path_segments.add(part)

    in_conc = set()
    missing = {}
    covered_as_group = set()
    for name, elem in form_elements.items():
        if name in conc_element_names:
            in_conc.add(name)
        elif name in conc_path_segments:
            # This element name appears as a path segment in concordance xpaths,
            # meaning it's a group container whose children are extracted
            covered_as_group.add(name)
        else:
            missing[name] = elem

    print(f"\n{'='*60}")
    print(f"RESULTS")
    print(f"{'='*60}")
    print(f"  Form elements in schemas:     {len(form_elements)}")
    print(f"  Found as leaf fields:         {len(in_conc)}")
    print(f"  Covered as group containers:  {len(covered_as_group)}")
    print(f"  MISSING from concordance:     {len(missing)}")
    total_covered = len(in_conc) + len(covered_as_group)
    pct = total_covered / len(form_elements) * 100 if form_elements else 0
    print(f"  Coverage:                     {pct:.1f}%")

    if not missing:
        print("\n✅ All form elements are in the concordance!")
        return missing

    # Group missing by pattern
    by_pattern = defaultdict(list)
    for name, elem in missing.items():
        by_pattern[elem["pattern"]].append(elem)

    print(f"\n{'='*60}")
    print(f"MISSING ELEMENTS BY XSD PATTERN")
    print(f"{'='*60}")
    for pattern, elems in sorted(by_pattern.items(), key=lambda x: -len(x[1])):
        print(f"\n  [{len(elems)}] {pattern}")
        for elem in sorted(elems, key=lambda e: e["name"])[:10]:
            print(f"      {elem['name']}")
            print(f"        file: {elem['file']}  line: {elem['line']}")
            print(f"        context: {elem['context']}")
        if len(elems) > 10:
            print(f"      ... and {len(elems) - 10} more")

    # Group missing by schedule
    by_schedule = defaultdict(list)
    for name, elem in missing.items():
        by_schedule[elem["schedule"] or "(no schedule)"].append(elem)

    print(f"\n{'='*60}")
    print(f"MISSING ELEMENTS BY SCHEDULE")
    print(f"{'='*60}")
    for sched, elems in sorted(by_schedule.items()):
        print(f"  {sched}: {len(elems)} missing")

    # Write detailed report
    return missing


def main():
    parser = argparse.ArgumentParser(
        description="Validate concordance coverage against raw XSD schemas")
    parser.add_argument("--schema-dir", required=True,
                        help="Root directory containing versioned schema folders")
    parser.add_argument("--concordance", required=True,
                        help="Path to field_lookup.json")
    parser.add_argument("--version", default=None,
                        help="Specific version to validate (e.g., 2020v4.2)")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--output", default=None,
                        help="Write JSON report to this path")

    args = parser.parse_args()

    missing = run_validation(args.schema_dir, args.concordance,
                             args.version, args.verbose)

    if args.output and missing:
        report = []
        for name, elem in sorted(missing.items()):
            report.append({
                "element_name": name,
                "file": elem["file"],
                "line": elem["line"],
                "pattern": elem["pattern"],
                "context": elem["context"],
                "schedule": elem["schedule"],
            })
        with open(args.output, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\nDetailed report written to: {args.output}")


if __name__ == "__main__":
    main()
