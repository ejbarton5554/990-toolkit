#!/usr/bin/env python3
from __future__ import annotations
"""
IRS 990 Concordance Auditor
============================
Validates a concordance (field_lookup.json) against real 990 XML filings.

Flags:
  1. UNKNOWN XPATHS   — Elements in the XML that have no concordance entry
  2. MISSING VERSIONS — Filing uses a schema version not in the concordance
  3. PHANTOM FIELDS   — Concordance says a field exists for a version, but
                        the real XML for that version never contains it
  4. XPATH MISMATCHES — Concordance xpath doesn't match the actual XML structure

Outputs:
  - audit_report.json     — Machine-readable audit results
  - audit_report.md       — Human-readable summary
  - unknown_xpaths.csv    — All xpaths found in filings but missing from concordance

Usage:
  # Audit concordance against a directory of real 990 XML filings
  python concordance_auditor.py \\
      --concordance ./concordance_output/field_lookup.json \\
      --xml-dir ./990_xmls \\
      --output-dir ./audit_output

  # Audit against a single filing
  python concordance_auditor.py \\
      --concordance ./concordance_output/field_lookup.json \\
      --xml-file ./filing.xml \\
      --output-dir ./audit_output

  # Also patch the concordance with discovered unknowns
  python concordance_auditor.py \\
      --concordance ./concordance_output/field_lookup.json \\
      --xml-dir ./990_xmls \\
      --output-dir ./audit_output \\
      --patch
"""

import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime

try:
    from lxml import etree
except ImportError:
    sys.exit("Missing dependency: pip install lxml")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class FilingInfo:
    """Metadata extracted from a single 990 XML filing."""
    filepath: str = ""
    object_id: str = ""
    ein: str = ""
    org_name: str = ""
    tax_period: str = ""
    return_version: str = ""
    form_type: str = ""          # IRS990, IRS990EZ, IRS990PF
    schedules_present: list = field(default_factory=list)
    total_elements: int = 0
    all_xpaths: list = field(default_factory=list)


@dataclass
class UnknownXpath:
    """An xpath found in a filing but not in the concordance."""
    xpath: str = ""
    element_name: str = ""
    schedule: str = ""
    parent_xpath: str = ""
    sample_value: str = ""       # First non-empty value seen
    value_count: int = 0         # How many filings had a non-empty value
    filing_count: int = 0        # How many filings contained this xpath
    versions_seen: set = field(default_factory=set)
    is_repeating: bool = False   # Appears multiple times in same filing
    data_type_guess: str = ""    # Inferred from values
    # Version classification
    version_covered: bool = False  # True if ALL versions seen are in concordance
    # Fuzzy match results
    fuzzy_match: str = ""          # Best matching concordance xpath, if any
    fuzzy_match_score: float = 0.0 # Match confidence 0-1
    fuzzy_match_reason: str = ""   # Why we think it's a match


@dataclass
class AuditResults:
    """Complete audit results."""
    # Counts
    filings_audited: int = 0
    total_elements_seen: int = 0
    unique_xpaths_seen: int = 0
    concordance_fields: int = 0

    # Coverage
    matched_xpaths: int = 0      # In both XML and concordance
    unknown_xpaths: int = 0      # In XML but not concordance
    unused_concordance: int = 0  # In concordance but never seen in XML
    coverage_pct: float = 0.0

    # Unknown classification
    unknowns_version_covered: int = 0  # Unknown AND version IS in concordance (real gap)
    unknowns_version_missing: int = 0  # Unknown AND version NOT in concordance (maybe rename)

    # Fuzzy matching
    fuzzy_matched: int = 0             # Unknowns with a probable concordance match
    fuzzy_matches: list = field(default_factory=list)  # List of (unknown_xpath, match_xpath, score, reason)

    # Version tracking
    versions_in_concordance: list = field(default_factory=list)
    versions_in_filings: list = field(default_factory=list)
    missing_versions: list = field(default_factory=list)

    # Detailed findings
    unknowns: dict = field(default_factory=dict)       # xpath -> UnknownXpath
    unused_fields: list = field(default_factory=list)   # canonical names never seen
    version_gaps: dict = field(default_factory=dict)    # version -> [missing xpaths]

    # Per-schedule breakdown
    schedule_coverage: dict = field(default_factory=dict)

    # Warnings
    warnings: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# XML filing parser
# ---------------------------------------------------------------------------

