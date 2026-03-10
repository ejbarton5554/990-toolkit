"""XML audit: extract all fields from a filing and check against extracted data."""

import os
import json
import re
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

try:
    from lxml import etree
except ImportError:
    etree = None  # type: ignore


# ---------------------------------------------------------------------------
# XML parsing — extract every leaf value regardless of concordance
# ---------------------------------------------------------------------------

def parse_xml(xml_path):
    # type: (str) -> Optional[Dict[str, Any]]
    """Parse a 990 XML filing and return header metadata + namespace info.

    Returns dict with keys:
        ein, tax_period, org_name, return_version, form_type,
        namespace, return_data (lxml Element), root (lxml Element)
    Or None on parse error.
    """
    if etree is None:
        raise ImportError("lxml is required for XML parsing")

    try:
        tree = etree.parse(xml_path)
    except etree.XMLSyntaxError:
        return None

    root = tree.getroot()

    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    version = root.get("returnVersion", "")

    header = root.find("%sReturnHeader" % ns)
    if header is None:
        return None

    ein_elem = header.find(".//%sFiler/%sEIN" % (ns, ns))
    ein = ein_elem.text.strip() if ein_elem is not None and ein_elem.text else ""

    tax_period_elem = header.find("%sTaxPeriodEndDt" % ns)
    tax_period = tax_period_elem.text.strip() if tax_period_elem is not None and tax_period_elem.text else ""

    org_name = ""
    name_elem = header.find(".//%sFiler/%sBusinessName/%sBusinessNameLine1Txt" % (ns, ns, ns))
    if name_elem is not None and name_elem.text:
        org_name = name_elem.text.strip()

    form_type_elem = header.find("%sReturnTypeCd" % ns)
    form_type = form_type_elem.text.strip() if form_type_elem is not None and form_type_elem.text else ""

    return_data = root.find("%sReturnData" % ns)

    return {
        "ein": ein,
        "tax_period": tax_period,
        "org_name": org_name,
        "return_version": version,
        "form_type": form_type,
        "namespace": ns,
        "return_data": return_data,
        "root": root,
    }


def _strip_ns(tag):
    # type: (str) -> str
    """Remove namespace prefix from a tag: {http://...}Foo -> Foo."""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def extract_all_fields(xml_path):
    # type: (str) -> Optional[Dict[str, Any]]
    """Extract every leaf field from a 990 XML filing.

    Walks the entire ReturnData element tree. For every element that has
    text content (a leaf), records the xpath and value.

    Returns dict with:
        header: {ein, tax_period, org_name, return_version, form_type}
        fields: OrderedDict of {xpath: value} for all leaf elements
        containers: list of xpaths that have children but no direct text
        field_count: total leaf fields found
        container_count: total container elements found
    Or None on parse error.
    """
    info = parse_xml(xml_path)
    if info is None:
        return None

    return_data = info["return_data"]
    if return_data is None:
        return {
            "header": {k: info[k] for k in ("ein", "tax_period", "org_name", "return_version", "form_type")},
            "fields": OrderedDict(),
            "containers": [],
            "field_count": 0,
            "container_count": 0,
        }

    fields = OrderedDict()  # type: OrderedDict[str, str]
    containers = []  # type: List[str]

    def _walk(elem, path_parts):
        # type: (Any, List[str]) -> None
        tag = _strip_ns(elem.tag)
        current_parts = path_parts + [tag]
        xpath = "/" + "/".join(current_parts)

        children = list(elem)
        if children:
            # Container element
            containers.append(xpath)
            for child in children:
                _walk(child, current_parts)
        else:
            # Leaf element
            text = elem.text.strip() if elem.text else ""
            if text:
                # Handle duplicate xpaths (repeating groups)
                if xpath in fields:
                    # Append instance number
                    i = 2
                    while "%s[%d]" % (xpath, i) in fields:
                        i += 1
                    xpath = "%s[%d]" % (xpath, i)
                fields[xpath] = text

    for child in return_data:
        _walk(child, [])

    return {
        "header": {k: info[k] for k in ("ein", "tax_period", "org_name", "return_version", "form_type")},
        "fields": fields,
        "containers": containers,
        "field_count": len(fields),
        "container_count": len(containers),
    }


