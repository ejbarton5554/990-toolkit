#!/usr/bin/env python
"""
Extract specific fields from IRS 990 XML filings.

Two modes:
  1. Field Finder mode (default): reads JSON exports from --fields-dir
  2. Schedule mode: extracts all leaf fields for a schedule from the concordance

Usage:
    python extract_fields.py --fields-dir ./Fields --limit 100 --verbose
    python extract_fields.py --schedule IRS990ScheduleJ --limit 100 --verbose
    python extract_fields.py --list-schedules
"""

import argparse
import csv
import json
import multiprocessing
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

def _build_concordance_leaf_index(concordance):
    # type: (dict) -> Dict[str, List[Dict[str, str]]]
    """Build an index from xpath leaf pattern to list of xpath dicts.

    For example, a concordance field with xpath "/IRS990/USAddress/CityNm"
    produces leaf key "USAddress/CityNm", mapping to its version->xpath dict.
    This lets us find all schedule variants of the same logical field.
    """
    fields = concordance.get("fields", {})
    index = {}  # type: Dict[str, List[Dict[str, str]]]
    for fname, fmeta in fields.items():
        if fmeta.get("type") == "(group)":
            continue
        xpaths = fmeta.get("xpaths", {})
        if not xpaths:
            continue
        # Get leaf pattern: strip the top-level schedule element
        # e.g. "/IRS990/USAddress/CityNm" -> "USAddress/CityNm"
        # e.g. "/IRS990N/WebsiteAddressTxt" -> "WebsiteAddressTxt"
        sample = next(iter(xpaths.values()))
        parts = sample.strip("/").split("/")
        if len(parts) < 2:
            continue
        leaf = "/".join(parts[1:])  # everything after the schedule root
        if leaf not in index:
            index[leaf] = []
        index[leaf].append(xpaths)
    return index


def load_field_specs(fields_dir, concordance_path):
    # type: (str, str) -> Tuple[List[FieldSpec], Dict[str, GroupSpec]]
    """Load all Field Finder JSON exports and classify fields.

    Returns (scalar_specs, group_specs) where group_specs maps
    group_name -> GroupSpec with child fields.
    """
    # Load concordance for supplemental metadata
    with open(concordance_path, "r") as f:
        concordance = json.load(f)

    # Build leaf index for cross-schedule xpath enrichment
    leaf_index = _build_concordance_leaf_index(concordance)

    # Collect all fields, merging duplicates across JSONs
    all_fields = OrderedDict()  # type: Dict[str, FieldSpec]

    json_files = sorted([
        fn for fn in os.listdir(fields_dir)
        if fn.endswith(".json")
    ])

    if not json_files:
        print("ERROR: No JSON files found in %s" % fields_dir)
        sys.exit(1)

    enriched_count = 0

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
            spec.xpaths = dict(fld.get("xpaths", {}))
            spec.source_jsons = [json_file]

            # Enrich xpaths from concordance: find all schedule variants
            # of the same leaf field and merge their version xpaths in
            sample_xpath = ""
            for v in sorted(spec.xpaths.keys()):
                sample_xpath = spec.xpaths[v]
                break
            if sample_xpath:
                parts = sample_xpath.strip("/").split("/")
                if len(parts) >= 2:
                    leaf = "/".join(parts[1:])
                    if leaf in leaf_index:
                        before = len(spec.xpaths)
                        for conc_xpaths in leaf_index[leaf]:
                            for ver, xpath in conc_xpaths.items():
                                if ver not in spec.xpaths:
                                    spec.xpaths[ver] = xpath
                        if len(spec.xpaths) > before:
                            enriched_count += 1

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
    if enriched_count:
        print("  Enriched %d fields with cross-schedule xpaths from concordance" % enriched_count)

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
# Load fields from concordance by schedule name
# ---------------------------------------------------------------------------