class FilingParser:
    """Parse a real 990 XML filing and extract all element xpaths."""

    # IRS efile namespace — we detect dynamically but this is the common one
    EFILE_NS = "http://www.irs.gov/efile"

    def parse(self, xml_path: str) -> FilingInfo:
        """Parse one filing and return its info + all xpaths."""
        info = FilingInfo(filepath=xml_path)

        try:
            tree = etree.parse(xml_path)
        except etree.XMLSyntaxError as e:
            info.all_xpaths = []
            return info

        root = tree.getroot()

        # Detect namespace
        ns = ""
        if root.tag.startswith("{"):
            ns = root.tag.split("}")[0] + "}"

        nsmap = {"efile": ns.strip("{}")} if ns else {}

        # Extract filing metadata from ReturnHeader
        info.return_version = root.get("returnVersion", "")
        header = root.find(f"{ns}ReturnHeader", nsmap) if ns else root.find("ReturnHeader")
        if header is not None:
            info = self._parse_header(header, ns, info)

        # Find which schedules/forms are present in ReturnData
        return_data = root.find(f"{ns}ReturnData") if ns else root.find("ReturnData")
        if return_data is None:
            # Some filings have forms directly under root
            return_data = root

        # Walk all elements and collect xpaths
        all_xpaths = []
        xpath_values = {}  # xpath -> first non-empty value

        for elem in return_data.iter():
            # Skip comments, processing instructions, etc.
            if not isinstance(elem.tag, str):
                continue
            # Build a clean xpath (strip namespace, strip Return/ReturnData wrapper)
            xpath = self._build_xpath(elem, ns, return_data)
            if xpath:
                all_xpaths.append(xpath)
                # Capture sample values for leaf elements
                if elem.text and elem.text.strip() and xpath not in xpath_values:
                    xpath_values[xpath] = elem.text.strip()[:100]

        # Detect schedules
        for child in return_data:
            if not isinstance(child.tag, str):
                continue
            tag = self._strip_ns(child.tag, ns)
            if tag.startswith("IRS990") or tag.startswith("IRS"):
                if tag not in info.schedules_present:
                    info.schedules_present.append(tag)
            # Detect form type
            if tag in ("IRS990", "IRS990EZ", "IRS990PF"):
                info.form_type = tag

        info.total_elements = len(all_xpaths)
        info.all_xpaths = list(set(all_xpaths))  # deduplicate

        # Attach sample values
        info._xpath_values = xpath_values

        return info

    def _parse_header(self, header, ns: str, info: FilingInfo) -> FilingInfo:
        """Extract metadata from ReturnHeader."""
        def find_text(parent, *names):
            for name in names:
                elem = parent.find(f"{ns}{name}")
                if elem is not None and elem.text:
                    return elem.text.strip()
            return ""

        info.tax_period = find_text(header, "TaxPeriodEndDt", "TaxPeriodEndDate",
                                     "TaxYr", "TaxYear")
        # Filer section
        filer = header.find(f"{ns}Filer")
        if filer is not None:
            info.ein = find_text(filer, "EIN")
            # Business name can be nested
            bname = filer.find(f"{ns}BusinessName")
            if bname is None:
                bname = filer.find(f"{ns}Name")
            if bname is not None:
                line1 = bname.find(f"{ns}BusinessNameLine1Txt")
                if line1 is None:
                    line1 = bname.find(f"{ns}BusinessNameLine1")
                if line1 is not None and line1.text:
                    info.org_name = line1.text.strip()

        return info

    def _build_xpath(self, elem, ns: str, return_data) -> str:
        """Build a concordance-style xpath from an element.
        Format: /IRS990/FieldName or /IRS990ScheduleJ/GroupName/FieldName
        Strips the Return/ReturnData wrapper and namespace."""
        parts = []
        current = elem
        while current is not None and current is not return_data:
            if not isinstance(current.tag, str):
                break
            tag = self._strip_ns(current.tag, ns)
            if tag in ("Return", "ReturnData", "ReturnHeader"):
                break
            parts.insert(0, tag)
            current = current.getparent()

        if not parts:
            return ""
        return "/" + "/".join(parts)

    @staticmethod
    def _strip_ns(tag: str, ns: str) -> str:
        """Remove namespace prefix from a tag."""
        if ns and tag.startswith(ns):
            return tag[len(ns):]
        if tag.startswith("{"):
            return tag.split("}", 1)[1]
        return tag


# ---------------------------------------------------------------------------
# Concordance auditor
# ---------------------------------------------------------------------------

