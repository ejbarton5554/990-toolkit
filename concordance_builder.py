#!/usr/bin/env python3
from __future__ import annotations
"""
IRS 990 Schema Concordance Builder
===================================
Reads IRS 990 .xsd schema files across multiple version-years and produces:

1. A machine-readable concordance CSV mapping every xpath to a canonical
   variable name, with version coverage and data types.
2. A human-readable reference (Markdown) explaining every field in plain
   language, organized by schedule and form part.

Usage:
    python concordance_builder.py --schema-dir ./schemas --output-dir ./output

The schema directory should be organized as:
    schemas/
        2013v3.0/
            IRS990.xsd
            IRS990ScheduleA.xsd
            ...
        2014v5.0/
            ...
        ...

Each version folder should contain the .xsd files published by the IRS for
that schema version.
"""

import argparse
import csv
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from lxml import etree

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

XS = "http://www.w3.org/2001/XMLSchema"
XS_PREFIX = f"{{{XS}}}"

# Schedules / top-level forms we care about
KNOWN_FORMS = {
    "IRS990": "Form 990 (Full)",
    "IRS990EZ": "Form 990-EZ (Short)",
    "IRS990PF": "Form 990-PF (Private Foundation)",
    "IRS990T": "Form 990-T (Exempt Organization Business Income Tax)",
    "IRS990TScheduleA": "Form 990-T Schedule A (Unrelated Business Taxable Income)",
    "IRS990ScheduleA": "Schedule A – Public Charity Status and Public Support",
    "IRS990ScheduleB": "Schedule B – Contributors (Restricted)",
    "IRS990ScheduleC": "Schedule C – Political Campaign and Lobbying Activities",
    "IRS990ScheduleD": "Schedule D – Supplemental Financial Statements",
    "IRS990ScheduleE": "Schedule E – Schools",
    "IRS990ScheduleF": "Schedule F – Activities Outside the United States",
    "IRS990ScheduleG": "Schedule G – Fundraising and Gaming Activities",
    "IRS990ScheduleH": "Schedule H – Hospitals",
    "IRS990ScheduleI": "Schedule I – Grants and Other Assistance (Domestic)",
    "IRS990ScheduleJ": "Schedule J – Compensation Information",
    "IRS990ScheduleK": "Schedule K – Tax-Exempt Bonds",
    "IRS990ScheduleL": "Schedule L – Transactions with Interested Persons",
    "IRS990ScheduleM": "Schedule M – Noncash Contributions",
    "IRS990ScheduleN": "Schedule N – Liquidation, Termination, Dissolution, or Significant Disposition of Assets",
    "IRS990ScheduleO": "Schedule O – Supplemental Information",
    "IRS990ScheduleR": "Schedule R – Related Organizations and Unrelated Partnerships",
    "ReturnHeader": "Return Header (Filing Metadata)",
    # Related tax forms that appear in 990 filings
    "IRS4562": "Form 4562 – Depreciation and Amortization",
    "IRS4136": "Form 4136 – Credit for Federal Tax Paid on Fuels",
    "IRS3800": "Form 3800 – General Business Credit",
    "IRS8949": "Form 8949 – Sales and Dispositions of Capital Assets",
    "IRS1041ScheduleD": "Form 1041 Schedule D – Capital Gains and Losses",
    "IRS1041ScheduleI": "Form 1041 Schedule I – Alternative Minimum Tax",
    "IRS1120ScheduleD": "Form 1120 Schedule D – Capital Gains and Losses",
    # Dependency schedules (990-PF and common)
    "ExpenditureResponsibilityStmt": "Expenditure Responsibility Statement",
    "TransfersToControlledEntities": "Transfers to Controlled Entities Schedule",
    "TransfersFrmControlledEntities": "Transfers from Controlled Entities Schedule",
    "LoansFromOfficersSchedule": "Loans from Officers Schedule",
    "MortgagesAndNotesPayableSch": "Mortgages and Notes Payable Schedule",
    "OtherNotesLoansRcvblLongSch": "Other Notes/Loans Receivable (Long) Schedule",
    "AffiliateListing": "Affiliate Listing",
    "CompensationExplanation": "Compensation Explanation",
    "ContractorCompensationExpln": "Contractor Compensation Explanation",
    "AmortizationSchedule": "Amortization Schedule",
    "GainLossSaleOtherAssetsSch": "Gain/Loss from Sale of Other Assets Schedule",
    "AccountingFeesSchedule": "Accounting Fees Schedule",
    "TaxesSchedule": "Taxes Schedule",
    "OtherExpensesSchedule": "Other Expenses Schedule",
    "InvestmentsCorpStockSchedule": "Investments – Corporate Stock Schedule",
    "InvestmentsOtherSchedule2": "Investments – Other Schedule",
    "OtherIncreasesSchedule": "Other Increases Schedule",
    "OtherDecreasesSchedule": "Other Decreases Schedule",
    "LegalFeesSchedule": "Legal Fees Schedule",
    "DepreciationSchedule": "Depreciation Schedule",
    "OtherAssetsSchedule": "Other Assets Schedule",
    "OtherLiabilitiesSchedule": "Other Liabilities Schedule",
    "ActyNotPreviouslyRptExpln": "Activities Not Previously Reported Explanation",
}