def load_schedule_fields(concordance_path, schedule_name):
    # type: (str, str) -> Tuple[List[FieldSpec], Dict[str, GroupSpec]]
    """Load all leaf fields for a schedule directly from the concordance.

    Returns (scalar_specs, group_specs) — same shape as load_field_specs().
    """
    with open(concordance_path, "r") as f:
        concordance = json.load(f)

    fields = concordance.get("fields", {})

    # Optionally load enrichment files from same directory
    conc_dir = os.path.dirname(concordance_path)
    freq_data = {}
    cat_data = {}

    freq_path = os.path.join(conc_dir, "field_frequency.json")
    if os.path.isfile(freq_path):
        with open(freq_path, "r") as f:
            freq_raw = json.load(f)
        freq_data = freq_raw.get("fields", {})

    cat_path = os.path.join(conc_dir, "category_mapping.json")
    if os.path.isfile(cat_path):
        with open(cat_path, "r") as f:
            cat_raw = json.load(f)
        cat_data = cat_raw.get("field_to_categories", {})

    # Filter fields for this schedule, skip groups
    matched = OrderedDict()  # type: Dict[str, dict]
    for fname, fmeta in fields.items():
        if fmeta.get("schedule") != schedule_name:
            continue
        if fmeta.get("type") == "(group)":
            continue
        matched[fname] = fmeta

    if not matched:
        print("ERROR: No leaf fields found for schedule '%s'" % schedule_name)
        print("Use --list-schedules to see available schedule names.")
        sys.exit(1)

    # Convert to FieldSpec objects
    all_fields = OrderedDict()  # type: Dict[str, FieldSpec]
    for fname, fmeta in matched.items():
        spec = FieldSpec()
        spec.field_name = fname
        spec.label = fmeta.get("label", "")
        spec.schedule = fmeta.get("schedule", "")
        spec.field_type = fmeta.get("type", "")
        spec.description = fmeta.get("description", "")
        spec.xpaths = fmeta.get("xpaths", {})
        spec.source_jsons = ["concordance:%s" % schedule_name]

        # Enrich with frequency
        if fname in freq_data:
            spec.frequency_pct = freq_data[fname].get("present_pct")

        # Enrich with categories
        if fname in cat_data:
            # cat_data[fname] is list of category paths like [["Expenses", "Compensation", ...]]
            # Flatten to top-level category names
            cats = []
            for path in cat_data[fname]:
                if path and path[0] not in cats:
                    cats.append(path[0])
            spec.categories = cats

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

    print("Loaded %d leaf fields for schedule '%s' from concordance" % (
        len(all_fields), schedule_name))

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


def list_schedules(concordance_path):
    # type: (str) -> None
    """Print all schedule names with field counts from the concordance."""
    with open(concordance_path, "r") as f:
        concordance = json.load(f)

    fields = concordance.get("fields", {})

    # Count leaf fields per schedule
    schedule_counts = {}  # type: Dict[str, int]
    schedule_groups = {}  # type: Dict[str, int]
    for fname, fmeta in fields.items():
        sched = fmeta.get("schedule", "")
        if not sched:
            continue
        if fmeta.get("type") == "(group)":
            schedule_groups[sched] = schedule_groups.get(sched, 0) + 1
        else:
            schedule_counts[sched] = schedule_counts.get(sched, 0) + 1

    print("Schedules in concordance (%d total):\n" % len(schedule_counts))
    print("  %-45s  %s  %s" % ("Schedule", "Leaf fields", "Groups"))
    print("  %-45s  %s  %s" % ("-" * 45, "-" * 11, "-" * 6))
    for sched in sorted(schedule_counts.keys()):
        leaf = schedule_counts[sched]
        grp = schedule_groups.get(sched, 0)
        print("  %-45s  %5d        %3d" % (sched, leaf, grp))


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