def xml_tree_text(xml_path, save_path=None, full_path=False):
    # type: (str, Optional[str], bool) -> Optional[str]
    """Render the full XML filing as a text tree.

    Every element is shown with indentation reflecting depth. Leaf elements
    show their value inline. Attributes (like referenceDocumentId) are shown
    in parentheses.

    If full_path is True, each line shows the complete xpath instead of
    using indentation (e.g. "/IRS990/USAddress/CityNm = SANTA MONICA").

    If save_path is provided, writes the tree to that file.
    Returns the tree as a string, or None on parse error.
    """
    info = parse_xml(xml_path)
    if info is None:
        return None

    lines = []
    h = info
    lines.append("Filing: EIN=%s  Org=%s" % (h["ein"], h["org_name"]))
    lines.append("Tax Period: %s  Version: %s  Form: %s" % (
        h["tax_period"], h["return_version"], h["form_type"]))
    lines.append("")

    # Also render ReturnHeader
    root = info["root"]
    ns = info["namespace"]

    def _render(elem, depth, path_parts):
        # type: (Any, int, List[str]) -> None
        tag = _strip_ns(elem.tag)
        current_parts = path_parts + [tag]
        xpath = "/" + "/".join(current_parts)
        indent = "  " * depth

        # Collect non-namespace attributes
        attrs = []
        for k, v in elem.attrib.items():
            if not k.startswith("{") and k != "returnVersion":
                attrs.append("%s=%s" % (k, v))
        attr_str = " (%s)" % ", ".join(attrs) if attrs else ""

        children = list(elem)
        text = elem.text.strip() if elem.text else ""

        if children:
            if full_path:
                lines.append("%s%s" % (xpath, attr_str))
            else:
                lines.append("%s%s%s" % (indent, tag, attr_str))
            for child in children:
                _render(child, depth + 1, current_parts)
        else:
            if text:
                if full_path:
                    lines.append("%s%s = %s" % (xpath, attr_str, text))
                else:
                    lines.append("%s%s%s = %s" % (indent, tag, attr_str, text))
            else:
                if full_path:
                    lines.append("%s%s (empty)" % (xpath, attr_str))
                else:
                    lines.append("%s%s%s (empty)" % (indent, tag, attr_str))

    # Render full document: header + return data
    header = root.find("%sReturnHeader" % ns)
    if header is not None:
        if full_path:
            lines.append("/ReturnHeader")
        else:
            lines.append("ReturnHeader")
        for child in header:
            _render(child, 1, ["ReturnHeader"])
        lines.append("")

    return_data = info["return_data"]
    if return_data is not None:
        if not full_path:
            lines.append("ReturnData")
        for child in return_data:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            _render(child, 1, [tag])

    tree_text = "\n".join(lines)

    if save_path:
        with open(save_path, "w", encoding="utf-8") as f:
            f.write(tree_text)

    return tree_text


def extract_all_fields_flat(xml_path):
    # type: (str) -> Optional[pd.DataFrame]
    """Extract all leaf fields as a DataFrame.

    Returns DataFrame with columns: xpath, schedule, field_path, value.
    schedule is the top-level element (IRS990, IRS990ScheduleI, etc.).
    field_path is everything after the schedule root.
    """
    result = extract_all_fields(xml_path)
    if result is None:
        return None

    rows = []
    for xpath, value in result["fields"].items():
        parts = xpath.strip("/").split("/")
        schedule = parts[0] if parts else ""
        field_path = "/".join(parts[1:]) if len(parts) > 1 else ""
        rows.append({
            "xpath": xpath,
            "schedule": schedule,
            "field_path": field_path,
            "value": value,
        })

    df = pd.DataFrame(rows, columns=["xpath", "schedule", "field_path", "value"])
    return df


# ---------------------------------------------------------------------------
# Check extracted data against XML
# ---------------------------------------------------------------------------

def _load_concordance_xpath_index(concordance_path):
    # type: (str) -> Dict[str, str]
    """Load concordance xpath_index: {xpath -> canonical_name}."""
    with open(concordance_path, "r") as f:
        data = json.load(f)
    return data.get("xpath_index", {})


def _find_filing_in_csv(csv_path, ein, tax_period):
    # type: (str, str, str) -> Optional[pd.Series]
    """Find a filing's row in a CSV by EIN and tax_period."""
    df = pd.read_csv(csv_path, low_memory=False, dtype=str)
    mask = (df["EIN"] == str(ein))
    if "tax_period" in df.columns:
        mask = mask & (df["tax_period"] == str(tax_period))
    matches = df[mask]
    if len(matches) == 0:
        return None
    return matches.iloc[0]


