#!/usr/bin/env python
"""
Extract specific fields from IRS 990 XML filings based on Field Finder exports.

Reads JSON field specifications exported by the dashboard's Field Finder,
extracts those fields from all XML filings, and produces CSVs split by
repeating group with a cross-reference file.

Usage:
    python extract_fields.py --fields-dir ./Fields --xml-dir ./990_xmls \
        --concordance ./concordance_output/field_lookup.json \
        --output-dir ./extracted_output --limit 100 --verbose
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from lxml import etree


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class FieldSpec:
    field_name = ""       # type: str
    label = ""            # type: str
    schedule = ""         # type: str
    field_type = ""       # type: str
    description = ""      # type: str
    frequency_pct = None  # type: Optional[float]
    priority = ""         # type: str
    relevance = ""        # type: str
    categories = None     # type: List[str]
    xpaths = None         # type: Dict[str, str]
    source_jsons = None   # type: List[str]
    is_repeating = False  # type: bool
    group_name = ""       # type: str
    group_xpath_prefix = ""  # type: str
    relative_xpath = ""   # type: str

    def __init__(self):
        self.categories = []
        self.xpaths = {}
        self.source_jsons = []


@dataclass
class GroupSpec:
    group_name = ""       # type: str
    group_xpath_prefix = ""  # type: str
    schedule = ""         # type: str
    child_fields = None   # type: List[FieldSpec]

    def __init__(self, group_name, group_xpath_prefix, schedule):
        self.group_name = group_name
        self.group_xpath_prefix = group_xpath_prefix
        self.schedule = schedule
        self.child_fields = []


# Suffixes that indicate a repeating group container
GROUP_SUFFIXES = ("Grp", "Detail", "Table")


# ---------------------------------------------------------------------------
# Group detection
# ---------------------------------------------------------------------------

def detect_group(xpath):
    # type: (str) -> Tuple[str, str, str]
    """Detect if an xpath belongs to a repeating group.

    Returns (group_name, group_xpath_prefix, relative_xpath) or
    ("", "", "") if scalar.

    Strategy: parse xpath segments, scan non-leaf/non-schedule segments
    from deepest to shallowest; first segment ending in Grp/Detail/Table
    is the group anchor.
    """
    if not xpath or not xpath.startswith("/"):
        return ("", "", "")

    segments = xpath.strip("/").split("/")
    if len(segments) < 2:
        return ("", "", "")

    # The first segment is the schedule/form (IRS990, IRS990ScheduleJ, etc.)
    # Non-leaf means all segments except the last
    # Scan from deepest non-leaf to shallowest non-leaf (skip index 0 = schedule)
    for i in range(len(segments) - 2, 0, -1):
        seg = segments[i]
        if any(seg.endswith(suffix) for suffix in GROUP_SUFFIXES):
            group_name = seg
            group_xpath_prefix = "/" + "/".join(segments[:i + 1])
            relative_xpath = "/".join(segments[i + 1:])
            return (group_name, group_xpath_prefix, relative_xpath)

    return ("", "", "")


# ---------------------------------------------------------------------------
# Load field specifications from Field Finder JSON exports
# ---------------------------------------------------------------------------

def load_field_specs(fields_dir, concordance_path):
    # type: (str, str) -> Tuple[List[FieldSpec], Dict[str, GroupSpec]]
    """Load all Field Finder JSON exports and classify fields.

    Returns (scalar_specs, group_specs) where group_specs maps
    group_name -> GroupSpec with child fields.
    """
    # Load concordance for supplemental metadata
    with open(concordance_path, "r") as f:
        concordance = json.load(f)

    # Collect all fields, merging duplicates across JSONs
    all_fields = OrderedDict()  # type: Dict[str, FieldSpec]

    json_files = sorted([
        fn for fn in os.listdir(fields_dir)
        if fn.endswith(".json")
    ])

    if not json_files:
        print("ERROR: No JSON files found in %s" % fields_dir)
        sys.exit(1)

    for json_file in json_files:
        json_path = os.path.join(fields_dir, json_file)
        with open(json_path, "r") as f:
            data = json.load(f)

        for fld in data.get("fields", []):
            fname = fld["field_name"]

            if fname in all_fields:
                # Merge: add this JSON as a source
                if json_file not in all_fields[fname].source_jsons:
                    all_fields[fname].source_jsons.append(json_file)
                continue

            spec = FieldSpec()
            spec.field_name = fname
            spec.label = fld.get("label", "")
            spec.schedule = fld.get("schedule", "")
            spec.field_type = fld.get("type", "")
            spec.description = fld.get("description", "")
            spec.frequency_pct = fld.get("frequency_pct")
            spec.priority = fld.get("priority", "")
            spec.relevance = fld.get("relevance", "")
            spec.categories = fld.get("categories", [])
            spec.xpaths = fld.get("xpaths", {})
            spec.source_jsons = [json_file]

            # Detect group membership from xpath
            sample_xpath = ""
            for v in sorted(spec.xpaths.keys()):
                sample_xpath = spec.xpaths[v]
                break

            group_name, group_prefix, rel_xpath = detect_group(sample_xpath)
            spec.is_repeating = bool(group_name)
            spec.group_name = group_name
            spec.group_xpath_prefix = group_prefix
            spec.relative_xpath = rel_xpath

            all_fields[fname] = spec

    print("Loaded %d unique fields from %d JSON files" % (len(all_fields), len(json_files)))

    # Split into scalar vs group
    scalar_specs = []  # type: List[FieldSpec]
    group_specs = OrderedDict()  # type: Dict[str, GroupSpec]

    for spec in all_fields.values():
        if spec.is_repeating:
            gname = spec.group_name
            if gname not in group_specs:
                group_specs[gname] = GroupSpec(gname, spec.group_xpath_prefix, spec.schedule)
            group_specs[gname].child_fields.append(spec)
        else:
            scalar_specs.append(spec)

    print("  Scalar fields: %d" % len(scalar_specs))
    print("  Group fields: %d across %d groups" % (
        sum(len(g.child_fields) for g in group_specs.values()),
        len(group_specs)
    ))
    for gname, gspec in group_specs.items():
        print("    %s: %d child fields" % (gname, len(gspec.child_fields)))

    return scalar_specs, group_specs


# ---------------------------------------------------------------------------
# XML file discovery
# ---------------------------------------------------------------------------

def find_xml_files(xml_dir):
    # type: (str) -> List[str]
    """Recursively find all .xml files in xml_dir."""
    xml_files = []
    for dirpath, dirnames, filenames in os.walk(xml_dir):
        for fn in filenames:
            if fn.lower().endswith(".xml"):
                xml_files.append(os.path.join(dirpath, fn))
    xml_files.sort()
    return xml_files


# ---------------------------------------------------------------------------
# XML parsing helpers
# ---------------------------------------------------------------------------

def parse_filing(xml_path):
    # type: (str) -> Optional[Tuple[Dict[str, str], object, str, str]]
    """Parse an XML filing and extract header metadata.

    Returns (header_dict, return_data_elem, namespace, version) or None on error.
    header_dict has keys: EIN, tax_period, org_name, return_version, form_type
    """
    try:
        tree = etree.parse(xml_path)
    except etree.XMLSyntaxError:
        return None

    root = tree.getroot()

    # Detect namespace
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    # Extract version from Return@returnVersion
    version = root.get("returnVersion", "")

    # Find ReturnHeader
    header = root.find("%sReturnHeader" % ns)
    if header is None:
        return None

    # Extract header fields
    ein_elem = header.find(".//%sFiler/%sEIN" % (ns, ns))
    ein = ein_elem.text.strip() if ein_elem is not None and ein_elem.text else ""

    tax_period_elem = header.find("%sTaxPeriodEndDt" % ns)
    tax_period = tax_period_elem.text.strip() if tax_period_elem is not None and tax_period_elem.text else ""

    # Organization name - try Filer/BusinessName/BusinessNameLine1Txt
    org_name = ""
    name_elem = header.find(".//%sFiler/%sBusinessName/%sBusinessNameLine1Txt" % (ns, ns, ns))
    if name_elem is not None and name_elem.text:
        org_name = name_elem.text.strip()

    # Form type (ReturnTypeCd)
    form_type_elem = header.find("%sReturnTypeCd" % ns)
    form_type = form_type_elem.text.strip() if form_type_elem is not None and form_type_elem.text else ""

    # Find ReturnData
    return_data = root.find("%sReturnData" % ns)
    if return_data is None:
        return None

    header_dict = {
        "EIN": ein,
        "tax_period": tax_period,
        "org_name": org_name,
        "return_version": version,
        "form_type": form_type,
    }

    return header_dict, return_data, ns, version


def resolve_xpath(xpaths, version):
    # type: (Dict[str, str], str) -> Optional[str]
    """Resolve the best xpath for a given filing version.

    Fallback chain:
    1. Exact match
    2. Same year, latest sub-version
    3. Nearest prior version
    4. Nearest overall
    """
    if not xpaths:
        return None

    # 1. Exact match
    if version in xpaths:
        return xpaths[version]

    # Parse versions for comparison
    def parse_ver(v):
        # type: (str) -> Tuple[int, int]
        """Parse '2022v5.0' -> (2022, 50)"""
        m = re.match(r"(\d{4})v(\d+)\.(\d+)", v)
        if m:
            return (int(m.group(1)), int(m.group(2)) * 10 + int(m.group(3)))
        return (0, 0)

    target_year, target_sub = parse_ver(version)
    available = sorted(xpaths.keys(), key=parse_ver)

    # 2. Same year, latest sub-version
    same_year = [v for v in available if parse_ver(v)[0] == target_year]
    if same_year:
        return xpaths[same_year[-1]]

    # 3. Nearest prior version
    prior = [v for v in available if parse_ver(v) < (target_year, target_sub)]
    if prior:
        return xpaths[prior[-1]]

    # 4. Nearest overall (first available)
    if available:
        return xpaths[available[-1]]

    return None


def make_ns_xpath(concordance_xpath, ns):
    # type: (str, str) -> str
    """Convert concordance xpath to namespace-qualified lxml xpath.

    /IRS990/ContractorCompensationGrp/CompensationAmt
    -> {http://...}IRS990/{http://...}ContractorCompensationGrp/{http://...}CompensationAmt
    """
    if not concordance_xpath:
        return ""
    parts = concordance_xpath.strip("/").split("/")
    return "/".join("%s%s" % (ns, p) for p in parts)


def make_ns_xpath_relative(relative_xpath, ns):
    # type: (str, str) -> str
    """Convert a relative xpath (no leading slash) to namespace-qualified."""
    if not relative_xpath:
        return ""
    parts = relative_xpath.split("/")
    return "/".join("%s%s" % (ns, p) for p in parts)


# ---------------------------------------------------------------------------
# Field extraction
# ---------------------------------------------------------------------------

def extract_scalar_fields(return_data, ns, version, scalar_specs):
    # type: (object, str, str, List[FieldSpec]) -> Dict[str, str]
    """Extract scalar field values from a filing.

    Returns {field_name: text_value}.
    """
    result = {}  # type: Dict[str, str]

    for spec in scalar_specs:
        xpath = resolve_xpath(spec.xpaths, version)
        if not xpath:
            result[spec.field_name] = ""
            continue

        ns_xpath = make_ns_xpath(xpath, ns)
        elem = return_data.find(ns_xpath)

        if elem is not None and elem.text and elem.text.strip():
            result[spec.field_name] = elem.text.strip()
        else:
            result[spec.field_name] = ""

    return result


def extract_group_instances(return_data, ns, version, group_specs):
    # type: (object, str, str, Dict[str, GroupSpec]) -> Dict[str, List[Dict[str, str]]]
    """Extract repeating group instances from a filing.

    Returns {group_name: [instance_dict, ...]} where each instance_dict
    maps field_name -> text_value.
    """
    result = {}  # type: Dict[str, List[Dict[str, str]]]

    for gname, gspec in group_specs.items():
        instances = []

        # Resolve group container xpath using any child's xpaths
        if not gspec.child_fields:
            continue

        # Get the group container xpath for this version
        # Use the first child's xpath to derive the group container path
        child0 = gspec.child_fields[0]
        child_xpath = resolve_xpath(child0.xpaths, version)
        if not child_xpath:
            result[gname] = []
            continue

        # Derive group container xpath from the full child xpath
        # e.g. /IRS990/ContractorCompensationGrp/CompensationAmt
        #    -> /IRS990/ContractorCompensationGrp
        group_prefix = child_xpath
        # Find the group name in the xpath and truncate after it
        parts = group_prefix.strip("/").split("/")
        group_container_parts = []
        for part in parts:
            group_container_parts.append(part)
            if part == gname:
                break
        group_container_xpath = "/" + "/".join(group_container_parts)

        ns_container = make_ns_xpath(group_container_xpath, ns)
        container_elems = return_data.findall(ns_container)

        for idx, container in enumerate(container_elems):
            instance = {}  # type: Dict[str, str]
            for child_spec in gspec.child_fields:
                # Resolve child's full xpath for this version
                full_xpath = resolve_xpath(child_spec.xpaths, version)
                if not full_xpath:
                    instance[child_spec.field_name] = ""
                    continue

                # Derive relative xpath (part after group container)
                # e.g. /IRS990/ContractorCompensationGrp/ContractorAddress/USAddress/CityNm
                # group_container_xpath = /IRS990/ContractorCompensationGrp
                # relative = ContractorAddress/USAddress/CityNm
                if full_xpath.startswith(group_container_xpath + "/"):
                    rel = full_xpath[len(group_container_xpath) + 1:]
                else:
                    # Fallback: use stored relative xpath
                    rel = child_spec.relative_xpath

                ns_rel = make_ns_xpath_relative(rel, ns)
                elem = container.find(ns_rel)

                if elem is not None and elem.text and elem.text.strip():
                    instance[child_spec.field_name] = elem.text.strip()
                else:
                    instance[child_spec.field_name] = ""

            instances.append(instance)

        result[gname] = instances

    return result


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_outputs(output_dir, scalar_rows, group_rows, scalar_specs, group_specs):
    # type: (str, List[Dict[str, str]], Dict[str, List[Dict[str, str]]], List[FieldSpec], Dict[str, GroupSpec]) -> None
    """Write all output CSVs."""
    os.makedirs(output_dir, exist_ok=True)

    header_cols = ["EIN", "tax_period", "org_name", "return_version", "form_type"]

    # 1. scalar_fields.csv
    scalar_field_names = [s.field_name for s in scalar_specs]
    scalar_path = os.path.join(output_dir, "scalar_fields.csv")
    with open(scalar_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header_cols + scalar_field_names,
                                extrasaction="ignore")
        writer.writeheader()
        for row in scalar_rows:
            writer.writerow(row)
    print("Wrote %s (%d rows, %d field columns)" % (
        scalar_path, len(scalar_rows), len(scalar_field_names)))

    # 2. Per-group CSVs
    for gname, gspec in group_specs.items():
        child_field_names = [c.field_name for c in gspec.child_fields]
        group_header = ["EIN", "tax_period", "instance_num"] + child_field_names
        group_path = os.path.join(output_dir, "%s.csv" % gname)

        rows = group_rows.get(gname, [])
        with open(group_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=group_header,
                                    extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        print("Wrote %s (%d rows)" % (group_path, len(rows)))

    # 3. field_reference.csv
    ref_path = os.path.join(output_dir, "field_reference.csv")
    ref_cols = [
        "field_name", "label", "schedule", "type", "description",
        "frequency_pct", "priority", "relevance", "categories",
        "source_jsons", "output_file", "sample_xpath"
    ]
    with open(ref_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ref_cols)
        writer.writeheader()

        # Scalar fields
        for spec in scalar_specs:
            sample_xpath = ""
            for v in sorted(spec.xpaths.keys()):
                sample_xpath = spec.xpaths[v]
                break
            writer.writerow({
                "field_name": spec.field_name,
                "label": spec.label,
                "schedule": spec.schedule,
                "type": spec.field_type,
                "description": spec.description,
                "frequency_pct": spec.frequency_pct if spec.frequency_pct is not None else "",
                "priority": spec.priority,
                "relevance": spec.relevance,
                "categories": "; ".join(spec.categories),
                "source_jsons": "; ".join(spec.source_jsons),
                "output_file": "scalar_fields.csv",
                "sample_xpath": sample_xpath,
            })

        # Group fields
        for gname, gspec in group_specs.items():
            for spec in gspec.child_fields:
                sample_xpath = ""
                for v in sorted(spec.xpaths.keys()):
                    sample_xpath = spec.xpaths[v]
                    break
                writer.writerow({
                    "field_name": spec.field_name,
                    "label": spec.label,
                    "schedule": spec.schedule,
                    "type": spec.field_type,
                    "description": spec.description,
                    "frequency_pct": spec.frequency_pct if spec.frequency_pct is not None else "",
                    "priority": spec.priority,
                    "relevance": spec.relevance,
                    "categories": "; ".join(spec.categories),
                    "source_jsons": "; ".join(spec.source_jsons),
                    "output_file": "%s.csv" % gname,
                    "sample_xpath": sample_xpath,
                })

    total_fields = len(scalar_specs) + sum(
        len(g.child_fields) for g in group_specs.values())
    print("Wrote %s (%d fields)" % (ref_path, total_fields))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Extract fields from IRS 990 XML filings based on Field Finder exports.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --limit 100 --verbose
  %(prog)s --fields-dir ./Fields --xml-dir ./990_xmls --output-dir ./extracted_output
  %(prog)s --concordance ./concordance_output/field_lookup.json --limit 500
        """
    )
    parser.add_argument("--fields-dir", default="./Fields",
                        help="Directory of Field Finder JSON exports (default: ./Fields)")
    parser.add_argument("--xml-dir", default="./990_xmls",
                        help="Directory of IRS 990 XML filings (default: ./990_xmls)")
    parser.add_argument("--concordance", default="./concordance_output/field_lookup.json",
                        help="Path to field_lookup.json (default: ./concordance_output/field_lookup.json)")
    parser.add_argument("--output-dir", default="./extracted_output",
                        help="Output directory for CSVs (default: ./extracted_output)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max filings to process (0 = all, default: 0)")
    parser.add_argument("--verbose", action="store_true",
                        help="Show per-filing progress")

    args = parser.parse_args()

    # Validate paths
    if not os.path.isdir(args.fields_dir):
        print("ERROR: Fields directory not found: %s" % args.fields_dir)
        sys.exit(1)
    if not os.path.isdir(args.xml_dir):
        print("ERROR: XML directory not found: %s" % args.xml_dir)
        sys.exit(1)
    if not os.path.isfile(args.concordance):
        print("ERROR: Concordance file not found: %s" % args.concordance)
        sys.exit(1)

    # Load field specs
    print("Loading field specifications...")
    scalar_specs, group_specs = load_field_specs(args.fields_dir, args.concordance)

    # Find XML files
    print("\nScanning for XML filings in %s..." % args.xml_dir)
    xml_files = find_xml_files(args.xml_dir)
    print("Found %d XML files" % len(xml_files))

    if args.limit > 0:
        xml_files = xml_files[:args.limit]
        print("Processing first %d filings (--limit)" % args.limit)

    # Process filings
    print("\nExtracting fields...")
    scalar_rows = []  # type: List[Dict[str, str]]
    # group_rows: group_name -> list of row dicts
    group_rows = {}  # type: Dict[str, List[Dict[str, str]]]
    for gname in group_specs:
        group_rows[gname] = []

    header_cols = ["EIN", "tax_period", "org_name", "return_version", "form_type"]
    processed = 0
    skipped = 0
    t_start = time.time()

    for i, xml_path in enumerate(xml_files):
        result = parse_filing(xml_path)
        if result is None:
            skipped += 1
            if args.verbose:
                print("  SKIP (parse error): %s" % os.path.basename(xml_path))
            continue

        header_dict, return_data, ns, version = result
        processed += 1

        # Extract scalar fields
        scalar_vals = extract_scalar_fields(return_data, ns, version, scalar_specs)
        row = dict(header_dict)
        row.update(scalar_vals)
        scalar_rows.append(row)

        # Extract group instances
        group_instances = extract_group_instances(return_data, ns, version, group_specs)
        for gname, instances in group_instances.items():
            for idx, inst in enumerate(instances):
                grow = {
                    "EIN": header_dict["EIN"],
                    "tax_period": header_dict["tax_period"],
                    "instance_num": str(idx + 1),
                }
                grow.update(inst)
                group_rows[gname].append(grow)

        # Progress reporting
        if args.verbose and (processed % 500 == 0 or processed == 1):
            elapsed = time.time() - t_start
            rate = processed / elapsed if elapsed > 0 else 0
            print("  Processed %d/%d filings (%.0f/sec, %d skipped)" % (
                processed, len(xml_files), rate, skipped))

    elapsed = time.time() - t_start
    print("\nProcessed %d filings in %.1f seconds (%.0f/sec, %d skipped)" % (
        processed, elapsed, processed / elapsed if elapsed > 0 else 0, skipped))

    # Summary of group data
    for gname in group_specs:
        row_count = len(group_rows[gname])
        if row_count > 0:
            print("  %s: %d instances across filings" % (gname, row_count))

    # Write outputs
    print("\nWriting output files...")
    write_outputs(args.output_dir, scalar_rows, group_rows, scalar_specs, group_specs)
    print("\nDone!")


if __name__ == "__main__":
    main()