def resolve_xpath(xpaths, version, form_type=None):
    # type: (Dict[str, str], str, Optional[str]) -> Optional[str]
    """Resolve the best xpath for a given filing version.

    Fallback chain:
    1. Exact match
    2. Same year, latest sub-version
    3. Nearest prior version
    4. Nearest overall

    When form_type is provided (e.g. "990", "990EZ", "990PF"), candidates
    are filtered to prefer xpaths whose schedule root matches the form type.
    This prevents a 990 filing from getting a 990N xpath just because the
    version numbers are close.
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

    # Map form_type to expected schedule root in xpaths
    _FORM_TO_SCHEDULE = {
        "990": "IRS990",
        "990EZ": "IRS990EZ",
        "990PF": "IRS990PF",
        "990T": "IRS990T",
        "990N": "IRS990N",
    }

    # Roots that belong to other form types (not plain 990)
    _OTHER_FORM_ROOTS = {"IRS990EZ", "IRS990PF", "IRS990N", "IRS990T"}

    def _matches_form(xpath_val):
        # type: (str) -> bool
        """Check if an xpath's schedule root matches the filing's form type."""
        if not form_type:
            return True
        expected = _FORM_TO_SCHEDULE.get(form_type)
        if not expected:
            return True
        # xpath looks like "/IRS990/WebsiteAddressTxt"
        parts = xpath_val.strip("/").split("/")
        if not parts:
            return True
        root = parts[0]
        # For form 990, accept IRS990 plus all its schedules
        # (IRS990ScheduleI, IRS990ScheduleJ, etc.) and non-IRS990
        # roots (ContractorCompensationExpln, etc.), but reject
        # roots belonging to other form types (IRS990EZ, IRS990PF, etc.)
        if expected == "IRS990":
            return root not in _OTHER_FORM_ROOTS
        return root == expected

    def _pick_best(candidates):
        # type: (list) -> Optional[str]
        """From a list of version keys, pick the best one respecting form_type."""
        if not candidates:
            return None
        # Prefer candidates whose xpath matches the form type
        if form_type:
            matching = [v for v in candidates if _matches_form(xpaths[v])]
            if matching:
                return xpaths[matching[-1]]
            # No candidates match this form type — return None so the
            # fallback chain continues to a broader search
            return None
        # No form_type filter — use last candidate
        return xpaths[candidates[-1]]

    target_year, target_sub = parse_ver(version)
    available = sorted(xpaths.keys(), key=parse_ver)

    # 2. Same year, latest sub-version
    same_year = [v for v in available if parse_ver(v)[0] == target_year]
    if same_year:
        result = _pick_best(same_year)
        if result:
            return result

    # 3. Nearest prior version
    prior = [v for v in available if parse_ver(v) < (target_year, target_sub)]
    if prior:
        result = _pick_best(prior)
        if result:
            return result

    # 4. Nearest overall
    if available:
        result = _pick_best(available)
        if result:
            return result

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
# Xpath resolution cache
# ---------------------------------------------------------------------------

# Cache: (version, form_type) -> {field_name: resolved_xpath_or_None}
_xpath_cache = {}  # type: Dict[Tuple[str, Optional[str]], Dict[str, Optional[str]]]


def _resolve_all_xpaths(version, form_type, scalar_specs, group_specs):
    # type: (str, Optional[str], List[FieldSpec], Dict[str, GroupSpec]) -> Dict[str, Optional[str]]
    """Resolve xpaths for ALL fields at once for a (version, form_type) pair.

    Returns {field_name: resolved_xpath_or_None}. Results are cached so
    subsequent filings with the same version/form_type pay zero cost.
    """
    cache_key = (version, form_type)
    if cache_key in _xpath_cache:
        return _xpath_cache[cache_key]

    resolved = {}  # type: Dict[str, Optional[str]]

    # Scalar fields
    for spec in scalar_specs:
        resolved[spec.field_name] = resolve_xpath(spec.xpaths, version, form_type=form_type)

    # Group fields (children + derive container xpaths)
    for gname, gspec in group_specs.items():
        for child_spec in gspec.child_fields:
            resolved[child_spec.field_name] = resolve_xpath(
                child_spec.xpaths, version, form_type=form_type
            )

    _xpath_cache[cache_key] = resolved
    return resolved


# ---------------------------------------------------------------------------
# Field extraction
# ---------------------------------------------------------------------------

def extract_scalar_fields(return_data, ns, version, scalar_specs, form_type=None, resolved_xpaths=None):
    # type: (object, str, str, List[FieldSpec], Optional[str], Optional[Dict[str, Optional[str]]]) -> Dict[str, str]
    """Extract scalar field values from a filing.

    Returns {field_name: text_value}.
    If resolved_xpaths is provided, uses pre-cached xpath resolutions.
    """
    result = {}  # type: Dict[str, str]

    for spec in scalar_specs:
        if resolved_xpaths is not None:
            xpath = resolved_xpaths.get(spec.field_name)
        else:
            xpath = resolve_xpath(spec.xpaths, version, form_type=form_type)
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


def extract_group_instances(return_data, ns, version, group_specs, form_type=None, resolved_xpaths=None):
    # type: (object, str, str, Dict[str, GroupSpec], Optional[str], Optional[Dict[str, Optional[str]]]) -> Dict[str, List[Dict[str, str]]]
    """Extract repeating group instances from a filing.

    Returns {group_name: [instance_dict, ...]} where each instance_dict
    maps field_name -> text_value.
    If resolved_xpaths is provided, uses pre-cached xpath resolutions.
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
        if resolved_xpaths is not None:
            child_xpath = resolved_xpaths.get(child0.field_name)
        else:
            child_xpath = resolve_xpath(child0.xpaths, version, form_type=form_type)
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
                if resolved_xpaths is not None:
                    full_xpath = resolved_xpaths.get(child_spec.field_name)
                else:
                    full_xpath = resolve_xpath(child_spec.xpaths, version, form_type=form_type)
                if not full_xpath:
                    instance[child_spec.field_name] = ""
                    continue

                # Derive relative xpath (part after group container)
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
# Output — field reference (written once, not incremental)
# ---------------------------------------------------------------------------