class ConcordanceAuditor:
    """Compare concordance against real filings."""

    def __init__(self, concordance_path: str):
        with open(concordance_path, "r", encoding="utf-8") as f:
            self.concordance = json.load(f)
        self.fields = self.concordance.get("fields", {})
        self.xpath_index = self.concordance.get("xpath_index", {})
        self.metadata = self.concordance.get("metadata", {})

        # Build reverse indexes
        self.conc_versions = set()
        self.conc_schedules = set()
        self.all_conc_xpaths = set(self.xpath_index.keys())

        for name, info in self.fields.items():
            sched = info.get("schedule", "")
            if sched:
                self.conc_schedules.add(sched)
            for v in info.get("xpaths", {}).keys():
                self.conc_versions.add(v)

        self.results = AuditResults()
        self.results.concordance_fields = len(self.fields)
        self.results.versions_in_concordance = sorted(self.conc_versions)

        # Track what we've seen across all filings
        self._seen_xpaths: dict[str, dict] = {}  # xpath -> tracking info
        self._seen_conc_fields: set[str] = set()  # canonical names seen
        self._filing_versions: set[str] = set()

    def audit_filing(self, filing: FilingInfo):
        """Audit one filing against the concordance."""
        self.results.filings_audited += 1
        self.results.total_elements_seen += filing.total_elements
        version = filing.return_version

        if version:
            self._filing_versions.add(version)

        xpath_values = getattr(filing, '_xpath_values', {})

        # Track which xpaths from this filing are in/not in concordance
        for xpath in filing.all_xpaths:
            # Normalize: ensure leading slash
            if not xpath.startswith("/"):
                xpath = "/" + xpath

            if xpath in self._seen_xpaths:
                self._seen_xpaths[xpath]["filing_count"] += 1
                if version:
                    self._seen_xpaths[xpath]["versions"].add(version)
                if xpath in xpath_values and not self._seen_xpaths[xpath]["sample_value"]:
                    self._seen_xpaths[xpath]["sample_value"] = xpath_values.get(xpath, "")
            else:
                self._seen_xpaths[xpath] = {
                    "filing_count": 1,
                    "versions": {version} if version else set(),
                    "in_concordance": xpath in self.all_conc_xpaths,
                    "sample_value": xpath_values.get(xpath, ""),
                }

            # Track concordance field usage
            if xpath in self.xpath_index:
                canonical = self.xpath_index[xpath]
                self._seen_conc_fields.add(canonical)

    def finalize(self) -> AuditResults:
        """Compute final audit results after all filings processed."""
        r = self.results
        r.versions_in_filings = sorted(self._filing_versions)
        r.missing_versions = sorted(
            self._filing_versions - self.conc_versions)
        r.unique_xpaths_seen = len(self._seen_xpaths)

        # Classify xpaths
        matched = 0
        # Build prefix index for detecting group containers
        # An xpath is a "covered container" if it has no value itself
        # but concordance xpaths exist that start with it as a prefix
        conc_xpath_set = self.all_conc_xpaths
        covered_containers = 0

        for xpath, info in self._seen_xpaths.items():
            if info["in_concordance"]:
                matched += 1
            else:
                # Unknown xpath — not in concordance
                parts = xpath.strip("/").split("/")
                schedule = parts[0] if parts else ""
                element_name = parts[-1] if parts else ""
                parent_xpath = "/" + "/".join(parts[:-1]) if len(parts) > 1 else ""

                # Skip form-level container elements (e.g., /IRS990,
                # /IRS990ScheduleJ) — these are structural, not data
                if len(parts) == 1 and (
                    parts[0].startswith("IRS990") or
                    parts[0].startswith("IRS") or
                    parts[0] == "ReturnHeader"
                ):
                    continue

                # Check if this is a group container whose children
                # are in the concordance (e.g., /IRS990/AccountsPayableAccrExpnssGrp
                # where /IRS990/AccountsPayableAccrExpnssGrp/BOYAmt exists)
                is_container = (
                    not info["sample_value"] and
                    any(cx.startswith(xpath + "/") for cx in conc_xpath_set)
                )
                if is_container:
                    covered_containers += 1
                    continue

                unk = UnknownXpath(
                    xpath=xpath,
                    element_name=element_name,
                    schedule=schedule,
                    parent_xpath=parent_xpath,
                    sample_value=info["sample_value"],
                    filing_count=info["filing_count"],
                    versions_seen=info["versions"],
                    data_type_guess=self._guess_type(info["sample_value"]),
                )
                # Check if it appears multiple times per filing (repeating)
                unk.is_repeating = False  # would need per-filing tracking
                r.unknowns[xpath] = unk

        r.matched_xpaths = matched
        r.unknown_xpaths = len(r.unknowns)

        # Classify unknowns: version covered vs version missing
        missing_versions_set = set(r.missing_versions) if r.missing_versions else set()
        # (missing_versions not computed yet — compute now)
        missing_versions_set = self._filing_versions - self.conc_versions
        for xpath, unk in r.unknowns.items():
            # version_covered = True means ALL versions this xpath was seen in
            # are covered by the concordance. This is a REAL concordance gap.
            # version_covered = False means at least one version is missing,
            # so this MIGHT be a version-specific rename we can't verify.
            if unk.versions_seen:
                uncovered = unk.versions_seen & missing_versions_set
                unk.version_covered = len(uncovered) == 0
            else:
                unk.version_covered = True  # no version info — assume covered

        r.unknowns_version_covered = sum(
            1 for u in r.unknowns.values() if u.version_covered)
        r.unknowns_version_missing = sum(
            1 for u in r.unknowns.values() if not u.version_covered)

        # Find concordance fields never seen in any filing
        all_conc_names = set(self.fields.keys())
        r.unused_fields = sorted(all_conc_names - self._seen_conc_fields)
        r.unused_concordance = len(r.unused_fields)

        # Run fuzzy matching on unknowns
        self._run_fuzzy_matching(r)

        # Coverage percentage
        total = matched + r.unknown_xpaths
        r.coverage_pct = (matched / total * 100) if total > 0 else 0.0

        # Per-schedule breakdown
        sched_seen = defaultdict(lambda: {"matched": 0, "unknown": 0, "total": 0})
        for xpath, info in self._seen_xpaths.items():
            parts = xpath.strip("/").split("/")
            sched = parts[0] if parts else "(root)"
            sched_seen[sched]["total"] += 1
            if info["in_concordance"]:
                sched_seen[sched]["matched"] += 1
            else:
                sched_seen[sched]["unknown"] += 1

        for sched, counts in sorted(sched_seen.items()):
            total = counts["total"]
            pct = (counts["matched"] / total * 100) if total > 0 else 0.0
            r.schedule_coverage[sched] = {
                "total_xpaths": total,
                "matched": counts["matched"],
                "unknown": counts["unknown"],
                "coverage_pct": round(pct, 1),
            }

        # Version gap analysis
        for version in r.missing_versions:
            r.warnings.append(
                f"MISSING VERSION: Filings use schema version '{version}' "
                f"which is not in the concordance. Fields from these filings "
                f"may be misidentified or missed entirely."
            )

        # Flag high-frequency unknowns (likely real fields we should add)
        high_freq = [
            (xpath, unk) for xpath, unk in r.unknowns.items()
            if unk.filing_count >= max(2, r.filings_audited * 0.1)
        ]
        if high_freq:
            covered = sum(1 for _, u in high_freq if u.version_covered)
            missing = len(high_freq) - covered
            r.warnings.append(
                f"HIGH-FREQUENCY UNKNOWNS: {len(high_freq)} unknown xpaths "
                f"appear in 10%+ of filings ({covered} in covered versions = "
                f"real concordance gaps, {missing} in missing versions = "
                f"possible renames)."
            )

        # Report fuzzy match results
        if r.fuzzy_matched > 0:
            high_conf = sum(1 for u in r.unknowns.values()
                            if u.fuzzy_match_score >= 0.8)
            med_conf = sum(1 for u in r.unknowns.values()
                           if 0.6 <= u.fuzzy_match_score < 0.8)
            low_conf = sum(1 for u in r.unknowns.values()
                           if u.fuzzy_match and u.fuzzy_match_score < 0.6)
            r.warnings.append(
                f"FUZZY MATCHES: {r.fuzzy_matched} unknown xpaths have "
                f"probable concordance matches ({high_conf} high confidence, "
                f"{med_conf} medium, {low_conf} low). Review in the "
                f"fuzzy_matches section of the report."
            )

        # Flag schedules with low coverage
        for sched, cov in r.schedule_coverage.items():
            if cov["coverage_pct"] < 50 and cov["total_xpaths"] > 5:
                # Check if the concordance has entries for this schedule
                conc_has_schedule = any(
                    info.get("schedule", "") == sched
                    for info in self.fields.values()
                )
                if conc_has_schedule and cov["matched"] == 0:
                    r.warnings.append(
                        f"XPATH MISMATCH: {sched} has concordance entries but "
                        f"ZERO matched real xpaths. The concordance may have "
                        f"wrong xpath structures for this schedule."
                    )
                elif not conc_has_schedule:
                    r.warnings.append(
                        f"MISSING SCHEDULE: {sched} appears in filings but has "
                        f"no concordance entries at all. Run the concordance "
                        f"builder with schemas that include this schedule."
                    )
                else:
                    r.warnings.append(
                        f"LOW COVERAGE: {sched} has only {cov['coverage_pct']}% "
                        f"concordance coverage ({cov['matched']}/{cov['total_xpaths']} "
                        f"xpaths matched)."
                    )

        return r

    @staticmethod
    def _guess_type(sample_value: str) -> str:
        """Guess a data type from a sample value."""
        if not sample_value:
            return "unknown"
        v = sample_value.strip()
        if v.lower() in ("true", "false", "0", "1", "x"):
            return "boolean"
        if re.match(r"^-?\d+$", v):
            return "integer"
        if re.match(r"^-?\d+\.\d+$", v):
            return "decimal"
        if re.match(r"^\d{4}-\d{2}-\d{2}", v):
            return "date"
        if re.match(r"^\d{2}-\d{7}$", v):
            return "EIN"
        if len(v) > 50:
            return "text_long"
        return "text"

    # ------------------------------------------------------------------
    # Fuzzy matching engine
    # ------------------------------------------------------------------

    def _run_fuzzy_matching(self, results: AuditResults):
        """For each unknown xpath, try to find a probable match in the
        concordance using multiple strategies. Updates each UnknownXpath
        in-place with match info."""

        # Build indexes for matching
        conc_by_leaf: dict[str, list[str]] = defaultdict(list)  # leaf_name -> [xpaths]
        conc_by_schedule_leaf: dict[str, list[str]] = defaultdict(list)
        conc_leaf_to_xpath: dict[str, str] = {}
        conc_descriptions: dict[str, str] = {}  # xpath -> description

        for xpath in self.all_conc_xpaths:
            parts = xpath.strip("/").split("/")
            leaf = parts[-1] if parts else ""
            sched = parts[0] if parts else ""
            if leaf:
                conc_by_leaf[leaf].append(xpath)
                key = f"{sched}::{leaf}"
                conc_by_schedule_leaf[key].append(xpath)
                conc_leaf_to_xpath[leaf] = xpath

        # Also index by canonical field info
        for name, info in self.fields.items():
            xpaths = info.get("xpaths", {})
            desc = info.get("description", "")
            for v, xp in xpaths.items():
                conc_descriptions[xp] = desc

        # Get all concordance leaf names for fuzzy comparison
        all_conc_leaves = set(conc_by_leaf.keys())

        for xpath, unk in results.unknowns.items():
            best_match = ""
            best_score = 0.0
            best_reason = ""

            # Strategy 1: Exact leaf name match in same schedule
            # e.g., /IRS990/FooAmt is unknown but /IRS990EZ/FooAmt exists
            same_sched_key = f"{unk.schedule}::{unk.element_name}"
            if unk.element_name in conc_by_leaf:
                candidates = conc_by_leaf[unk.element_name]
                # Prefer same schedule, fall back to any
                same_sched = [c for c in candidates
                              if c.strip("/").split("/")[0] == unk.schedule]
                if same_sched:
                    # Same leaf, same schedule but different parent path
                    best_match = same_sched[0]
                    best_score = 0.9
                    best_reason = "exact leaf name, same schedule, different path"
                else:
                    best_match = candidates[0]
                    best_score = 0.7
                    best_reason = f"exact leaf name in different schedule"

            # Strategy 2: IRS abbreviation pattern matching
            # IRS abbreviates: Organization->Org, Compensation->Cmpnstn,
            # Amount->Amt, Group->Grp, etc.
            if best_score < 0.8:
                expanded = self._expand_irs_abbreviations(unk.element_name)
                for conc_leaf in all_conc_leaves:
                    conc_expanded = self._expand_irs_abbreviations(conc_leaf)
                    if expanded and conc_expanded and expanded == conc_expanded:
                        cand = conc_by_leaf[conc_leaf][0]
                        # Higher score if same schedule
                        if cand.strip("/").split("/")[0] == unk.schedule:
                            score = 0.85
                            reason = "IRS abbreviation match, same schedule"
                        else:
                            score = 0.65
                            reason = "IRS abbreviation match, different schedule"
                        if score > best_score:
                            best_match = cand
                            best_score = score
                            best_reason = reason

            # Strategy 3: Suffix/type pattern matching
            # e.g., TotalRevenueAmt matches CYTotalRevenueAmt
            if best_score < 0.7:
                for conc_leaf in all_conc_leaves:
                    score, reason = self._suffix_match(
                        unk.element_name, conc_leaf, unk.schedule,
                        conc_by_leaf[conc_leaf])
                    if score > best_score:
                        best_match = conc_by_leaf[conc_leaf][0]
                        best_score = score
                        best_reason = reason

            # Strategy 4: Levenshtein-like similarity on leaf names
            # (only for same schedule to avoid false positives)
            if best_score < 0.6:
                for conc_leaf in all_conc_leaves:
                    for cand_xpath in conc_by_leaf[conc_leaf]:
                        cand_sched = cand_xpath.strip("/").split("/")[0]
                        if cand_sched != unk.schedule:
                            continue
                        sim = self._name_similarity(unk.element_name, conc_leaf)
                        if sim > best_score and sim >= 0.6:
                            best_match = cand_xpath
                            best_score = sim
                            best_reason = f"name similarity ({sim:.0%})"

            # Strategy 5: Structural match — same parent, similar position
            # If the unknown's parent xpath IS in the concordance, and an
            # unused concordance field has the same parent, likely a rename
            if best_score < 0.5:
                parent = unk.parent_xpath
                if parent in self.all_conc_xpaths:
                    # Find concordance fields under the same parent that
                    # are unused (never seen in filings)
                    for unused_name in results.unused_fields:
                        unused_info = self.fields.get(unused_name, {})
                        unused_xpaths = unused_info.get("xpaths", {})
                        for v, uxpath in unused_xpaths.items():
                            u_parts = uxpath.strip("/").split("/")
                            u_parent = "/" + "/".join(u_parts[:-1]) if len(u_parts) > 1 else ""
                            if u_parent == parent:
                                # Same parent, unused — probable rename
                                u_leaf = u_parts[-1] if u_parts else ""
                                sim = self._name_similarity(unk.element_name, u_leaf)
                                score = max(0.5, sim) if sim > 0.3 else 0.45
                                if score > best_score:
                                    best_match = uxpath
                                    best_score = score
                                    best_reason = f"same parent, unused field (similarity {sim:.0%})"

            # Apply match if confidence is high enough
            if best_score >= 0.45:
                unk.fuzzy_match = best_match
                unk.fuzzy_match_score = round(best_score, 3)
                unk.fuzzy_match_reason = best_reason

        # Count fuzzy matches
        results.fuzzy_matched = sum(
            1 for u in results.unknowns.values() if u.fuzzy_match)
        results.fuzzy_matches = [
            (u.xpath, u.fuzzy_match, u.fuzzy_match_score, u.fuzzy_match_reason)
            for u in sorted(results.unknowns.values(),
                            key=lambda u: -u.fuzzy_match_score)
            if u.fuzzy_match
        ]

    @staticmethod
    def _expand_irs_abbreviations(name: str) -> str:
        """Expand common IRS abbreviations to normalized form for comparison.
        This lets us match 'BsCmpnstnFlngOrgAmt' to 'BaseCompensationFilingOrgAmt'."""
        # Split PascalCase into words
        words = re.findall(r'[A-Z][a-z]*|[a-z]+|[A-Z]+(?=[A-Z][a-z]|\d|\b)', name)
        if not words:
            return name.lower()

        # Normalize each word
        ABBREVS = {
            "amt": "amount", "grp": "group", "ind": "indicator",
            "cnt": "count", "rt": "rate", "txt": "text", "cd": "code",
            "dt": "date", "nm": "name", "yr": "year", "num": "number",
            "org": "organization", "orgn": "organization",
            "nztn": "organization", "nzn": "organization",
            "cmpnstn": "compensation", "cmpnst": "compensation",
            "rltd": "related", "offcr": "officer", "trst": "trust",
            "empl": "employee", "cmps": "compensation",
            "flng": "filing", "bs": "base", "bns": "bonus",
            "dfrd": "deferred", "svrnc": "severance",
            "prsn": "person", "addr": "address",
            "desc": "description", "frgn": "foreign",
            "rpt": "report", "rptbl": "reportable",
            "cntrbtn": "contribution", "cntrbtns": "contributions",
            "grnts": "grants", "grnt": "grant",
            "fndrsng": "fundraising", "invst": "investment",
            "pblc": "public", "sprt": "support",
            "ttl": "total", "rvn": "revenue", "rvns": "revenues",
            "expns": "expenses", "expnss": "expenses",
            "bnft": "benefit", "bnfts": "benefits",
            "schdl": "schedule", "pymnt": "payment",
            "dsqlfyng": "disqualifying", "xcs": "excess",
            "dsrgrd": "disregarded", "prtnr": "partner",
            "shr": "share", "shrhld": "shareholder",
            "id": "identification", "eoy": "endofyear",
            "boy": "beginningofyear", "cy": "currentyear",
            "py": "prioryear",
        }

        normalized = []
        for w in words:
            wl = w.lower()
            normalized.append(ABBREVS.get(wl, wl))
        return " ".join(normalized)

    @staticmethod
    def _suffix_match(unknown_name: str, conc_name: str,
                      unknown_sched: str, conc_xpaths: list) -> tuple:
        """Check if names match via common prefix/suffix patterns.
        Returns (score, reason)."""
        un = unknown_name.lower()
        cn = conc_name.lower()

        # One is a substring of the other (e.g., RevenueAmt vs CYRevenueAmt)
        if len(un) > 5 and len(cn) > 5:
            if un in cn:
                # Check same schedule
                same_sched = any(
                    xp.strip("/").split("/")[0] == unknown_sched
                    for xp in conc_xpaths)
                score = 0.75 if same_sched else 0.55
                return score, f"'{unknown_name}' is substring of '{conc_name}'"
            if cn in un:
                same_sched = any(
                    xp.strip("/").split("/")[0] == unknown_sched
                    for xp in conc_xpaths)
                score = 0.75 if same_sched else 0.55
                return score, f"'{conc_name}' is substring of '{unknown_name}'"

        # Same suffix (last 8+ chars) — catches type-suffix patterns
        if len(un) > 8 and len(cn) > 8 and un[-8:] == cn[-8:]:
            same_sched = any(
                xp.strip("/").split("/")[0] == unknown_sched
                for xp in conc_xpaths)
            score = 0.6 if same_sched else 0.45
            return score, f"shared suffix '{unknown_name[-8:]}'"

        return 0.0, ""

    @staticmethod
    def _name_similarity(name1: str, name2: str) -> float:
        """Compute similarity between two element names using character
        bigram overlap (Dice coefficient). Fast and works well for
        detecting abbreviation-based renames."""
        def bigrams(s):
            s = s.lower()
            return set(s[i:i+2] for i in range(len(s)-1))

        if not name1 or not name2:
            return 0.0
        b1 = bigrams(name1)
        b2 = bigrams(name2)
        if not b1 or not b2:
            return 0.0
        overlap = len(b1 & b2)
        return 2.0 * overlap / (len(b1) + len(b2))


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

