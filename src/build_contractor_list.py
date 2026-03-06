#!/usr/bin/env python
"""
Build a unified contractor/vendor list from multiple extracted 990 CSV files.

Combines contractor compensation, fee aggregates, and Schedule R related-org
transactions into a single combined_contractors.csv.

Usage:
    python build_contractor_list.py --extracted-dir ./extracted_output --output ./extracted_output/combined_contractors.csv
"""

import argparse
import csv
import os
import re
import sys


def load_payer_lookup(extracted_dir):
    """Load scalar_fields.csv into a dict keyed by 'EIN|tax_period' -> org_name."""
    lookup = {}
    path = os.path.join(extracted_dir, 'scalar_fields.csv')
    if not os.path.exists(path):
        print("WARNING: scalar_fields.csv not found, payer names will be empty")
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


_RE_DOLLAR = re.compile(r'\$\s*([\d,]+(?:\.\d+)?)')


def process_contractor_compensation(extracted_dir, payer_lookup):
    """Process ContractorCompensationGrp.csv — named contractors with compensation."""
    path = os.path.join(extracted_dir, 'ContractorCompensationGrp.csv')
    if not os.path.exists(path):
        return []
    pfx = 'ContractorCompensationGrp_'
    rows = []
    with open(path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            ein = row.get('EIN', '').strip()
            period = row.get('tax_period', '').strip()
            name = first_nonempty(row, [
                pfx + 'ContractorName_BusinessName_BusinessNameLine1Txt',
                pfx + 'ContractorName_PersonNm',
            ])
            rows.append({
                'payer_ein': ein,
                'payer_name': payer_lookup.get('%s|%s' % (ein, period), ''),
                'tax_period': period,
                'contractor_name': name,
                'amount': row.get(pfx + 'CompensationAmt', '').strip(),
                'service_description': row.get(pfx + 'ServicesDesc', '').strip(),
                'source': 'ContractorCompensationGrp',
            })
    return rows


def process_highest_paid_contractor(extracted_dir, payer_lookup):
    """Process CompensationOfHghstPdCntrctGrp.csv — use both prefix variants."""
    path = os.path.join(extracted_dir, 'CompensationOfHghstPdCntrctGrp.csv')
    if not os.path.exists(path):
        return []
    pfx1 = 'CompensationOfHghstPdCntrctGrp_'
    pfx2 = 'OfficerDirTrstKeyEmplInfoGrp_CompensationOfHghstPdCntrctGrp_'
    rows = []
    with open(path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            ein = row.get('EIN', '').strip()
            period = row.get('tax_period', '').strip()
            name = first_nonempty(row, [
                pfx1 + 'BusinessName_BusinessNameLine1Txt',
                pfx1 + 'PersonNm',
                pfx2 + 'BusinessName_BusinessNameLine1Txt',
                pfx2 + 'PersonNm',
            ])
            amount = first_nonempty(row, [
                pfx1 + 'CompensationAmt',
                pfx2 + 'CompensationAmt',
            ])
            desc = first_nonempty(row, [
                pfx1 + 'ServiceTypeTxt',
                pfx2 + 'ServiceTypeTxt',
            ])
            rows.append({
                'payer_ein': ein,
                'payer_name': payer_lookup.get('%s|%s' % (ein, period), ''),
                'tax_period': period,
                'contractor_name': name,
                'amount': amount,
                'service_description': desc,
                'source': 'CompensationOfHghstPdCntrctGrp',
            })
    return rows


def process_contractor_explanation(extracted_dir, payer_lookup):
    """Process ContractorCompExplnGrp.csv — regex dollar amount from ExplanationTxt."""
    path = os.path.join(extracted_dir, 'ContractorCompExplnGrp.csv')
    if not os.path.exists(path):
        return []
    pfx = 'ContractorCompExplnGrp_'
    rows = []
    with open(path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            ein = row.get('EIN', '').strip()
            period = row.get('tax_period', '').strip()
            name = first_nonempty(row, [
                pfx + 'ContractorBusinessName_BusinessNameLine1Txt',
                pfx + 'ContractorPersonNm',
            ])
            explanation = row.get(pfx + 'ExplanationTxt', '').strip()
            # Extract dollar amount from free text
            amount = ''
            m = _RE_DOLLAR.search(explanation)
            if m:
                amount = m.group(1).replace(',', '')
            rows.append({
                'payer_ein': ein,
                'payer_name': payer_lookup.get('%s|%s' % (ein, period), ''),
                'tax_period': period,
                'contractor_name': name,
                'amount': amount,
                'service_description': explanation,
                'source': 'ContractorCompExplnGrp',
            })
    return rows


def process_fees_for_services(extracted_dir, payer_lookup, filename, prefix, category):
    """Generic handler for FeesForServices*Grp.csv files — org-level fee aggregates."""
    path = os.path.join(extracted_dir, filename)
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            ein = row.get('EIN', '').strip()
            period = row.get('tax_period', '').strip()
            amount = row.get(prefix + '_TotalAmt', '').strip()
            rows.append({
                'payer_ein': ein,
                'payer_name': payer_lookup.get('%s|%s' % (ein, period), ''),
                'tax_period': period,
                'contractor_name': '',
                'amount': amount,
                'service_description': category,
                'source': filename.replace('.csv', ''),
            })
    return rows


def process_schedule_r_transactions(extracted_dir, payer_lookup):
    """Process IRS990ScheduleR/TransactionsRelatedOrgGrp.csv — related-org transactions."""
    path = os.path.join(extracted_dir, 'IRS990ScheduleR', 'TransactionsRelatedOrgGrp.csv')
    if not os.path.exists(path):
        return []
    pfx = 'TransactionsRelatedOrgGrp_'
    rows = []
    with open(path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            ein = row.get('EIN', '').strip()
            period = row.get('tax_period', '').strip()
            name = row.get(pfx + 'OtherOrganizationName_BusinessNameLine1Txt', '').strip()
            rows.append({
                'payer_ein': ein,
                'payer_name': payer_lookup.get('%s|%s' % (ein, period), ''),
                'tax_period': period,
                'contractor_name': name,
                'amount': row.get(pfx + 'InvolvedAmt', '').strip(),
                'service_description': row.get(pfx + 'TransactionTypeTxt', '').strip(),
                'source': 'ScheduleR_TransactionsRelatedOrgGrp',
            })
    return rows


def write_combined(all_rows, output_path):
    """Write combined_contractors.csv."""
    fieldnames = [
        'payer_ein', 'payer_name', 'tax_period',
        'contractor_name', 'amount', 'service_description', 'source',
    ]
    output_dir = os.path.dirname(output_path)
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir)
    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in all_rows:
            writer.writerow(row)


def print_summary(all_rows):
    """Print summary statistics."""
    source_counts = {}
    total_amount = 0.0
    with_name = 0
    with_amount = 0

    for row in all_rows:
        src = row['source']
        source_counts[src] = source_counts.get(src, 0) + 1
        if row['contractor_name']:
            with_name += 1
        amt_str = row['amount']
        if amt_str:
            try:
                amt = float(amt_str.replace(',', ''))
                total_amount += amt
                with_amount += 1
            except ValueError:
                pass

    print("\n=== Combined Contractors Summary ===")
    print("Total rows: %d" % len(all_rows))
    print("\nRows per source:")
    for src in sorted(source_counts.keys()):
        print("  %-45s %6d" % (src, source_counts[src]))
    print("\nWith contractor name: %6d (%.1f%%)" % (with_name, 100.0 * with_name / len(all_rows) if all_rows else 0))
    print("With amount:          %6d (%.1f%%)" % (with_amount, 100.0 * with_amount / len(all_rows) if all_rows else 0))
    print("Total amount: $%s" % format(total_amount, ',.0f'))


# Fee file definitions: (filename, column_prefix, category_label)
FEE_FILES = [
    ('FeesForServicesAccountingGrp.csv', 'FeesForServicesAccountingGrp', 'Accounting'),
    ('FeesForServicesLegalGrp.csv', 'FeesForServicesLegalGrp', 'Legal'),
    ('FeesForServicesLobbyingGrp.csv', 'FeesForServicesLobbyingGrp', 'Lobbying'),
    ('FeesForServicesManagementGrp.csv', 'FeesForServicesManagementGrp', 'Management'),
    ('FeesForServicesOtherGrp.csv', 'FeesForServicesOtherGrp', 'Other professional services'),
    ('FeesForSrvcInvstMgmntFeesGrp.csv', 'FeesForSrvcInvstMgmntFeesGrp', 'Investment management'),
]


def main():
    parser = argparse.ArgumentParser(description='Build unified contractor list from extracted 990 CSVs')
    parser.add_argument('--extracted-dir', default='./data/extracted',
                        help='Directory containing extracted CSVs (default: ./data/extracted)')
    parser.add_argument('--output', default='./data/extracted/combined_contractors.csv',
                        help='Output path for combined CSV (default: ./data/extracted/combined_contractors.csv)')
    args = parser.parse_args()

    if not os.path.isdir(args.extracted_dir):
        print("ERROR: extracted dir not found: %s" % args.extracted_dir)
        sys.exit(1)

    print("Loading payer lookup from scalar_fields.csv...")
    payer_lookup = load_payer_lookup(args.extracted_dir)
    print("  %d payer EIN/period entries loaded" % len(payer_lookup))

    all_rows = []

    # Tier 1: Named contractors
    tier1_sources = [
        ("ContractorCompensationGrp", process_contractor_compensation),
        ("CompensationOfHghstPdCntrctGrp", process_highest_paid_contractor),
        ("ContractorCompExplnGrp", process_contractor_explanation),
    ]
    for name, func in tier1_sources:
        print("Processing %s..." % name)
        rows = func(args.extracted_dir, payer_lookup)
        print("  %d rows" % len(rows))
        all_rows.extend(rows)

    # Tier 2: Fee aggregates
    for filename, prefix, category in FEE_FILES:
        print("Processing %s..." % filename.replace('.csv', ''))
        rows = process_fees_for_services(args.extracted_dir, payer_lookup, filename, prefix, category)
        print("  %d rows" % len(rows))
        all_rows.extend(rows)

    # Tier 3: Schedule R
    print("Processing ScheduleR TransactionsRelatedOrgGrp...")
    rows = process_schedule_r_transactions(args.extracted_dir, payer_lookup)
    print("  %d rows" % len(rows))
    all_rows.extend(rows)

    print("\nWriting %s..." % args.output)
    write_combined(all_rows, args.output)
    print("Done.")

    print_summary(all_rows)


if __name__ == '__main__':
    main()