def write_field_reference(output_dir, scalar_specs, group_specs):
    # type: (str, List[FieldSpec], Dict[str, GroupSpec]) -> None
    """Write field_reference.csv describing all extracted fields."""
    os.makedirs(output_dir, exist_ok=True)

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
# Single-filing worker (used by both serial and parallel modes)
# ---------------------------------------------------------------------------

def _process_one_filing(xml_path, scalar_specs, group_specs):
    # type: (str, List[FieldSpec], Dict[str, GroupSpec]) -> Optional[Tuple[Dict[str, str], Dict[str, List[Dict[str, str]]]]]
    """Parse one XML filing and extract all fields.

    Returns (scalar_row, group_rows_dict) or None on parse error.
    group_rows_dict maps group_name -> [instance_dict, ...].
    """
    result = parse_filing(xml_path)
    if result is None:
        return None

    header_dict, return_data, ns, version = result
    filing_form_type = header_dict.get("form_type", "")

    # Resolve all xpaths once for this (version, form_type), cached
    resolved = _resolve_all_xpaths(version, filing_form_type, scalar_specs, group_specs)

    # Extract scalar fields
    scalar_vals = extract_scalar_fields(
        return_data, ns, version, scalar_specs,
        form_type=filing_form_type, resolved_xpaths=resolved,
    )
    scalar_row = dict(header_dict)
    scalar_row.update(scalar_vals)

    # Extract group instances
    group_instances = extract_group_instances(
        return_data, ns, version, group_specs,
        form_type=filing_form_type, resolved_xpaths=resolved,
    )
    group_rows = {}  # type: Dict[str, List[Dict[str, str]]]
    for gname, instances in group_instances.items():
        rows = []
        for idx, inst in enumerate(instances):
            grow = {
                "EIN": header_dict["EIN"],
                "tax_period": header_dict["tax_period"],
                "instance_num": str(idx + 1),
            }
            grow.update(inst)
            rows.append(grow)
        group_rows[gname] = rows

    return scalar_row, group_rows


# ---------------------------------------------------------------------------
# Multiprocessing worker (needs module-level function for pickling)
# ---------------------------------------------------------------------------

# These are set by _init_worker and used by _worker_func
_worker_scalar_specs = None  # type: Optional[List[FieldSpec]]
_worker_group_specs = None   # type: Optional[Dict[str, GroupSpec]]


def _init_worker(scalar_specs, group_specs):
    # type: (List[FieldSpec], Dict[str, GroupSpec]) -> None
    """Initialize per-worker globals (avoids pickling specs with every task)."""
    global _worker_scalar_specs, _worker_group_specs, _xpath_cache
    _worker_scalar_specs = scalar_specs
    _worker_group_specs = group_specs
    _xpath_cache = {}  # fresh cache per worker