def write_unknown_xpaths_csv(results: AuditResults, output_dir: str):
    """Write CSV of all unknown xpaths for review."""
    path = os.path.join(output_dir, "unknown_xpaths.csv")

    # Sort: high-frequency first, then by schedule, then by xpath
    unknowns = sorted(
        results.unknowns.values(),
        key=lambda u: (-u.filing_count, u.schedule, u.xpath),
    )

    headers = [
        "xpath", "element_name", "schedule", "parent_xpath",
        "filing_count", "pct_of_filings", "versions_seen",
        "version_covered", "data_type_guess", "sample_value",
        "fuzzy_match", "fuzzy_score", "fuzzy_reason",
    ]

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for unk in unknowns:
            pct = (unk.filing_count / results.filings_audited * 100
                   if results.filings_audited > 0 else 0)
            writer.writerow({
                "xpath": unk.xpath,
                "element_name": unk.element_name,
                "schedule": unk.schedule,
                "parent_xpath": unk.parent_xpath,
                "filing_count": unk.filing_count,
                "pct_of_filings": f"{pct:.1f}%",
                "versions_seen": ";".join(sorted(unk.versions_seen)),
                "version_covered": "YES" if unk.version_covered else "NO",
                "data_type_guess": unk.data_type_guess,
                "sample_value": unk.sample_value[:100],
                "fuzzy_match": unk.fuzzy_match,
                "fuzzy_score": f"{unk.fuzzy_match_score:.0%}" if unk.fuzzy_match else "",
                "fuzzy_reason": unk.fuzzy_match_reason,
            })


