#!/usr/bin/env python
"""
Build a unified grantee list from multiple extracted 990 CSV files.

Combines grant outflow data from RecipientTable, GrantOrContributionPdDurYrGrp,
GrantOrContriApprvForFutGrp, ExpenditureResponsibilityGrp, Schedule L interested
persons, Schedule O free-text narratives, and Schedule I (grants to individuals,
supplemental info) into a single combined_grants.csv.

Usage:
    python build_grant_list.py --extracted-dir ./extracted_output --output ./extracted_output/combined_grants.csv
"""

import argparse
import csv
import os
import re
import sys


def load_grantor_lookup(extracted_dir):
    """Load scalar_fields.csv into a dict keyed by 'EIN|tax_period' -> org_name."""
    lookup = {}
    path = os.path.join(extracted_dir, 'scalar_fields.csv')
    if not os.path.exists(path):
        print("WARNING: scalar_fields.csv not found, grantor names will be empty")
        return lookup
    with open(path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            ein = row.get('EIN', '').strip()
            period = row.get('tax_period', '').strip()
            name = row.get('org_name', '').strip()
            if ein and period:
                lookup['%s|%s' % (ein, period)] = name
    return lookup


def first_nonempty(row, keys):
    """Return the first non-empty value from row for given keys."""
    for k in keys:
        val = row.get(k, '').strip()
        if val:
            return val
    return ''


def process_recipient_table(extracted_dir, grantor_lookup):
    """Process RecipientTable.csv — prefer Schedule I version (has more columns)."""
    # Schedule I extraction has IRC section, foreign addresses, etc.
    sched_i_path = os.path.join(extracted_dir, 'IRS990ScheduleI', 'RecipientTable.csv')
    main_path = os.path.join(extracted_dir, 'RecipientTable.csv')
    path = sched_i_path if os.path.exists(sched_i_path) else main_path
    if not os.path.exists(path):
        return []
    pfx = 'RecipientTable_'
    rows = []
    with open(path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            ein = row.get('EIN', '').strip()
            period = row.get('tax_period', '').strip()
            grantee_name = first_nonempty(row, [
                pfx + 'RecipientBusinessName',
                pfx + 'RecipientBusinessName_BusinessNameLine1Txt',
            ])
            # Address: prefer US, fall back to foreign
            city = first_nonempty(row, [
                pfx + 'USAddress_CityNm',
                pfx + 'ForeignAddress_CityNm',
            ])
            state = first_nonempty(row, [
                pfx + 'USAddress_StateAbbreviationCd',
                pfx + 'ForeignAddress_ProvinceOrStateNm',
            ])
            zip_code = first_nonempty(row, [
                pfx + 'USAddress_ZIPCd',
                pfx + 'ForeignAddress_ForeignPostalCd',
            ])
            country = row.get(pfx + 'ForeignAddress_CountryCd', '').strip()
            rows.append({
                'grantor_ein': ein,
                'grantor_name': grantor_lookup.get('%s|%s' % (ein, period), ''),
                'tax_period': period,
                'grantee_ein': row.get(pfx + 'RecipientEIN', '').strip(),
                'grantee_name': grantee_name,
                'amount': row.get(pfx + 'CashGrantAmt', '').strip(),
                'non_cash_amount': row.get(pfx + 'NonCashAssistanceAmt', '').strip(),
                'non_cash_description': row.get(pfx + 'NonCashAssistanceDesc', '').strip(),
                'purpose': row.get(pfx + 'PurposeOfGrantTxt', '').strip(),
                'irc_section': row.get(pfx + 'IRCSectionDesc', '').strip(),
                'valuation_method': row.get(pfx + 'ValuationMethodUsedDesc', '').strip(),
                'address': first_nonempty(row, [
                    pfx + 'USAddress_AddressLine1Txt',
                    pfx + 'ForeignAddress_AddressLine1Txt',
                ]),
                'city': city,
                'state': state,
                'zip_code': zip_code,
                'country': country,
                'source': 'RecipientTable',
            })
    return rows


def process_grant_paid_during_year(extracted_dir, grantor_lookup):
    """Process GrantOrContributionPdDurYrGrp.csv — 990PF paid grants."""
    path = os.path.join(extracted_dir, 'GrantOrContributionPdDurYrGrp.csv')
    if not os.path.exists(path):
        return []
    pfx = 'SupplementaryInformationGrp_GrantOrContributionPdDurYrGrp_'
    rows = []
    with open(path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            ein = row.get('EIN', '').strip()
            period = row.get('tax_period', '').strip()
            grantee_name = first_nonempty(row, [
                pfx + 'RecipientBusinessName',
                pfx + 'RecipientBusinessName_BusinessNameLine1Txt',
                pfx + 'RecipientPersonNm',
            ])
            rows.append({
                'grantor_ein': ein,
                'grantor_name': grantor_lookup.get('%s|%s' % (ein, period), ''),
                'tax_period': period,
                'grantee_ein': '',
                'grantee_name': grantee_name,
                'amount': row.get(pfx + 'Amt', '').strip(),
                'purpose': row.get(pfx + 'GrantOrContributionPurposeTxt', '').strip(),
                'source': 'GrantOrContributionPdDurYrGrp',
            })
    return rows


def process_grant_approved_future(extracted_dir, grantor_lookup):
    """Process GrantOrContriApprvForFutGrp.csv — 990PF approved-for-future."""
    path = os.path.join(extracted_dir, 'GrantOrContriApprvForFutGrp.csv')
    if not os.path.exists(path):
        return []
    pfx = 'SupplementaryInformationGrp_GrantOrContriApprvForFutGrp_'
    rows = []
    with open(path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            ein = row.get('EIN', '').strip()
            period = row.get('tax_period', '').strip()
            grantee_name = first_nonempty(row, [
                pfx + 'RecipientBusinessName_BusinessNameLine1Txt',
                pfx + 'RecipientBusinessName',
            ])
            rows.append({
                'grantor_ein': ein,
                'grantor_name': grantor_lookup.get('%s|%s' % (ein, period), ''),
                'tax_period': period,
                'grantee_ein': '',
                'grantee_name': grantee_name,
                'amount': row.get(pfx + 'Amt', '').strip(),
                'purpose': row.get(pfx + 'GrantOrContributionPurposeTxt', '').strip(),
                'source': 'GrantOrContriApprvForFutGrp',
            })
    return rows


def process_expenditure_responsibility(extracted_dir, grantor_lookup):
    """Process ExpenditureResponsibilityGrp.csv — restricted grants."""
    path = os.path.join(extracted_dir, 'ExpenditureResponsibilityGrp.csv')
    if not os.path.exists(path):
        return []
    pfx = 'ExpenditureResponsibilityGrp_'
    rows = []
    with open(path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            ein = row.get('EIN', '').strip()
            period = row.get('tax_period', '').strip()
            rows.append({
                'grantor_ein': ein,
                'grantor_name': grantor_lookup.get('%s|%s' % (ein, period), ''),
                'tax_period': period,
                'grantee_ein': '',
                'grantee_name': row.get(pfx + 'BusinessName_BusinessNameLine1Txt', '').strip(),
                'amount': row.get(pfx + 'GrantAmt', '').strip(),
                'purpose': '',
                'source': 'ExpenditureResponsibilityGrp',
            })
    return rows


def process_schedule_l(extracted_dir, grantor_lookup):
    """Process GrntAsstBnftInterestedPrsnGrp.csv — Schedule L interested persons."""
    path = os.path.join(extracted_dir, 'IRS990ScheduleL', 'GrntAsstBnftInterestedPrsnGrp.csv')
    if not os.path.exists(path):
        return []
    pfx = 'GrntAsstBnftInterestedPrsnGrp_'
    rows = []
    with open(path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            ein = row.get('EIN', '').strip()
            period = row.get('tax_period', '').strip()
            grantee_name = first_nonempty(row, [
                pfx + 'PersonNm',
                pfx + 'BusinessName_BusinessNameLine1Txt',
            ])
            rows.append({
                'grantor_ein': ein,
                'grantor_name': grantor_lookup.get('%s|%s' % (ein, period), ''),
                'tax_period': period,
                'grantee_ein': '',
                'grantee_name': grantee_name,
                'amount': row.get(pfx + 'CashGrantAmt', '').strip(),
                'purpose': row.get(pfx + 'AssistancePurposeTxt', '').strip(),
                'source': 'ScheduleL',
            })
    return rows


# --- Schedule O parsing helpers ---

# Pattern: "Donee's Name: X" or "Grantee Name: X"
_RE_DONEE_NAME = re.compile(
    r"(?:Donee'?s?\s*Name|Grantee\s*Name|GRANTEE)\s*:?\s*(.+?)(?:\||Donee|Address|Cash|Amount|\$|$)",
    re.IGNORECASE
)

# Pattern: "AFFILIATE NAME: X" or "NAME: X"
_RE_NAMED = re.compile(
    r"(?:AFFILIATE\s+NAME|NAME)\s*:\s*(.+?)(?:\s*(?:AFFILIATE\s+)?ADDRESS|PURPOSE|\.|$)",
    re.IGNORECASE
)

# Pattern: "Activity ..., Grantee Name:, Grantee Address:, Amount:" pipe-delimited rows
# e.g. "| Scholarship funding, Weber State University, ..."
_RE_PIPE_ENTRY = re.compile(
    r"\|\s*([^,|]+),\s*([^,|]+),\s*\"?([^\"]*?)\"?,\s*\$?([\d,.]+)",
    re.IGNORECASE
)

# Dollar amount extraction
_RE_DOLLAR = re.compile(r'\$\s*([\d,]+(?:\.\d+)?)')
_RE_AMOUNT_LABEL = re.compile(
    r'(?:AMOUNT(?:\s+OF\s+PAYMENT)?|CASH\s+(?:CONTRIBUTION|AMOUNT\s+GIVEN))\s*:?\s*\$?\s*([\d,]+(?:\.\d+)?)',
    re.IGNORECASE
)

# "Grants And Similar Amounts Paid:, Amount:" pipe-delimited simple format
# e.g. "| Artistic Fees paid to Ruthie Foster, $2500|"
_RE_SIMPLE_PIPE = re.compile(
    r'\|\s*([^,$|]+?)\s*,\s*\$\s*([\d,]+(?:\.\d+)?)\s*\|',
    re.IGNORECASE
)


def parse_schedule_o_row(text):
    """Parse a Schedule O free-text grant narrative. Returns list of (name, amount) tuples."""
    results = []

    # Strategy 1: Pipe-delimited with labeled columns (Donee's Name format)
    m = _RE_DONEE_NAME.search(text)
    if m:
        name = m.group(1).strip().rstrip('|').strip()
        amt_m = _RE_DOLLAR.search(text)
        amt = amt_m.group(1).replace(',', '') if amt_m else ''
        if name:
            results.append((name, amt))
            return results

    # Strategy 2: AFFILIATE NAME / NAME: format
    for m in _RE_NAMED.finditer(text):
        name = m.group(1).strip().rstrip('.')
        # find amount after this match
        remainder = text[m.end():]
        amt_m = _RE_AMOUNT_LABEL.search(remainder)
        if not amt_m:
            amt_m = _RE_DOLLAR.search(remainder)
        amt = amt_m.group(1).replace(',', '') if amt_m else ''
        if name:
            results.append((name, amt))
    if results:
        return results

    # Strategy 3: Pipe-delimited multi-entry (activity, name, address, amount)
    for m in _RE_PIPE_ENTRY.finditer(text):
        name = m.group(2).strip()
        amt = m.group(4).replace(',', '')
        if name:
            results.append((name, amt))
    if results:
        return results

    # Strategy 4: Simple pipe-delimited "| description, $amount|"
    for m in _RE_SIMPLE_PIPE.finditer(text):
        name = m.group(1).strip()
        amt = m.group(2).replace(',', '')
        if name:
            results.append((name, amt))
    if results:
        return results

    # Strategy 5: Amount label without structured name — use raw text
    amt_m = _RE_AMOUNT_LABEL.search(text)
    if amt_m:
        amt = amt_m.group(1).replace(',', '')
        results.append((text[:120].strip(), amt))
        return results

    # Fallback: try to find any dollar amount
    amt_m = _RE_DOLLAR.search(text)
    amt = amt_m.group(1).replace(',', '') if amt_m else ''
    results.append((text[:120].strip(), amt))
    return results


def process_schedule_o(extracted_dir, grantor_lookup):
    """Process Schedule O supplemental info — filter grant-related, regex-parse."""
    path = os.path.join(extracted_dir, 'IRS990ScheduleO', 'SupplementalInformationDetail.csv')
    if not os.path.exists(path):
        return []

    ref_col = 'IRS990ScheduleO_SupplementalInformationDetail_FormAndLineReferenceDesc'
    txt_col = 'IRS990ScheduleO_SupplementalInformationDetail_ExplanationTxt'

    # Patterns to filter grant-related rows
    grant_patterns = re.compile(
        r'(?:line\s*10|grants\s+and\s+similar)',
        re.IGNORECASE
    )
    # Exclude Part X line 10 (balance sheet) and other non-grant line 10s
    exclude_patterns = re.compile(
        r'part\s+(?:x|vi|vii|viii|ix|xi)',
        re.IGNORECASE
    )

    rows = []
    with open(path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            ref = row.get(ref_col, '').strip()
            if not grant_patterns.search(ref):
                continue
            # Exclude balance sheet or other non-Part-I references
            if exclude_patterns.search(ref) and 'part i' not in ref.lower():
                continue

            ein = row.get('EIN', '').strip()
            period = row.get('tax_period', '').strip()
            text = row.get(txt_col, '').strip()
            if not text:
                continue

            parsed = parse_schedule_o_row(text)
            for name, amt in parsed:
                rows.append({
                    'grantor_ein': ein,
                    'grantor_name': grantor_lookup.get('%s|%s' % (ein, period), ''),
                    'tax_period': period,
                    'grantee_ein': '',
                    'grantee_name': name,
                    'amount': amt,
                    'purpose': text,
                    'source': 'ScheduleO',
                })
    return rows


def process_grants_to_individuals(extracted_dir, grantor_lookup):
    """Process GrantsOtherAsstToIndivInUSGrp.csv — Schedule I grants to individuals (aggregate)."""
    path = os.path.join(extracted_dir, 'IRS990ScheduleI', 'GrantsOtherAsstToIndivInUSGrp.csv')
    if not os.path.exists(path):
        return []
    pfx = 'GrantsOtherAsstToIndivInUSGrp_'
    rows = []
    with open(path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            ein = row.get('EIN', '').strip()
            period = row.get('tax_period', '').strip()
            rows.append({
                'grantor_ein': ein,
                'grantor_name': grantor_lookup.get('%s|%s' % (ein, period), ''),
                'tax_period': period,
                'grantee_ein': '',
                'grantee_name': '',
                'amount': row.get(pfx + 'CashGrantAmt', '').strip(),
                'non_cash_amount': row.get(pfx + 'NonCashAssistanceAmt', '').strip(),
                'non_cash_description': row.get(pfx + 'NonCashAssistanceDesc', '').strip(),
                'purpose': row.get(pfx + 'GrantTypeTxt', '').strip(),
                'recipient_count': row.get(pfx + 'RecipientCnt', '').strip(),
                'valuation_method': row.get(pfx + 'ValuationMethodUsedDesc', '').strip(),
                'source': 'GrantsOtherAsstToIndivInUSGrp',
            })
    return rows


def process_schedule_i_supplemental(extracted_dir, grantor_lookup):
    """Process Schedule I SupplementalInformationDetail — narrative grant info."""
    path = os.path.join(extracted_dir, 'IRS990ScheduleI', 'SupplementalInformationDetail.csv')
    if not os.path.exists(path):
        return []
    pfx = 'IRS990ScheduleI_SupplementalInformationDetail_'
    rows = []
    with open(path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            ein = row.get('EIN', '').strip()
            period = row.get('tax_period', '').strip()
            text = row.get(pfx + 'ExplanationTxt', '').strip()
            if not text:
                continue
            rows.append({
                'grantor_ein': ein,
                'grantor_name': grantor_lookup.get('%s|%s' % (ein, period), ''),
                'tax_period': period,
                'grantee_ein': '',
                'grantee_name': '',
                'amount': '',
                'purpose': text,
                'source': 'ScheduleI_Supplemental',
            })
    return rows


def write_combined(all_rows, output_path):
    """Write combined_grants.csv."""
    fieldnames = [
        'grantor_ein', 'grantor_name', 'tax_period',
        'grantee_ein', 'grantee_name', 'amount',
        'non_cash_amount', 'non_cash_description',
        'purpose', 'irc_section', 'recipient_count', 'valuation_method',
        'address', 'city', 'state', 'zip_code', 'country',
        'source',
    ]
    output_dir = os.path.dirname(output_path)
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir)
    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in all_rows:
            # Fill missing keys with empty string (non-Schedule-I sources)
            for key in fieldnames:
                row.setdefault(key, '')
            writer.writerow(row)


def print_summary(all_rows):
    """Print summary statistics."""
    source_counts = {}
    total_amount = 0.0
    total_non_cash = 0.0
    with_ein = 0
    with_name = 0
    with_amount = 0
    with_non_cash = 0
    with_address = 0

    for row in all_rows:
        src = row['source']
        source_counts[src] = source_counts.get(src, 0) + 1
        if row.get('grantee_ein'):
            with_ein += 1
        if row.get('grantee_name'):
            with_name += 1
        if row.get('city') or row.get('address'):
            with_address += 1
        amt_str = row.get('amount', '')
        if amt_str:
            try:
                amt = float(amt_str.replace(',', ''))
                total_amount += amt
                with_amount += 1
            except ValueError:
                pass
        nc_str = row.get('non_cash_amount', '')
        if nc_str:
            try:
                nc = float(nc_str.replace(',', ''))
                total_non_cash += nc
                with_non_cash += 1
            except ValueError:
                pass

    n = len(all_rows) if all_rows else 1
    print("\n=== Combined Grants Summary ===")
    print("Total rows: %d" % len(all_rows))
    print("\nRows per source:")
    for src in sorted(source_counts.keys()):
        print("  %-40s %6d" % (src, source_counts[src]))
    print("\nWith grantee EIN:    %6d (%.1f%%)" % (with_ein, 100.0 * with_ein / n))
    print("With grantee name:   %6d (%.1f%%)" % (with_name, 100.0 * with_name / n))
    print("With cash amount:    %6d (%.1f%%)" % (with_amount, 100.0 * with_amount / n))
    print("With non-cash:       %6d (%.1f%%)" % (with_non_cash, 100.0 * with_non_cash / n))
    print("With address:        %6d (%.1f%%)" % (with_address, 100.0 * with_address / n))
    print("Total cash amount:     $%s" % format(total_amount, ',.0f'))
    print("Total non-cash amount: $%s" % format(total_non_cash, ',.0f'))


def main():
    parser = argparse.ArgumentParser(description='Build unified grantee list from extracted 990 CSVs')
    parser.add_argument('--extracted-dir', default='./data/extracted',
                        help='Directory containing extracted CSVs (default: ./data/extracted)')
    parser.add_argument('--output', default='./data/extracted/combined_grants.csv',
                        help='Output path for combined CSV (default: ./data/extracted/combined_grants.csv)')
    args = parser.parse_args()

    if not os.path.isdir(args.extracted_dir):
        print("ERROR: extracted dir not found: %s" % args.extracted_dir)
        sys.exit(1)

    print("Loading grantor lookup from scalar_fields.csv...")
    grantor_lookup = load_grantor_lookup(args.extracted_dir)
    print("  %d grantor EIN/period entries loaded" % len(grantor_lookup))

    all_rows = []

    sources = [
        ("RecipientTable", process_recipient_table),
        ("GrantOrContributionPdDurYrGrp", process_grant_paid_during_year),
        ("GrantOrContriApprvForFutGrp", process_grant_approved_future),
        ("ExpenditureResponsibilityGrp", process_expenditure_responsibility),
        ("Schedule L", process_schedule_l),
        ("Schedule O", process_schedule_o),
        ("Schedule I GrantsToIndividuals", process_grants_to_individuals),
        ("Schedule I Supplemental", process_schedule_i_supplemental),
    ]

    for name, func in sources:
        print("Processing %s..." % name)
        rows = func(args.extracted_dir, grantor_lookup)
        print("  %d rows" % len(rows))
        all_rows.extend(rows)

    print("\nWriting %s..." % args.output)
    write_combined(all_rows, args.output)
    print("Done.")

    print_summary(all_rows)


if __name__ == '__main__':
    main()