# Map IRS XSD types to human-friendly descriptions
TYPE_DESCRIPTIONS = {
    "BooleanType": "Yes/No",
    "CheckboxType": "Checkbox (X if checked)",
    "USAmountType": "Dollar amount (USD)",
    "USAmountNNType": "Dollar amount, non-negative (USD)",
    "IntegerType": "Whole number",
    "IntegerNNType": "Whole number, non-negative",
    "LargeRatioType": "Ratio / percentage",
    "RatioType": "Ratio / percentage",
    "DecimalType": "Decimal number",
    "LineExplanationType": "Free-text explanation",
    "ExplanationType": "Free-text explanation",
    "ShortExplanationType": "Short free-text explanation",
    "ShortDescriptionType": "Short description",
    "PersonNameType": "Person name",
    "BusinessNameType": "Organization name",
    "BusinessNameLine1Type": "Organization name (line 1)",
    "BusinessNameLine2Type": "Organization name (line 2)",
    "BusinessNameLine1Txt": "Organization name (line 1)",
    "EINType": "Employer Identification Number",
    "SSNType": "Social Security Number",
    "YearType": "Four-digit year",
    "DateType": "Date",
    "TimestampType": "Date and time",
    "PhoneNumberType": "Phone number",
    "ZIPCodeType": "ZIP code",
    "StateType": "US state abbreviation",
    "CountryType": "Country code",
    "StringType": "Text",
    "StreetAddressType": "Street address",
    "CityType": "City name",
    "CountType": "Count (whole number)",
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SchemaElement:
    """One element extracted from an XSD file."""
    xpath: str                          # Full xpath from root
    element_name: str                   # Leaf element name
    xsd_type: str = ""                  # IRS/XSD type string
    documentation: str = ""             # xs:documentation text if present
    annotation: str = ""                # xs:annotation / comment text
    parent_group: str = ""              # Enclosing complex type / group name
    is_repeating: bool = False          # Inside a repeating group?
    min_occurs: str = ""
    max_occurs: str = ""
    schedule: str = ""                  # Which form / schedule
    version: str = ""                   # Schema version string


@dataclass
class CanonicalField:
    """A single logical field that may appear under different xpaths across
    schema versions."""
    canonical_name: str                 # Stable machine-readable name
    schedule: str                       # Parent form/schedule
    human_label: str = ""               # Plain-English label
    human_description: str = ""         # Longer explanation
    data_type: str = ""                 # Friendly type
    raw_xsd_type: str = ""              # Original XSD type
    xpaths_by_version: dict = field(default_factory=dict)  # {version: xpath}
    parent_group: str = ""              # Repeating group if any
    is_repeating: bool = False
    line_number: str = ""               # Form line number if known
    versions_present: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Schema parsing
# ---------------------------------------------------------------------------

class GlobalTypeCollector:
    """First pass: collect ALL named complex types AND named groups from ALL
    .xsd files in a version directory, following xs:include references.
    
    Real IRS schemas split types across files:
      - efileTypes.xsd (common types like USAmountType, BusinessNameType)
      - Each form .xsd (form-specific types like IRS990ScheduleDType)
    And use xs:include with relative paths like '../../../Common/efileTypes.xsd'
    """

    def __init__(self):
        self.complex_types: dict[str, etree._Element] = {}
        self.groups: dict[str, etree._Element] = {}
        self._visited_files: set[str] = set()

    def collect_from_file(self, xsd_path: str):
        """Extract all named complex types and groups from one .xsd file,
        following xs:include references."""
        real_path = os.path.realpath(xsd_path)
        if real_path in self._visited_files:
            return
        self._visited_files.add(real_path)

        try:
            tree = etree.parse(xsd_path)
        except (etree.XMLSyntaxError, OSError):
            return
        root = tree.getroot()

        # Collect named complex types
        for ct in root.iter(f"{XS_PREFIX}complexType"):
            name = ct.get("name", "")
            if name:
                self.complex_types[name] = ct

        # Collect named groups (xs:group with a name attribute)
        for grp in root.iter(f"{XS_PREFIX}group"):
            name = grp.get("name", "")
            if name:
                self.groups[name] = grp

        # Follow xs:include references
        base_dir = os.path.dirname(xsd_path)
        for inc in root.iter(f"{XS_PREFIX}include"):
            schema_loc = inc.get("schemaLocation", "")
            if schema_loc:
                inc_path = os.path.normpath(os.path.join(base_dir, schema_loc))
                if os.path.exists(inc_path):
                    self.collect_from_file(inc_path)

        # Follow xs:import references too
        for imp in root.iter(f"{XS_PREFIX}import"):
            schema_loc = imp.get("schemaLocation", "")
            if schema_loc:
                imp_path = os.path.normpath(os.path.join(base_dir, schema_loc))
                if os.path.exists(imp_path):
                    self.collect_from_file(imp_path)

    def collect_from_directory(self, dir_path: str):
        """Collect complex types from every .xsd file in a directory tree,
        recursing into subdirectories. Also follows xs:include references."""
        for xsd_file in sorted(Path(dir_path).rglob("*.xsd")):
            self.collect_from_file(str(xsd_file))


class SchemaParser:
    """Parse a single .xsd file and extract all element definitions with
    their xpaths, types, and documentation."""

    def __init__(self, xsd_path: str, version: str,
                 global_complex_types: dict[str, etree._Element] = None,
                 global_groups: dict[str, etree._Element] = None):
        self.xsd_path = xsd_path
        self.version = version
        self.elements: list[SchemaElement] = []
        self.schedule = self._guess_schedule(xsd_path)
        # Use global types/groups if provided, otherwise file-local only
        self._global_complex_types = global_complex_types or {}
        self._global_groups = global_groups or {}

    @staticmethod
    def _guess_schedule(path: str) -> str:
        """Infer the schedule name from the filename."""
        stem = Path(path).stem
        # Handle common patterns: IRS990.xsd, IRS990ScheduleJ.xsd, etc.
        for name in sorted(KNOWN_FORMS.keys(), key=len, reverse=True):
            if stem.startswith(name) or stem == name:
                return name
        # Also handle ReturnHeader variants
        if "ReturnHeader" in stem or "returnheader" in stem.lower():
            return "ReturnHeader"
        return stem

    @staticmethod
    def _strip_ns_prefix(type_str: str) -> str:
        """Strip namespace prefix from a type reference.
        'efile:IRS990Type' -> 'IRS990Type', 'xsd:string' -> 'string'."""
        if ":" in type_str:
            return type_str.split(":", 1)[1]
        return type_str

    def parse(self) -> list[SchemaElement]:
        """Parse the XSD and return all extracted elements."""
        try:
            tree = etree.parse(self.xsd_path)
        except etree.XMLSyntaxError as e:
            print(f"  WARNING: Could not parse {self.xsd_path}: {e}",
                  file=sys.stderr)
            return []

        root = tree.getroot()
        self._walk_schema(root)
        return self.elements

    def _walk_schema(self, root):
        """Walk the schema tree extracting element definitions."""
        # Collect file-local complex types and merge with global ones
        complex_types = dict(self._global_complex_types)  # start with global
        for ct in root.iter(f"{XS_PREFIX}complexType"):
            name = ct.get("name", "")
            if name:
                complex_types[name] = ct

        # Collect file-local groups and merge with global ones
        groups = dict(self._global_groups)
        for grp in root.iter(f"{XS_PREFIX}group"):
            name = grp.get("name", "")
            if name:
                groups[name] = grp

        # Detect actual schedule name from the first top-level xs:element.
        # This is more reliable than filename-based guessing because
        # dependency files often have different names than their root element
        # (e.g., TransfersToControlledEntitiesSchedule.xsd defines
        #  element name="TransfersToControlledEntities")
        for elem in root:
            tag = etree.QName(elem.tag).localname if isinstance(elem.tag, str) else ""
            if tag == "element" and elem.get("name", ""):
                self.schedule = elem.get("name")
                break

        # Start from direct children of the schema root that are elements.
        for elem in root:
            tag = etree.QName(elem.tag).localname if isinstance(elem.tag, str) else ""
            if tag == "element":
                self._process_element(elem, "", "", False,
                                      complex_types, groups)

    def _process_element(self, elem, parent_xpath: str, parent_group: str,
                         is_repeating: bool, complex_types: dict,
                         groups: dict, depth: int = 0):
        """Recursively process an xs:element and its children."""
        if depth > 30:  # Safety valve
            return

        name = elem.get("name", "")
        if not name:
            # Could be a ref — strip namespace prefix from refs too
            name = elem.get("ref", "")
            if name:
                name = self._strip_ns_prefix(name)
            if not name:
                return

        # Build xpath — for top-level form elements, prepend schedule name
        if not parent_xpath:
            xpath = f"/{self.schedule}/{name}" if name != self.schedule else f"/{name}"
        else:
            xpath = f"{parent_xpath}/{name}"

        xsd_type_raw = elem.get("type", "")
        xsd_type = self._strip_ns_prefix(xsd_type_raw) if xsd_type_raw else ""
        min_occurs = elem.get("minOccurs", "")
        max_occurs = elem.get("maxOccurs", "")

        # Detect repeating groups
        elem_is_repeating = is_repeating or max_occurs == "unbounded"

        # Extract documentation
        doc_text = self._get_documentation(elem)

        # Determine if this is a leaf (has a simple type) or branch (complex)
        inline_complex = elem.find(f"{XS_PREFIX}complexType")

        if inline_complex is not None:
            # This element has an inline complex type.
            # It might be:
            #   (a) A direct sequence/all/choice with child elements
            #   (b) A complexContent/extension pointing to a named type
            #   (c) A simpleContent/extension — STILL A LEAF, just with attributes
            #       e.g., BooleanType + referenceDocumentId attribute

            # Check for simpleContent first — these are leaf values with
            # added attributes (very common in IRS schemas for referenceDocumentId)
            simple_content = inline_complex.find(f"{XS_PREFIX}simpleContent")
            if simple_content is not None:
                # This is a leaf element. Extract the base type from the extension.
                sc_ext = simple_content.find(f"{XS_PREFIX}extension")
                if sc_ext is not None:
                    base_type = sc_ext.get("base", "")
                    if base_type:
                        base_type = self._strip_ns_prefix(base_type)
                else:
                    base_type = ""
                sc_restrict = simple_content.find(f"{XS_PREFIX}restriction")
                if sc_restrict is not None and not base_type:
                    base_type = sc_restrict.get("base", "")
                    if base_type:
                        base_type = self._strip_ns_prefix(base_type)
                se = SchemaElement(
                    xpath=xpath, element_name=name,
                    xsd_type=base_type or xsd_type_raw,
                    documentation=doc_text, parent_group=parent_group,
                    is_repeating=is_repeating, min_occurs=min_occurs,
                    max_occurs=max_occurs, schedule=self.schedule,
                    version=self.version,
                )
                self.elements.append(se)
                return

            # Check for complexContent/extension pattern
            ext_type = self._resolve_extension(inline_complex)
            if ext_type and ext_type in complex_types:
                # The anonymous type extends a named type — recurse into
                # the named type AND any additional elements in the extension
                ct = complex_types[ext_type]
                group_name = name if elem_is_repeating else parent_group

                if elem_is_repeating and name:
                    se = SchemaElement(
                        xpath=xpath, element_name=name, xsd_type="(group)",
                        documentation=doc_text, parent_group=parent_group,
                        is_repeating=True, min_occurs=min_occurs,
                        max_occurs=max_occurs, schedule=self.schedule,
                        version=self.version,
                    )
                    self.elements.append(se)

                # Recurse into the base named type
                for child in self._get_direct_child_elements(ct, groups, complex_types):
                    self._process_element(
                        child, xpath, group_name if elem_is_repeating else parent_group,
                        elem_is_repeating, complex_types, groups, depth + 1)

                # Also recurse into any elements added in the extension itself
                ext_elem = inline_complex.find(f"{XS_PREFIX}complexContent")
                if ext_elem is not None:
                    ext_node = ext_elem.find(f"{XS_PREFIX}extension")
                    if ext_node is not None:
                        for child in self._get_direct_child_elements(ext_node, groups, complex_types):
                            self._process_element(
                                child, xpath,
                                group_name if elem_is_repeating else parent_group,
                                elem_is_repeating, complex_types, groups,
                                depth + 1)
            else:
                # Regular inline complex type with direct children
                group_name = name if elem_is_repeating else parent_group

                if elem_is_repeating and name:
                    se = SchemaElement(
                        xpath=xpath, element_name=name, xsd_type="(group)",
                        documentation=doc_text, parent_group=parent_group,
                        is_repeating=True, min_occurs=min_occurs,
                        max_occurs=max_occurs, schedule=self.schedule,
                        version=self.version,
                    )
                    self.elements.append(se)

                for child in self._get_direct_child_elements(inline_complex, groups, complex_types):
                    self._process_element(
                        child, xpath,
                        group_name if elem_is_repeating else parent_group,
                        elem_is_repeating, complex_types, groups, depth + 1)

        elif xsd_type and xsd_type in complex_types:
            # Named complex type reference — recurse into it
            ct = complex_types[xsd_type]
            group_name = name if elem_is_repeating else parent_group

            if elem_is_repeating and name:
                se = SchemaElement(
                    xpath=xpath, element_name=name, xsd_type="(group)",
                    documentation=doc_text, parent_group=parent_group,
                    is_repeating=True, min_occurs=min_occurs,
                    max_occurs=max_occurs, schedule=self.schedule,
                    version=self.version,
                )
                self.elements.append(se)

            for child in self._get_direct_child_elements(ct, groups, complex_types):
                self._process_element(
                    child, xpath,
                    group_name if elem_is_repeating else parent_group,
                    elem_is_repeating, complex_types, groups, depth + 1)
        else:
            # Leaf element — record it
            se = SchemaElement(
                xpath=xpath, element_name=name,
                xsd_type=xsd_type_raw,  # preserve original type string
                documentation=doc_text, parent_group=parent_group,
                is_repeating=is_repeating, min_occurs=min_occurs,
                max_occurs=max_occurs, schedule=self.schedule,
                version=self.version,
            )
            self.elements.append(se)

    @staticmethod
    def _resolve_extension(complex_node) -> str:
        """Check if a complexType uses complexContent/extension and return
        the base type name, or empty string."""
        cc = complex_node.find(f"{XS_PREFIX}complexContent")
        if cc is not None:
            ext = cc.find(f"{XS_PREFIX}extension")
            if ext is not None:
                base = ext.get("base", "")
                if ":" in base:
                    base = base.split(":", 1)[1]
                return base
        return ""

    def _get_direct_child_elements(self, complex_node, groups: dict = None,
                                    complex_types: dict = None):
        """Get direct xs:element children from a complexType or group node.
        Looks inside xs:sequence, xs:all, xs:choice containers.
        Resolves xs:group ref= references and follows extension base types."""
        if groups is None:
            groups = {}
        if complex_types is None:
            complex_types = {}
        results = []
        self._collect_child_elements(complex_node, groups, complex_types,
                                     results, depth=0)
        return results

    def _collect_child_elements(self, node, groups: dict, complex_types: dict,
                                results: list, depth: int):
        """Recursively collect xs:element children, expanding group refs,
        nested sequence/choice/all containers, AND following extension
        base type inheritance chains."""
        if depth > 15:
            return
        for child in node:
            tag = etree.QName(child.tag).localname if isinstance(child.tag, str) else ""
            if tag == "element":
                results.append(child)
            elif tag in ("sequence", "all", "choice"):
                self._collect_child_elements(child, groups, complex_types,
                                             results, depth + 1)
            elif tag == "group":
                # xs:group ref="SomeGroupName" — expand it
                ref = child.get("ref", "")
                if ref:
                    ref = self._strip_ns_prefix(ref)
                    if ref in groups:
                        self._collect_child_elements(
                            groups[ref], groups, complex_types,
                            results, depth + 1)
            elif tag == "complexContent":
                # Look inside for extension/restriction
                for sub in child:
                    stag = etree.QName(sub.tag).localname if isinstance(sub.tag, str) else ""
                    if stag in ("extension", "restriction"):
                        # CRITICAL: follow the base= attribute to get
                        # inherited fields from the parent type
                        base = sub.get("base", "")
                        if base:
                            base = self._strip_ns_prefix(base)
                            if base in complex_types:
                                self._collect_child_elements(
                                    complex_types[base], groups,
                                    complex_types, results, depth + 1)
                        # Then also get locally-added elements
                        self._collect_child_elements(
                            sub, groups, complex_types, results, depth + 1)

    @staticmethod
    def _get_documentation(elem) -> str:
        """Extract xs:documentation text from an element's annotation."""
        ann = elem.find(f"{XS_PREFIX}annotation")
        if ann is not None:
            doc = ann.find(f"{XS_PREFIX}documentation")
            if doc is not None and doc.text:
                return doc.text.strip()
        return ""


# ---------------------------------------------------------------------------
# Cross-version matching and canonicalization
# ---------------------------------------------------------------------------

class ConcordanceBuilder:
    """Takes parsed elements from all versions and builds a unified
    concordance mapping."""

    def __init__(self):
        # {(schedule, xpath_tail): [SchemaElement, ...]}
        self.elements_by_xpath: dict[str, list[SchemaElement]] = defaultdict(list)
        # All unique versions seen
        self.versions: list[str] = []
        # Final canonical fields
        self.canonical_fields: list[CanonicalField] = []

    def add_version(self, version: str, elements: list[SchemaElement]):
        """Register all elements from one schema version."""
        if version not in self.versions:
            self.versions.append(version)
        for elem in elements:
            key = elem.xpath  # full xpath is the primary key
            self.elements_by_xpath[key].append(elem)

    def build(self):
        """Match elements across versions and produce canonical fields."""
        self.versions.sort(key=_version_sort_key)

        # Step 1: Group by exact xpath match (covers ~90% of fields)
        exact_groups = defaultdict(list)  # xpath -> [SchemaElement]
        for xpath, elems in self.elements_by_xpath.items():
            exact_groups[xpath].extend(elems)

        # Step 2: Build canonical fields from exact groups
        seen_xpaths = set()
        for xpath in sorted(exact_groups.keys()):
            if xpath in seen_xpaths:
                continue
            elems = exact_groups[xpath]
            if not elems:
                continue
            cf = self._make_canonical(xpath, elems)
            self.canonical_fields.append(cf)
            seen_xpaths.add(xpath)

        # Step 3: Try fuzzy matching for xpaths that only appear in some
        # versions (handles renames between schema eras)
        self._fuzzy_match_orphans(seen_xpaths)

        # Step 4: Merge single-version fields that have identical
        # descriptions within the same schedule + group (catches IRS
        # abbreviation renames like BaseCompensationFilingOrgAmt →
        # BsCmpnstnFlngOrgAmt)
        self._merge_by_description()

        # Sort by schedule then xpath for clean output
        self.canonical_fields.sort(key=lambda f: (f.schedule, f.canonical_name))

    def _make_canonical(self, xpath: str, elems: list[SchemaElement]) -> CanonicalField:
        """Create a CanonicalField from a set of elements sharing an xpath."""
        # Use the most recent element for metadata
        elems_sorted = sorted(elems, key=lambda e: _version_sort_key(e.version))
        latest = elems_sorted[-1]

        canonical_name = self._xpath_to_canonical_name(xpath)
        versions_present = sorted(set(e.version for e in elems),
                                  key=_version_sort_key)
        xpaths_by_version = {e.version: e.xpath for e in elems}

        # Pick best documentation
        doc = ""
        for e in reversed(elems_sorted):
            if e.documentation:
                doc = e.documentation
                break

        human_label = self._make_human_label(latest.element_name, xpath)
        data_type = TYPE_DESCRIPTIONS.get(latest.xsd_type, latest.xsd_type)

        return CanonicalField(
            canonical_name=canonical_name,
            schedule=latest.schedule,
            human_label=human_label,
            human_description=doc if doc else self._infer_description(xpath, latest),
            data_type=data_type,
            raw_xsd_type=latest.xsd_type,
            xpaths_by_version=xpaths_by_version,
            parent_group=latest.parent_group,
            is_repeating=latest.is_repeating,
            versions_present=versions_present,
        )

    def _fuzzy_match_orphans(self, seen_xpaths: set):
        """For xpaths that appear in only some versions, try to find their
        counterpart by:
        1. Matching on leaf element name + schedule (handles simple cases)
        2. Matching on description + parent group + schedule (handles IRS
           abbreviation renames like BaseCompensationFilingOrgAmt →
           BsCmpnstnFlngOrgAmt where the description stayed the same)
        """
        # Build index of canonical fields by (schedule, leaf_name)
        by_leaf = defaultdict(list)
        for cf in self.canonical_fields:
            leaf = cf.canonical_name.split("_")[-1] if "_" in cf.canonical_name else cf.canonical_name
            by_leaf[(cf.schedule, leaf)].append(cf)

        # Build index by (schedule, parent_group, description) for
        # description-based matching
        by_desc = defaultdict(list)
        for cf in self.canonical_fields:
            if cf.human_description:
                key = (cf.schedule, cf.parent_group,
                       cf.human_description.strip().lower())
                by_desc[key].append(cf)

        # Look for unmatched xpaths
        all_xpaths = set(self.elements_by_xpath.keys())
        orphan_xpaths = all_xpaths - seen_xpaths

        for xpath in sorted(orphan_xpaths):
            elems = self.elements_by_xpath[xpath]
            if not elems:
                continue
            latest = sorted(elems, key=lambda e: _version_sort_key(e.version))[-1]
            leaf = latest.element_name
            matched = False

            # Strategy 1: exact leaf match within same schedule
            candidates = by_leaf.get((latest.schedule, leaf), [])
            if len(candidates) == 1:
                cf = candidates[0]
                for e in elems:
                    if e.version not in cf.xpaths_by_version:
                        cf.xpaths_by_version[e.version] = e.xpath
                        if e.version not in cf.versions_present:
                            cf.versions_present.append(e.version)
                cf.versions_present.sort(key=_version_sort_key)
                seen_xpaths.add(xpath)
                matched = True

            # Strategy 2: description match within same group + schedule
            if not matched and latest.documentation:
                desc_key = (latest.schedule, latest.parent_group,
                            latest.documentation.strip().lower())
                desc_candidates = by_desc.get(desc_key, [])
                if len(desc_candidates) == 1:
                    cf = desc_candidates[0]
                    # Merge: add this version's xpath to existing field
                    for e in elems:
                        if e.version not in cf.xpaths_by_version:
                            cf.xpaths_by_version[e.version] = e.xpath
                            if e.version not in cf.versions_present:
                                cf.versions_present.append(e.version)
                    cf.versions_present.sort(key=_version_sort_key)
                    seen_xpaths.add(xpath)
                    matched = True

            if not matched:
                # No match — create new canonical field
                cf = self._make_canonical(xpath, elems)
                self.canonical_fields.append(cf)
                by_leaf_key = cf.canonical_name.split("_")[-1] if "_" in cf.canonical_name else cf.canonical_name
                by_leaf[(cf.schedule, by_leaf_key)].append(cf)
                if cf.human_description:
                    desc_k = (cf.schedule, cf.parent_group,
                              cf.human_description.strip().lower())
                    by_desc[desc_k].append(cf)
                seen_xpaths.add(xpath)

    def _merge_by_description(self):
        """Post-processing: merge canonical fields that appear in
        non-overlapping version sets but have identical descriptions within
        the same schedule and parent group. This catches IRS abbreviation
        renames (e.g., BaseCompensationFilingOrgAmt → BsCmpnstnFlngOrgAmt)
        where the field meaning didn't change, only the element name did."""
        # Group fields by (schedule, parent_group, normalized description)
        by_desc = defaultdict(list)
        for cf in self.canonical_fields:
            if cf.human_description:
                key = (cf.schedule, cf.parent_group,
                       cf.human_description.strip().lower())
                by_desc[key].append(cf)

        merged_away = set()  # ids of fields absorbed into others
        merge_count = 0

        for key, group in by_desc.items():
            if len(group) < 2:
                continue

            # Check if versions are non-overlapping (indicating a rename)
            # Pick the one with the latest version as the "primary"
            group_sorted = sorted(
                group,
                key=lambda f: _version_sort_key(f.versions_present[-1])
                    if f.versions_present else (0, 0, 0)
            )
            primary = group_sorted[-1]

            for other in group_sorted[:-1]:
                # Check for version overlap
                overlap = set(primary.versions_present) & set(other.versions_present)
                if not overlap:
                    # No overlap — these are the same field renamed. Merge.
                    for v, xp in other.xpaths_by_version.items():
                        if v not in primary.xpaths_by_version:
                            primary.xpaths_by_version[v] = xp
                        if v not in primary.versions_present:
                            primary.versions_present.append(v)
                    primary.versions_present.sort(key=_version_sort_key)
                    merged_away.add(id(other))
                    merge_count += 1

        if merge_count > 0:
            self.canonical_fields = [
                cf for cf in self.canonical_fields
                if id(cf) not in merged_away
            ]
            print(f"  → Merged {merge_count} renamed fields by matching descriptions")

    @staticmethod
    def _xpath_to_canonical_name(xpath: str) -> str:
        """Convert an xpath like /IRS990ScheduleJ/CompInfoGrp/TotalAmt
        to a canonical variable name like ScheduleJ_CompInfoGrp_TotalAmt."""
        parts = xpath.strip("/").split("/")
        if not parts:
            return xpath
        # Drop the top-level form name since it's in the schedule field
        if len(parts) > 1:
            parts = parts[1:]
        # Abbreviate for readability while keeping uniqueness
        return "_".join(parts)

    @staticmethod
    def _make_human_label(element_name: str, xpath: str) -> str:
        """Generate a human-readable label from the element name.
        Converts CamelCase/abbreviated names to spaced words."""
        # Insert spaces before capitals
        label = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', element_name)
        # Expand common abbreviations
        label = label.replace("Amt", "Amount")
        label = label.replace("Ind", "Indicator")
        label = label.replace("Cnt", "Count")
        label = label.replace("Dt", "Date")
        label = label.replace("Txt", "Text")
        label = label.replace("Nm", "Name")
        label = label.replace("Cd", "Code")
        label = label.replace("Grp", "Group")
        label = label.replace("Prsn", "Person")
        label = label.replace("Org", "Organization")
        label = label.replace("Cmpnstn", "Compensation")
        label = label.replace("Rltd", "Related")
        label = label.replace("Offcr", "Officer")
        label = label.replace("Trst", "Trust")
        label = label.replace("Empl", "Employee")
        label = label.replace("Fndrsng", "Fundraising")
        label = label.replace("Rvn", "Revenue")
        label = label.replace("Expns", "Expense")
        label = label.replace("Bsnss", "Business")
        label = label.replace("Strt", "Street")
        label = label.replace("Addrss", "Address")
        label = label.replace("Ttl", "Total")
        label = label.replace("Prtcl", "Protocol")
        label = label.replace("Dfrrd", "Deferred")
        label = label.replace("Nntxbl", "Nontaxable")
        label = label.replace("Bnfts", "Benefits")
        label = label.replace("Flng", "Filing")
        label = label.replace("Orgnztn", "Organization")
        label = label.replace("Cntrbtn", "Contribution")
        label = label.replace("Prgrm", "Program")
        label = label.replace("Srvcs", "Services")
        label = label.replace("Gvrnng", "Governing")
        label = label.replace("Mmbr", "Member")
        label = label.replace("Schdl", "Schedule")
        label = label.replace("Intrstd", "Interested")
        label = label.replace("Prty", "Party")
        label = label.replace("Trnsctn", "Transaction")
        return label.strip()

    @staticmethod
    def _infer_description(xpath: str, elem: SchemaElement) -> str:
        """Generate a description when none is provided in the schema."""
        parts = xpath.strip("/").split("/")
        if len(parts) >= 2:
            context = parts[-2] if len(parts) >= 2 else ""
            leaf = parts[-1]
            context_clean = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', context)
            leaf_clean = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', leaf)
            return f"{leaf_clean} (within {context_clean})"
        return elem.element_name


# ---------------------------------------------------------------------------
# Output generation
# ---------------------------------------------------------------------------

class OutputWriter:
    """Writes the concordance in both machine-readable and human-readable
    formats."""

    def __init__(self, canonical_fields: list[CanonicalField],
                 versions: list[str], output_dir: str):
        self.fields = canonical_fields
        self.versions = sorted(versions, key=_version_sort_key)
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def write_all(self):
        """Generate all output files."""
        self.write_machine_csv()
        self.write_human_reference()
        self.write_field_lookup_json()
        print(f"\nOutput written to: {self.output_dir}/")
        print(f"  concordance.csv          — Machine-readable concordance ({len(self.fields)} fields)")
        print(f"  field_reference.md        — Human-readable field reference")
        print(f"  field_lookup.json         — JSON lookup table for code integration")

    def write_machine_csv(self):
        """Write the master concordance CSV.

        Columns:
          canonical_name    - Stable identifier for this field
          schedule          - Which form/schedule (IRS990, IRS990ScheduleJ, etc.)
          xpath             - The xpath in the most recent schema version
          human_label       - Plain-English short name
          data_type         - Friendly type description
          raw_xsd_type      - Original XSD type string
          parent_group      - Repeating group name, if any
          is_repeating      - Whether field is inside a repeating group
          description       - Explanation of what this field contains
          version_start     - Earliest schema version where this field appears
          version_end       - Latest schema version (blank = still current)
          versions          - Semicolon-separated list of all versions present
          [one column per version with the xpath in that version, or blank]
        """
        path = os.path.join(self.output_dir, "concordance.csv")

        # Column headers
        base_cols = [
            "canonical_name", "schedule", "xpath", "human_label",
            "data_type", "raw_xsd_type", "parent_group", "is_repeating",
            "description", "version_start", "version_end", "versions",
        ]
        # Add per-version xpath columns
        version_cols = [f"xpath_{v}" for v in self.versions]
        headers = base_cols + version_cols

        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            writer.writeheader()

            for cf in self.fields:
                # Determine the "current" xpath (from latest version present)
                sorted_versions = sorted(cf.versions_present, key=_version_sort_key)
                current_xpath = cf.xpaths_by_version.get(
                    sorted_versions[-1], "") if sorted_versions else ""

                row = {
                    "canonical_name": cf.canonical_name,
                    "schedule": cf.schedule,
                    "xpath": current_xpath,
                    "human_label": cf.human_label,
                    "data_type": cf.data_type,
                    "raw_xsd_type": cf.raw_xsd_type,
                    "parent_group": cf.parent_group,
                    "is_repeating": cf.is_repeating,
                    "description": cf.human_description,
                    "version_start": sorted_versions[0] if sorted_versions else "",
                    "version_end": sorted_versions[-1] if sorted_versions else "",
                    "versions": ";".join(sorted_versions),
                }
                # Per-version xpaths
                for v in self.versions:
                    row[f"xpath_{v}"] = cf.xpaths_by_version.get(v, "")

                writer.writerow(row)

    def write_human_reference(self):
        """Write a Markdown file that explains each field in plain language,
        organized by schedule."""
        path = os.path.join(self.output_dir, "field_reference.md")

        # Group fields by schedule
        by_schedule = defaultdict(list)
        for cf in self.fields:
            by_schedule[cf.schedule].append(cf)

        with open(path, "w", encoding="utf-8") as f:
            f.write("# IRS Form 990 Field Reference\n\n")
            f.write("This document describes every field extracted from the IRS 990 "
                    "XML schemas, organized by form and schedule. Each entry shows:\n\n")
            f.write("- **What it is** (human-readable label and description)\n")
            f.write("- **Data type** (what kind of value to expect)\n")
            f.write("- **Where to find it** (xpath in the XML)\n")
            f.write("- **Which versions** include this field\n")
            f.write("- **Repeating group**, if the field appears once per entity "
                    "(e.g., once per officer, once per grant)\n\n")
            f.write("---\n\n")

            for schedule in sorted(by_schedule.keys()):
                fields_list = by_schedule[schedule]
                title = KNOWN_FORMS.get(schedule, schedule)
                f.write(f"## {title}\n\n")

                # Sub-group by parent_group
                ungrouped = [cf for cf in fields_list if not cf.parent_group]
                grouped = defaultdict(list)
                for cf in fields_list:
                    if cf.parent_group:
                        grouped[cf.parent_group].append(cf)

                if ungrouped:
                    f.write("### Non-Repeating Fields\n\n")
                    for cf in ungrouped:
                        self._write_field_entry(f, cf)

                for group_name in sorted(grouped.keys()):
                    f.write(f"### Repeating Group: `{group_name}`\n\n")
                    f.write(f"*These fields repeat for each entry in the "
                            f"`{group_name}` group (e.g., one set per officer, "
                            f"per grant recipient, per related organization).*\n\n")
                    for cf in grouped[group_name]:
                        self._write_field_entry(f, cf)

                f.write("---\n\n")

    def _write_field_entry(self, f, cf: CanonicalField):
        """Write one field entry in the human reference."""
        f.write(f"#### {cf.human_label}\n\n")
        f.write(f"| Property | Value |\n")
        f.write(f"|----------|-------|\n")
        f.write(f"| **Canonical Name** | `{cf.canonical_name}` |\n")
        f.write(f"| **Data Type** | {cf.data_type} |\n")
        if cf.human_description:
            f.write(f"| **Description** | {cf.human_description} |\n")
        sorted_versions = sorted(cf.versions_present, key=_version_sort_key)
        if sorted_versions:
            f.write(f"| **First Version** | {sorted_versions[0]} |\n")
            f.write(f"| **Latest Version** | {sorted_versions[-1]} |\n")
            f.write(f"| **All Versions** | {', '.join(sorted_versions)} |\n")
        if cf.parent_group:
            f.write(f"| **Repeating Group** | `{cf.parent_group}` |\n")

        # Show xpath, preferring the most recent
        latest_xpath = ""
        for v in reversed(sorted_versions):
            if v in cf.xpaths_by_version:
                latest_xpath = cf.xpaths_by_version[v]
                break
        if latest_xpath:
            f.write(f"| **XPath** | `{latest_xpath}` |\n")

        # If xpath changed across versions, note that
        unique_xpaths = set(cf.xpaths_by_version.values())
        if len(unique_xpaths) > 1:
            f.write(f"| **Note** | XPath changed across versions — see concordance.csv for per-version xpaths |\n")

        f.write("\n")

    def write_field_lookup_json(self):
        """Write a JSON file optimized for programmatic lookup.

        Structure:
        {
            "metadata": { "versions": [...], "generated_by": "..." },
            "fields": {
                "canonical_name": {
                    "schedule": "...",
                    "label": "...",
                    "type": "...",
                    "description": "...",
                    "group": "...",
                    "repeating": true/false,
                    "xpaths": { "2013v3.0": "/...", "2016v3.0": "/..." }
                },
                ...
            },
            "xpath_index": {
                "/IRS990/TotalRevenueAmt": "canonical_name",
                ...
            }
        }
        """
        import json

        path = os.path.join(self.output_dir, "field_lookup.json")

        fields_dict = {}
        xpath_index = {}

        for cf in self.fields:
            fields_dict[cf.canonical_name] = {
                "schedule": cf.schedule,
                "label": cf.human_label,
                "type": cf.data_type,
                "raw_type": cf.raw_xsd_type,
                "description": cf.human_description,
                "group": cf.parent_group,
                "repeating": cf.is_repeating,
                "xpaths": cf.xpaths_by_version,
                "version_start": cf.versions_present[0] if cf.versions_present else "",
                "version_end": cf.versions_present[-1] if cf.versions_present else "",
            }
            # Reverse index: xpath -> canonical name
            for v, xp in cf.xpaths_by_version.items():
                xpath_index[xp] = cf.canonical_name

        output = {
            "metadata": {
                "versions": self.versions,
                "total_fields": len(self.fields),
                "generated_by": "concordance_builder.py",
                "description": (
                    "IRS 990 Schema Concordance. 'fields' maps canonical names "
                    "to field metadata. 'xpath_index' maps any xpath from any "
                    "version to its canonical name for quick lookup."
                ),
            },
            "fields": fields_dict,
            "xpath_index": xpath_index,
        }

        with open(path, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _version_sort_key(version: str) -> tuple:
    """Sort version strings like '2013v3.0' chronologically."""
    if isinstance(version, SchemaElement):
        version = version.version
    match = re.match(r"(\d{4})v?(\d+)?\.?(\d+)?", version)
    if match:
        return (int(match.group(1)),
                int(match.group(2) or 0),
                int(match.group(3) or 0))
    return (0, 0, 0)


def _has_top_level_element(xsd_path: str) -> bool:
    """Check if an .xsd file defines at least one top-level xs:element.
    Files that only define types/groups (like efileTypes.xsd) return False."""
    try:
        # Use iterparse for speed — stop as soon as we find one
        for event, elem in etree.iterparse(xsd_path, events=("end",)):
            if not isinstance(elem.tag, str):
                continue
            local = etree.QName(elem.tag).localname
            if local == "element" and elem.get("name"):
                # Check it's a direct child of xs:schema (top-level)
                parent = elem.getparent()
                if parent is not None and isinstance(parent.tag, str):
                    parent_local = etree.QName(parent.tag).localname
                    if parent_local == "schema":
                        return True
        return False
    except (etree.XMLSyntaxError, OSError):
        return False


def discover_schemas(schema_dir: str) -> dict[str, list[str]]:
    """Discover schema files organized by version.

    Supports the real IRS nested structure:
        schema_dir/
            2020v4.2/
                Common/
                    efileTypes.xsd
                TEGE/
                    Common/
                        IRS990ScheduleA/
                            IRS990ScheduleA.xsd
                        ...
                    TEGE990/
                        IRS990/
                            IRS990.xsd

    Also supports flat structure:
        schema_dir/
            2013v3.0/
                IRS990.xsd
                IRS990ScheduleA.xsd
                efileTypes.xsd

    Returns: {version_string: [list of .xsd file paths]}
    """
    versions = {}
    schema_path = Path(schema_dir)

    if not schema_path.exists():
        print(f"ERROR: Schema directory not found: {schema_dir}", file=sys.stderr)
        sys.exit(1)

    for version_dir in sorted(schema_path.iterdir()):
        if not version_dir.is_dir():
            continue
        version_name = version_dir.name
        # Recursive glob to find .xsd files in any subdirectory
        xsd_files = sorted(str(f) for f in version_dir.rglob("*.xsd"))
        if xsd_files:
            versions[version_name] = xsd_files

    if not versions:
        # Maybe schemas are flat (all .xsd in one dir with version in filename)
        xsd_files = sorted(str(f) for f in schema_path.rglob("*.xsd"))
        if xsd_files:
            versions["unknown"] = xsd_files

    return versions


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Build IRS 990 concordance from XSD schema files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --schema-dir ./schemas --output-dir ./output
  %(prog)s --schema-dir ./schemas/2016v3.0 --output-dir ./output --single-version 2016v3.0

Schema directory should contain one subdirectory per version:
  schemas/2013v3.0/IRS990.xsd, schemas/2014v5.0/IRS990.xsd, etc.

Download schemas from:
  https://www.irs.gov/e-file-providers/current-valid-xml-schemas-and-business-rules-for-exempt-organizations-and-other-tax-exempt-entities-modernized-e-file
        """
    )
    parser.add_argument("--schema-dir", required=True,
                        help="Directory containing schema version folders")
    parser.add_argument("--output-dir", default="./concordance_output",
                        help="Directory for output files (default: ./concordance_output)")
    parser.add_argument("--single-version", default=None,
                        help="Process only one version (version folder name)")
    parser.add_argument("--verbose", action="store_true",
                        help="Show detailed progress")

    args = parser.parse_args()

    print("=" * 60)
    print("IRS 990 Schema Concordance Builder")
    print("=" * 60)

    # Discover schemas
    all_versions = discover_schemas(args.schema_dir)
    if args.single_version:
        if args.single_version in all_versions:
            all_versions = {args.single_version: all_versions[args.single_version]}
        else:
            print(f"ERROR: Version '{args.single_version}' not found. "
                  f"Available: {list(all_versions.keys())}", file=sys.stderr)
            sys.exit(1)

    print(f"\nFound {len(all_versions)} schema version(s):")
    for v in sorted(all_versions.keys(), key=_version_sort_key):
        print(f"  {v}: {len(all_versions[v])} .xsd files")

    # Parse all schemas
    builder = ConcordanceBuilder()
    total_elements = 0

    for version in sorted(all_versions.keys(), key=_version_sort_key):
        xsd_files = all_versions[version]
        print(f"\nParsing {version}...")

        # First pass: collect ALL complex types AND groups from ALL .xsd files
        # in this version tree. Real IRS schemas split types across deeply
        # nested subdirectories (Common/efileTypes.xsd, TEGE/Common/IRS990ScheduleA/, etc.)
        # collect_from_directory now uses rglob to find all .xsd files recursively.
        # Compute the version root directory for type collection
        version_root = Path(args.schema_dir) / version
        if not version_root.is_dir():
            # Fallback: flat layout, version is "unknown", schemas in schema_dir itself
            version_root = Path(args.schema_dir)
        version_dir = str(version_root)
        type_collector = GlobalTypeCollector()
        type_collector.collect_from_directory(version_dir)
        global_types = type_collector.complex_types
        global_groups = type_collector.groups
        if args.verbose:
            print(f"  Collected {len(global_types)} complex types, "
                  f"{len(global_groups)} groups from {version_dir}")

        # Second pass: parse each .xsd that defines a top-level form element.
        # Instead of hardcoding filename prefixes, we detect which files contain
        # a top-level xs:element (i.e., they define a form/schedule root).
        # Files that only contain xs:complexType/xs:simpleType/xs:group
        # definitions (type libraries like efileTypes.xsd) are skipped.
        version_elements = []
        for xsd_path in xsd_files:
            stem = Path(xsd_path).stem

            # Quick check: does this file have a top-level xs:element?
            if not _has_top_level_element(xsd_path):
                if args.verbose:
                    print(f"  {Path(xsd_path).name}: skipped (type definitions only)")
                continue

            parser_obj = SchemaParser(xsd_path, version,
                                      global_types, global_groups)
            elements = parser_obj.parse()
            version_elements.extend(elements)
            if args.verbose and elements:
                print(f"  {Path(xsd_path).name}: {len(elements)} elements")
            elif args.verbose:
                print(f"  {Path(xsd_path).name}: 0 elements")

        builder.add_version(version, version_elements)
        total_elements += len(version_elements)
        print(f"  → {len(version_elements)} elements extracted")

    print(f"\nTotal elements across all versions: {total_elements}")

    # Build concordance
    print("\nBuilding cross-version concordance...")
    builder.build()
    print(f"  → {len(builder.canonical_fields)} canonical fields identified")

    # Count repeating vs non-repeating
    repeating = sum(1 for f in builder.canonical_fields if f.is_repeating)
    print(f"  → {repeating} in repeating groups, "
          f"{len(builder.canonical_fields) - repeating} non-repeating")

    # Write outputs
    print("\nWriting output files...")
    writer = OutputWriter(builder.canonical_fields, builder.versions,
                          args.output_dir)
    writer.write_all()

    print("\n" + "=" * 60)
    print("Done!")
    print("=" * 60)
    print(f"\nTo use in your code:")
    print(f"  import json")
    print(f"  with open('{args.output_dir}/field_lookup.json') as f:")
    print(f"      concordance = json.load(f)")
    print(f"  # Look up any xpath:")
    print(f"  canonical = concordance['xpath_index']['/IRS990/TotalRevenueAmt']")
    print(f"  info = concordance['fields'][canonical]")
    print(f"  print(info['label'], info['type'], info['description'])")


if __name__ == "__main__":
    main()