def write_audit_json(results: AuditResults, output_dir: str):
    """Write machine-readable audit report."""
    path = os.path.join(output_dir, "audit_report.json")

    # Convert unknowns to serializable format
    unknowns_list = []
    for xpath, unk in sorted(results.unknowns.items()):
        entry = {
            "xpath": unk.xpath,
            "element_name": unk.element_name,
            "schedule": unk.schedule,
            "parent_xpath": unk.parent_xpath,
            "filing_count": unk.filing_count,
            "versions_seen": sorted(unk.versions_seen),
            "version_covered": unk.version_covered,
            "data_type_guess": unk.data_type_guess,
            "sample_value": unk.sample_value,
        }
        if unk.fuzzy_match:
            entry["fuzzy_match"] = unk.fuzzy_match
            entry["fuzzy_match_score"] = unk.fuzzy_match_score
            entry["fuzzy_match_reason"] = unk.fuzzy_match_reason
        unknowns_list.append(entry)

    # Fuzzy matches as a separate top-level section
    fuzzy_list = []
    for unk_xpath, match_xpath, score, reason in results.fuzzy_matches:
        fuzzy_list.append({
            "unknown_xpath": unk_xpath,
            "matched_xpath": match_xpath,
            "confidence": score,
            "reason": reason,
        })

    report = {
        "audit_date": datetime.now().isoformat(),
        "summary": {
            "filings_audited": results.filings_audited,
            "unique_xpaths_in_filings": results.unique_xpaths_seen,
            "concordance_fields": results.concordance_fields,
            "matched_xpaths": results.matched_xpaths,
            "unknown_xpaths": results.unknown_xpaths,
            "unknowns_version_covered": results.unknowns_version_covered,
            "unknowns_version_missing": results.unknowns_version_missing,
            "fuzzy_matched": results.fuzzy_matched,
            "unused_concordance_fields": results.unused_concordance,
            "coverage_pct": round(results.coverage_pct, 1),
        },
        "versions": {
            "in_concordance": results.versions_in_concordance,
            "in_filings": results.versions_in_filings,
            "missing_from_concordance": results.missing_versions,
        },
        "schedule_coverage": results.schedule_coverage,
        "warnings": results.warnings,
        "fuzzy_matches": fuzzy_list,
        "unknown_xpaths": unknowns_list,
        "unused_concordance_fields": results.unused_fields,
    }

    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)