def check_against_extracted(xml_path, extracted_dir, concordance_path=None):
    # type: (str, str, Optional[str]) -> Optional[Dict[str, Any]]
    """Compare all fields in an XML filing against the extracted CSVs.

    For each leaf field in the XML:
    - Checks if the xpath is in the concordance (if provided)
    - Checks if the value appears in the corresponding extracted CSV
    - Classifies each field as: matched, missing_from_extract, not_in_concordance

    Returns dict with:
        header: filing header info
        xml_field_count: total leaf fields in XML
        concordance_mapped: count of fields with concordance mapping
        not_in_concordance: count of fields without concordance mapping
        matched: list of {xpath, canonical_name, xml_value, csv_value}
        value_mismatch: list of {xpath, canonical_name, xml_value, csv_value}
        missing_from_extract: list of {xpath, canonical_name, xml_value}
            (in concordance but not found or empty in CSV)
        unmapped: list of {xpath, value}
            (not in concordance at all)
        match_rate: fraction of concordance-mapped fields that matched
        schedules_in_xml: list of schedule names found in the XML
    """
    all_fields = extract_all_fields(xml_path)
    if all_fields is None:
        return None

    header = all_fields["header"]
    ein = header["ein"]
    tax_period = header["tax_period"]

    # Load concordance xpath index if available
    xpath_index = {}  # type: Dict[str, str]
    if concordance_path and os.path.exists(concordance_path):
        xpath_index = _load_concordance_xpath_index(concordance_path)

    # Find the scalar_fields row for this filing
    scalar_csv = os.path.join(extracted_dir, "scalar_fields.csv")
    scalar_row = None
    if os.path.exists(scalar_csv):
        scalar_row = _find_filing_in_csv(scalar_csv, ein, tax_period)

    # Collect group CSVs: load rows for this EIN+tax_period
    group_data = {}  # type: Dict[str, pd.DataFrame]
    for fn in os.listdir(extracted_dir):
        if fn.endswith(".csv") and fn != "scalar_fields.csv" and fn != "field_reference.csv":
            csv_path = os.path.join(extracted_dir, fn)
            try:
                df = pd.read_csv(csv_path, low_memory=False, dtype=str)
            except Exception:
                continue
            if "EIN" not in df.columns:
                continue
            mask = df["EIN"] == str(ein)
            if "tax_period" in df.columns:
                mask = mask & (df["tax_period"] == str(tax_period))
            rows = df[mask]
            if len(rows) > 0:
                group_name = os.path.splitext(fn)[0]
                group_data[group_name] = rows

    # Also check schedule subdirectories
    for subdir in os.listdir(extracted_dir):
        subdir_path = os.path.join(extracted_dir, subdir)
        if not os.path.isdir(subdir_path):
            continue
        for fn in os.listdir(subdir_path):
            if fn.endswith(".csv") and fn != "scalar_fields.csv" and fn != "field_reference.csv":
                csv_path = os.path.join(subdir_path, fn)
                try:
                    df = pd.read_csv(csv_path, low_memory=False, dtype=str)
                except Exception:
                    continue
                if "EIN" not in df.columns:
                    continue
                mask = df["EIN"] == str(ein)
                if "tax_period" in df.columns:
                    mask = mask & (df["tax_period"] == str(tax_period))
                rows = df[mask]
                if len(rows) > 0:
                    key = "%s/%s" % (subdir, os.path.splitext(fn)[0])
                    group_data[key] = rows

    # Classify each XML field
    matched = []
    value_mismatch = []
    missing_from_extract = []
    unmapped = []

    # Collect all extracted values for quick lookup
    extracted_values = {}  # type: Dict[str, str]
    if scalar_row is not None:
        for col in scalar_row.index:
            val = scalar_row[col]
            if pd.notna(val) and str(val).strip():
                extracted_values[col] = str(val).strip()

    for gname, gdf in group_data.items():
        for _, row in gdf.iterrows():
            for col in row.index:
                val = row[col]
                if pd.notna(val) and str(val).strip():
                    key = "%s.%s" % (gname, col)
                    extracted_values[key] = str(val).strip()

    schedules_in_xml = set()

    for xpath, xml_value in all_fields["fields"].items():
        # Strip instance numbers for concordance lookup
        clean_xpath = re.sub(r"\[\d+\]$", "", xpath)

        parts = clean_xpath.strip("/").split("/")
        if parts:
            schedules_in_xml.add(parts[0])

        canonical = xpath_index.get(clean_xpath)

        if canonical:
            # Field is in concordance — check if it was extracted
            # Check scalar row first
            csv_value = None

            # The canonical name might be the column name, or the leaf name
            # Try multiple matching strategies
            leaf_name = parts[-1] if parts else ""
            for check_name in (canonical, leaf_name):
                if check_name in extracted_values:
                    csv_value = extracted_values[check_name]
                    break

            # Also check group data by looking for the value
            if csv_value is None:
                for gname, gdf in group_data.items():
                    for col in gdf.columns:
                        if col in ("EIN", "tax_period", "instance_num"):
                            continue
                        col_vals = gdf[col].dropna().astype(str).str.strip()
                        if xml_value in col_vals.values:
                            csv_value = xml_value
                            break
                    if csv_value is not None:
                        break

            if csv_value is not None:
                if csv_value == xml_value:
                    matched.append({
                        "xpath": xpath,
                        "canonical_name": canonical,
                        "xml_value": xml_value,
                        "csv_value": csv_value,
                    })
                else:
                    value_mismatch.append({
                        "xpath": xpath,
                        "canonical_name": canonical,
                        "xml_value": xml_value,
                        "csv_value": csv_value,
                    })
            else:
                missing_from_extract.append({
                    "xpath": xpath,
                    "canonical_name": canonical,
                    "xml_value": xml_value,
                })
        else:
            unmapped.append({
                "xpath": xpath,
                "value": xml_value,
            })

    total_mapped = len(matched) + len(value_mismatch) + len(missing_from_extract)
    match_rate = len(matched) / total_mapped if total_mapped > 0 else 0.0

    return {
        "header": header,
        "xml_field_count": all_fields["field_count"],
        "concordance_mapped": total_mapped,
        "not_in_concordance": len(unmapped),
        "matched": matched,
        "value_mismatch": value_mismatch,
        "missing_from_extract": missing_from_extract,
        "unmapped": unmapped,
        "match_rate": round(match_rate, 3),
        "schedules_in_xml": sorted(schedules_in_xml),
    }