def _worker_func(xml_path):
    # type: (str) -> Optional[Tuple[str, Dict[str, str], Dict[str, List[Dict[str, str]]]]]
    """Worker entry point. Returns (xml_basename, scalar_row, group_rows) or None."""
    result = _process_one_filing(xml_path, _worker_scalar_specs, _worker_group_specs)
    if result is None:
        return None
    scalar_row, group_rows = result
    return (os.path.basename(xml_path), scalar_row, group_rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Extract fields from IRS 990 XML filings based on Field Finder exports or concordance schedules.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --limit 100 --verbose
  %(prog)s --fields-dir ./Fields --xml-dir ./990_xmls --output-dir ./extracted_output
  %(prog)s --schedule IRS990ScheduleJ --limit 100 --verbose
  %(prog)s --list-schedules
  %(prog)s --concordance ./concordance_output/field_lookup.json --limit 500
  %(prog)s --workers 8 --verbose
        """
    )
    parser.add_argument("--fields-dir", default=None,
                        help="Directory of Field Finder JSON exports (default: ./Fields)")
    parser.add_argument("--schedule", default=None,
                        help="Extract all leaf fields from this concordance schedule (e.g. IRS990ScheduleJ)")
    parser.add_argument("--list-schedules", action="store_true",
                        help="List available schedule names from the concordance and exit")
    parser.add_argument("--xml-dir", default="./data/xmls",
                        help="Directory of IRS 990 XML filings (default: ./data/xmls)")
    parser.add_argument("--concordance", default="./data/concordance/field_lookup.json",
                        help="Path to field_lookup.json (default: ./data/concordance/field_lookup.json)")
    parser.add_argument("--output-dir", default=None,
                        help="Output directory for CSVs (default: ./extracted_output/<schedule> or ./extracted_output)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max filings to process (0 = all, default: 0)")
    parser.add_argument("--workers", "-w", type=int, default=1,
                        help="Number of parallel workers (default: 1, 0 = all CPUs)")
    parser.add_argument("--verbose", action="store_true",
                        help="Show per-filing progress")

    args = parser.parse_args()

    # --list-schedules: print and exit
    if args.list_schedules:
        if not os.path.isfile(args.concordance):
            print("ERROR: Concordance file not found: %s" % args.concordance)
            sys.exit(1)
        list_schedules(args.concordance)
        sys.exit(0)

    # Validate mutual exclusivity
    if args.schedule and args.fields_dir is not None:
        print("ERROR: --schedule and --fields-dir are mutually exclusive.")
        sys.exit(1)

    # Default output directory
    if args.output_dir is None:
        if args.schedule:
            args.output_dir = os.path.join("./data/extracted", args.schedule)
        else:
            args.output_dir = "./data/extracted"

    # Validate concordance
    if not os.path.isfile(args.concordance):
        print("ERROR: Concordance file not found: %s" % args.concordance)
        sys.exit(1)

    # Load field specs
    print("Loading field specifications...")
    if args.schedule:
        scalar_specs, group_specs = load_schedule_fields(args.concordance, args.schedule)
    else:
        fields_dir = args.fields_dir if args.fields_dir is not None else "./Fields"
        if not os.path.isdir(fields_dir):
            print("ERROR: Fields directory not found: %s" % fields_dir)
            sys.exit(1)
        scalar_specs, group_specs = load_field_specs(fields_dir, args.concordance)

    if not os.path.isdir(args.xml_dir):
        print("ERROR: XML directory not found: %s" % args.xml_dir)
        sys.exit(1)

    # Find XML files
    print("\nScanning for XML filings in %s..." % args.xml_dir)
    xml_files = find_xml_files(args.xml_dir)
    print("Found %d XML files" % len(xml_files))

    if args.limit > 0:
        xml_files = xml_files[:args.limit]
        print("Processing first %d filings (--limit)" % args.limit)

    # Resolve worker count
    n_workers = args.workers
    if n_workers == 0:
        n_workers = multiprocessing.cpu_count()
    use_parallel = n_workers > 1 and len(xml_files) > 100

    if use_parallel:
        print("\nUsing %d parallel workers" % n_workers)
    else:
        if n_workers > 1 and len(xml_files) <= 100:
            print("\nToo few files for parallel mode, using serial")

    # Write field reference (doesn't depend on extraction)
    os.makedirs(args.output_dir, exist_ok=True)
    write_field_reference(args.output_dir, scalar_specs, group_specs)

    # Open CSV files for incremental writing
    header_cols = ["EIN", "tax_period", "org_name", "return_version", "form_type"]
    scalar_field_names = [s.field_name for s in scalar_specs]
    scalar_path = os.path.join(args.output_dir, "scalar_fields.csv")
    scalar_file = open(scalar_path, "w", newline="")
    scalar_writer = csv.DictWriter(
        scalar_file, fieldnames=header_cols + scalar_field_names,
        extrasaction="ignore",
    )
    scalar_writer.writeheader()

    group_writers = {}  # type: Dict[str, Tuple[object, csv.DictWriter]]
    group_row_counts = {}  # type: Dict[str, int]
    for gname, gspec in group_specs.items():
        child_field_names = [c.field_name for c in gspec.child_fields]
        group_header = ["EIN", "tax_period", "instance_num"] + child_field_names
        group_path = os.path.join(args.output_dir, "%s.csv" % gname)
        gf = open(group_path, "w", newline="")
        gw = csv.DictWriter(gf, fieldnames=group_header, extrasaction="ignore")
        gw.writeheader()
        group_writers[gname] = (gf, gw)
        group_row_counts[gname] = 0

    # Process filings
    print("\nExtracting fields...")
    processed = 0
    skipped = 0
    scalar_count = 0
    t_start = time.time()

    def _write_result(scalar_row, group_rows):
        # type: (Dict[str, str], Dict[str, List[Dict[str, str]]]) -> None
        """Write one filing's results to the open CSV files."""
        nonlocal scalar_count
        scalar_writer.writerow(scalar_row)
        scalar_count += 1
        for gname, rows in group_rows.items():
            if gname in group_writers:
                _, gw = group_writers[gname]
                for row in rows:
                    gw.writerow(row)
                group_row_counts[gname] = group_row_counts.get(gname, 0) + len(rows)

    if use_parallel:
        # Parallel mode with multiprocessing
        pool = multiprocessing.Pool(
            processes=n_workers,
            initializer=_init_worker,
            initargs=(scalar_specs, group_specs),
        )
        try:
            for result in pool.imap_unordered(_worker_func, xml_files, chunksize=64):
                if result is None:
                    skipped += 1
                    continue
                basename, scalar_row, group_rows = result
                _write_result(scalar_row, group_rows)
                processed += 1

                if args.verbose and (processed % 2000 == 0 or processed == 1):
                    elapsed = time.time() - t_start
                    rate = processed / elapsed if elapsed > 0 else 0
                    print("  Processed %d/%d filings (%.0f/sec, %d skipped)" % (
                        processed, len(xml_files), rate, skipped))
        finally:
            pool.close()
            pool.join()
    else:
        # Serial mode (with xpath cache still active)
        for xml_path in xml_files:
            result = _process_one_filing(xml_path, scalar_specs, group_specs)
            if result is None:
                skipped += 1
                if args.verbose:
                    print("  SKIP (parse error): %s" % os.path.basename(xml_path))
                continue

            scalar_row, group_rows = result
            _write_result(scalar_row, group_rows)
            processed += 1

            if args.verbose and (processed % 500 == 0 or processed == 1):
                elapsed = time.time() - t_start
                rate = processed / elapsed if elapsed > 0 else 0
                print("  Processed %d/%d filings (%.0f/sec, %d skipped)" % (
                    processed, len(xml_files), rate, skipped))

    # Close all CSV files
    scalar_file.close()
    for gf, gw in group_writers.values():
        gf.close()

    elapsed = time.time() - t_start
    print("\nProcessed %d filings in %.1f seconds (%.0f/sec, %d skipped)" % (
        processed, elapsed, processed / elapsed if elapsed > 0 else 0, skipped))

    # Summary
    print("Wrote %s (%d rows, %d field columns)" % (
        scalar_path, scalar_count, len(scalar_field_names)))
    for gname in group_specs:
        rc = group_row_counts.get(gname, 0)
        if rc > 0:
            group_path = os.path.join(args.output_dir, "%s.csv" % gname)
            print("Wrote %s (%d rows)" % (group_path, rc))

    print("\nDone!")


if __name__ == "__main__":
    main()