def write_audit_markdown(results: AuditResults, output_dir: str):
    """Write human-readable audit report."""
    path = os.path.join(output_dir, "audit_report.md")
    r = results

    with open(path, "w", encoding="utf-8") as f:
        f.write("# IRS 990 Concordance Audit Report\n\n")
        f.write(f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")

        # Summary
        f.write("## Summary\n\n")
        f.write(f"| Metric | Value |\n")
        f.write(f"|--------|-------|\n")
        f.write(f"| Filings audited | {r.filings_audited} |\n")
        f.write(f"| Unique xpaths in filings | {r.unique_xpaths_seen} |\n")
        f.write(f"| Concordance fields | {r.concordance_fields} |\n")
        f.write(f"| Xpaths matched | {r.matched_xpaths} |\n")
        f.write(f"| **Unknown xpaths (NOT in concordance)** | **{r.unknown_xpaths}** |\n")
        f.write(f"| ↳ Version covered (real gaps) | {r.unknowns_version_covered} |\n")
        f.write(f"| ↳ Version missing (possible renames) | {r.unknowns_version_missing} |\n")
        f.write(f"| Fuzzy matches found | {r.fuzzy_matched} |\n")
        f.write(f"| Unused concordance fields | {r.unused_concordance} |\n")
        f.write(f"| **Coverage** | **{r.coverage_pct:.1f}%** |\n\n")

        # Warnings
        if r.warnings:
            f.write("## ⚠️ Warnings\n\n")
            for w in r.warnings:
                f.write(f"- {w}\n")
            f.write("\n")

        # Version analysis
        f.write("## Schema Version Coverage\n\n")
        f.write(f"**Concordance versions:** {', '.join(r.versions_in_concordance)}\n\n")
        f.write(f"**Filing versions seen:** {', '.join(r.versions_in_filings)}\n\n")
        if r.missing_versions:
            f.write(f"**⚠️ MISSING from concordance:** {', '.join(r.missing_versions)}\n\n")
            f.write("Filings with these versions may have fields that the concordance "
                    "can't identify. Run the concordance builder against these schema "
                    "versions to fix.\n\n")
        else:
            f.write("✅ All filing versions are covered by the concordance.\n\n")

        # Per-schedule coverage
        f.write("## Schedule Coverage\n\n")
        f.write(f"| Schedule | Total XPaths | Matched | Unknown | Coverage |\n")
        f.write(f"|----------|-------------|---------|---------|----------|\n")
        for sched, cov in sorted(r.schedule_coverage.items()):
            flag = " ⚠️" if cov["coverage_pct"] < 50 and cov["total_xpaths"] > 5 else ""
            f.write(f"| {sched} | {cov['total_xpaths']} | "
                    f"{cov['matched']} | {cov['unknown']} | "
                    f"{cov['coverage_pct']}%{flag} |\n")
        f.write("\n")

        # Fuzzy matches
        if r.fuzzy_matches:
            f.write("## 🔍 Fuzzy Matches (Probable Renames)\n\n")
            f.write("These unknown xpaths have probable matches in the concordance. "
                    "High-confidence matches (≥80%) are likely renames. "
                    "Medium (60-79%) are probable. Low (<60%) need manual review.\n\n")
            f.write(f"| Confidence | Unknown XPath | → Matched XPath | Reason |\n")
            f.write(f"|------------|---------------|-----------------|--------|\n")
            for unk_xpath, match_xpath, score, reason in r.fuzzy_matches:
                if score >= 0.8:
                    badge = "🟢 HIGH"
                elif score >= 0.6:
                    badge = "🟡 MED"
                else:
                    badge = "🔴 LOW"
                f.write(f"| {badge} ({score:.0%}) | `{unk_xpath}` | "
                        f"`{match_xpath}` | {reason} |\n")
            f.write("\n")

        # Top unknown xpaths — split by version classification
        high_freq = sorted(
            r.unknowns.values(),
            key=lambda u: -u.filing_count,
        )

        # Real gaps (version covered)
        real_gaps = [u for u in high_freq if u.version_covered]
        if real_gaps:
            f.write("## ❌ Real Concordance Gaps (version covered)\n\n")
            f.write("These xpaths appear in filings whose schema version IS in the "
                    "concordance — the concordance is genuinely missing these fields.\n\n")
            f.write(f"| XPath | Schedule | Filings | Type | Fuzzy Match | Sample |\n")
            f.write(f"|-------|----------|---------|------|-------------|--------|\n")
            for unk in real_gaps[:30]:
                pct = (unk.filing_count / r.filings_audited * 100
                       if r.filings_audited > 0 else 0)
                sample = unk.sample_value[:30].replace("|", "\\|") if unk.sample_value else ""
                fuzzy = f"→ `{unk.fuzzy_match.split('/')[-1]}` ({unk.fuzzy_match_score:.0%})" if unk.fuzzy_match else ""
                f.write(f"| `{unk.xpath}` | {unk.schedule} | "
                        f"{unk.filing_count} ({pct:.0f}%) | "
                        f"{unk.data_type_guess} | {fuzzy} | {sample} |\n")
            if len(real_gaps) > 30:
                f.write(f"\n*... and {len(real_gaps) - 30} more.*\n")
            f.write("\n")

        # Possible renames (version missing)
        possible_renames = [u for u in high_freq if not u.version_covered]
        if possible_renames:
            f.write("## ❓ Possible Renames (version missing from concordance)\n\n")
            f.write("These xpaths appear in filings whose schema version is NOT in the "
                    "concordance. They may be renamed fields or genuinely new fields — "
                    "can't tell without the schema.\n\n")
            f.write(f"| XPath | Schedule | Filings | Versions | Fuzzy Match | Sample |\n")
            f.write(f"|-------|----------|---------|----------|-------------|--------|\n")
            for unk in possible_renames[:30]:
                pct = (unk.filing_count / r.filings_audited * 100
                       if r.filings_audited > 0 else 0)
                sample = unk.sample_value[:30].replace("|", "\\|") if unk.sample_value else ""
                versions = ", ".join(sorted(unk.versions_seen))
                fuzzy = f"→ `{unk.fuzzy_match.split('/')[-1]}` ({unk.fuzzy_match_score:.0%})" if unk.fuzzy_match else ""
                f.write(f"| `{unk.xpath}` | {unk.schedule} | "
                        f"{unk.filing_count} ({pct:.0f}%) | "
                        f"{versions} | {fuzzy} | {sample} |\n")
            if len(possible_renames) > 30:
                f.write(f"\n*... and {len(possible_renames) - 30} more.*\n")
            f.write("\n")

        # Unused concordance fields
        if r.unused_fields:
            f.write("## Unused Concordance Fields\n\n")
            f.write(f"These {r.unused_concordance} fields are in the concordance "
                    f"but were never seen in any audited filing. They may be:\n\n")
            f.write("- Fields from schedules not present in the sample filings\n")
            f.write("- Deprecated fields no longer used\n")
            f.write("- Errors in the concordance\n\n")
            if len(r.unused_fields) <= 30:
                for name in r.unused_fields:
                    f.write(f"- `{name}`\n")
            else:
                for name in r.unused_fields[:20]:
                    f.write(f"- `{name}`\n")
                f.write(f"\n*... and {len(r.unused_fields) - 20} more. "
                        f"See audit_report.json for the full list.*\n")
            f.write("\n")


def write_patch_file(results: AuditResults, output_dir: str):
    """Write a patch file that can be used to extend the concordance with
    unknown xpaths. Only includes unknowns seen in 2+ filings."""
    path = os.path.join(output_dir, "concordance_patch.json")

    patch_fields = {}
    for xpath, unk in results.unknowns.items():
        if unk.filing_count < 2:
            continue

        # Generate a canonical name
        parts = xpath.strip("/").split("/")
        if len(parts) >= 2:
            canonical = "_".join(parts[1:])  # drop schedule prefix
        else:
            canonical = parts[-1] if parts else xpath

        patch_fields[canonical] = {
            "xpath": xpath,
            "schedule": unk.schedule,
            "element_name": unk.element_name,
            "type": unk.data_type_guess,
            "label": "",  # needs manual review
            "description": "",  # needs manual review
            "versions_seen": sorted(unk.versions_seen),
            "filing_count": unk.filing_count,
            "sample_value": unk.sample_value,
            "version_covered": unk.version_covered,
            "fuzzy_match": unk.fuzzy_match or None,
            "fuzzy_match_score": unk.fuzzy_match_score if unk.fuzzy_match else None,
            "fuzzy_match_reason": unk.fuzzy_match_reason or None,
            "status": "PROBABLE_RENAME" if unk.fuzzy_match_score >= 0.8
                      else "POSSIBLE_RENAME" if unk.fuzzy_match_score >= 0.6
                      else "NEEDS_REVIEW",
        }

    patch = {
        "generated": datetime.now().isoformat(),
        "description": (
            "Proposed concordance additions discovered by auditing real filings. "
            "Each entry needs manual review: verify the xpath is correct, add a "
            "human-readable label and description, and confirm the data type."
        ),
        "fields_to_add": patch_fields,
        "count": len(patch_fields),
    }

    with open(path, "w", encoding="utf-8") as f:
        json.dump(patch, f, indent=2, ensure_ascii=False)

    return len(patch_fields)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Audit concordance against real IRS 990 XML filings.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --concordance ./field_lookup.json --xml-dir ./990_xmls --output-dir ./audit
  %(prog)s --concordance ./field_lookup.json --xml-file ./filing.xml --output-dir ./audit
  %(prog)s --concordance ./field_lookup.json --xml-dir ./990_xmls --output-dir ./audit --patch
        """
    )
    parser.add_argument("--concordance", required=True,
                        help="Path to field_lookup.json from concordance builder")
    parser.add_argument("--xml-file", help="Single XML filing to audit against")
    parser.add_argument("--xml-dir", help="Directory of XML filings to audit against")
    parser.add_argument("--output-dir", default="./audit_output",
                        help="Directory for audit output files")
    parser.add_argument("--patch", action="store_true",
                        help="Generate a patch file for unknown xpaths")
    parser.add_argument("--verbose", action="store_true",
                        help="Show per-filing progress")
    parser.add_argument("--max-filings", type=int, default=0,
                        help="Max filings to process (0 = all)")

    args = parser.parse_args()

    print("=" * 60)
    print("IRS 990 Concordance Auditor")
    print("=" * 60)

    # Load concordance
    if not os.path.exists(args.concordance):
        sys.exit(f"ERROR: Concordance file not found: {args.concordance}")

    auditor = ConcordanceAuditor(args.concordance)
    print(f"\nConcordance: {auditor.results.concordance_fields} fields, "
          f"{len(auditor.conc_versions)} versions")

    # Collect XML files
    xml_files = []
    if args.xml_file:
        if os.path.exists(args.xml_file):
            xml_files.append(args.xml_file)
    if args.xml_dir:
        xml_dir = Path(args.xml_dir)
        if xml_dir.is_dir():
            xml_files.extend(sorted(str(f) for f in xml_dir.glob("*.xml")))
            # Also check subdirectories one level deep
            for subdir in sorted(xml_dir.iterdir()):
                if subdir.is_dir():
                    xml_files.extend(sorted(str(f) for f in subdir.glob("*.xml")))

    if not xml_files:
        sys.exit("ERROR: No XML files found. Use --xml-file or --xml-dir.")

    if args.max_filings > 0:
        xml_files = xml_files[:args.max_filings]

    print(f"XML filings to audit: {len(xml_files)}")

    # Parse and audit each filing
    filing_parser = FilingParser()
    for i, xml_path in enumerate(xml_files):
        if args.verbose:
            print(f"  [{i+1}/{len(xml_files)}] {Path(xml_path).name}", end="")

        filing = filing_parser.parse(xml_path)
        auditor.audit_filing(filing)

        if args.verbose:
            print(f" — v{filing.return_version}, "
                  f"{filing.total_elements} elements, "
                  f"{len(filing.schedules_present)} schedules")
        elif (i + 1) % 100 == 0:
            print(f"  Processed {i+1}/{len(xml_files)} filings...")

    # Finalize results
    print(f"\nFinalizing audit...")
    results = auditor.finalize()

    # Print summary
    print(f"\n{'='*60}")
    print(f"AUDIT RESULTS")
    print(f"{'='*60}")
    print(f"  Filings audited:        {results.filings_audited}")
    print(f"  Unique xpaths seen:     {results.unique_xpaths_seen}")
    print(f"  Matched (in concordance): {results.matched_xpaths}")
    print(f"  Unknown (NOT in conc.):   {results.unknown_xpaths}")
    print(f"    ↳ Version covered (real gaps):     {results.unknowns_version_covered}")
    print(f"    ↳ Version missing (possible renames): {results.unknowns_version_missing}")
    print(f"  Fuzzy matches found:      {results.fuzzy_matched}")
    print(f"  Unused concordance:       {results.unused_concordance}")
    print(f"  Coverage:                 {results.coverage_pct:.1f}%")

    if results.missing_versions:
        print(f"\n  ⚠️ MISSING VERSIONS: {', '.join(results.missing_versions)}")

    if results.warnings:
        print(f"\n  Warnings:")
        for w in results.warnings:
            print(f"    ⚠️ {w}")

    # Write outputs
    os.makedirs(args.output_dir, exist_ok=True)
    write_audit_json(results, args.output_dir)
    write_audit_markdown(results, args.output_dir)
    write_unknown_xpaths_csv(results, args.output_dir)

    patch_count = 0
    if args.patch:
        patch_count = write_patch_file(results, args.output_dir)

    print(f"\nOutput written to: {args.output_dir}/")
    print(f"  audit_report.json      — Machine-readable audit results")
    print(f"  audit_report.md        — Human-readable summary")
    print(f"  unknown_xpaths.csv     — All {results.unknown_xpaths} unknown xpaths")
    if args.patch:
        print(f"  concordance_patch.json — {patch_count} proposed additions "
              f"(seen in 2+ filings)")

    print(f"\n{'='*60}")
    print(f"Done!")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