def audit_report(xml_path, extracted_dir, concordance_path=None):
    # type: (str, str, Optional[str]) -> Optional[str]
    """Generate a human-readable audit report comparing XML to extracted data.

    Returns a formatted string report, or None on parse error.
    """
    result = check_against_extracted(xml_path, extracted_dir, concordance_path)
    if result is None:
        return None

    h = result["header"]
    lines = [
        "XML Audit Report",
        "=" * 60,
        "EIN: %s" % h["ein"],
        "Org: %s" % h["org_name"],
        "Tax Period: %s" % h["tax_period"],
        "Version: %s" % h["return_version"],
        "Form: %s" % h["form_type"],
        "Schedules: %s" % ", ".join(result["schedules_in_xml"]),
        "",
        "Fields in XML: %d" % result["xml_field_count"],
        "In concordance: %d" % result["concordance_mapped"],
        "Not in concordance: %d" % result["not_in_concordance"],
        "",
        "Matched in extract: %d" % len(result["matched"]),
        "Value mismatch: %d" % len(result["value_mismatch"]),
        "Missing from extract: %d" % len(result["missing_from_extract"]),
        "Match rate: %.1f%%" % (result["match_rate"] * 100),
    ]

    if result["value_mismatch"]:
        lines.append("")
        lines.append("VALUE MISMATCHES:")
        lines.append("-" * 60)
        for item in result["value_mismatch"]:
            lines.append("  %s" % item["xpath"])
            lines.append("    XML: %s" % item["xml_value"][:80])
            lines.append("    CSV: %s" % item["csv_value"][:80])

    if result["missing_from_extract"]:
        lines.append("")
        lines.append("MISSING FROM EXTRACT (in concordance but not in CSV):")
        lines.append("-" * 60)
        for item in result["missing_from_extract"][:50]:
            lines.append("  %-50s = %s" % (item["xpath"][:50], item["xml_value"][:30]))
        if len(result["missing_from_extract"]) > 50:
            lines.append("  ... and %d more" % (len(result["missing_from_extract"]) - 50))

    if result["unmapped"]:
        lines.append("")
        lines.append("NOT IN CONCORDANCE:")
        lines.append("-" * 60)
        for item in result["unmapped"][:50]:
            lines.append("  %-50s = %s" % (item["xpath"][:50], item["value"][:30]))
        if len(result["unmapped"]) > 50:
            lines.append("  ... and %d more" % (len(result["unmapped"]) - 50))

    return "\n".join(lines)
