# IRS 990 Nonprofit Fraud Investigation Platform

## Purpose
This project builds tools for investigating nonprofit fraud using IRS 990 XML filings. The pipeline extracts structured data from raw e-file XML, maps it through a schema concordance, and feeds it to an agentic LLM parser that produces standardized analysis with fraud pattern detection.

## Architecture

### 1. Concordance Builder (`concordance_builder.py`)
Parses IRS XSD schema files to build a cross-version field concordance. Maps every xpath across all schema versions to canonical field names with labels
**Input:** `./TEGESchemas/` — 33 or more (variable number) IRS schema versions, each with nested structure:
```
TEGESchemas/2020v4.2/
├── Common/efileTypes.xsd          (type definitions)
└── TEGE/
    ├── Common/IRS990ScheduleA/    (each schedule in its own folder)
    └── TEGE990/IRS990/IRS990.xsd  (main form, deeply nested)
```

**Output:** `./concordance_output/`
- `field_lookup.json` — machine-readable concordance with xpath_index
- `concordance.csv` — flat concordance table
- `field_reference.md` — human-readable reference

**Known XSD patterns that must be handled:**
- `simpleContent/extension` — IRS adds referenceDocumentId attributes to simple values (100+ per form)
- `complexContent/extension` with `base=` — type inheritance chains (IRS990Type extends IRS990BaseType)
- `xs:group ref=` — named group references
- `xs:include` with relative paths like `../../../Common/efileTypes.xsd`
- Named complex types defined in separate files (efileTypes.xsd, form-specific types)
- Dependency schedules and related tax forms (IRS990T, IRS4562, TransfersToControlledEntities, etc.) that appear in 990 filings but aren't named IRS990*

**Run:** `python concordance_builder.py --schema-dir ./TEGESchemas --output-dir ./concordance_output --verbose`

### 2. Concordance Validator (`concordance_validator.py`)
Brute-force extracts every `xs:element[@name]` from schema files and checks which are missing from the concordance. Groups missing elements by XSD pattern to diagnose builder gaps. Distinguishes leaf fields from group containers.

Should be checked with different versions, here's an example:

**Run:** `python concordance_validator.py --schema-dir ./TEGESchemas --concordance ./concordance_output/field_lookup.json --version 2020v4.2`

### 3. Concordance Auditor (`concordance_auditor.py`)
Validates concordance against real XML filings. For each unknown xpath:
- Classifies as "version covered" (real concordance gap) vs "version missing" (possible rename)
- Runs fuzzy matching against concordance (5 strategies: exact leaf, IRS abbreviation expansion, substring/suffix, bigram similarity, structural/unused field matching)
- Filters out container elements whose children ARE in the concordance
- Generates patch files for unknowns seen in 2+ filings

**Run:** `python concordance_auditor.py --concordance ./concordance_output/field_lookup.json --xml-dir ./990_xmls --output-dir ./audit_output --patch --verbose`

### 4. Agentic 990 Parser (`irs990_agent.py`)
LLM-powered parser that uses the concordance to extract structured data from raw 990 XML filings. Uses ConcordanceContext class for:
- Automatic schema version detection from returnVersion attribute
- Schedule identification from XML structure
- Prompt injection with relevant field definitions for the detected version/schedule
- Standardized output format with canonical field names

### 5. Fraud Pattern Analysis
The extracted data feeds into fraud detection patterns including:
- Compensation ratio analysis (officer pay vs program expenses)
- Related party transaction detection
- Revenue/expense trend anomalies
- Network analysis across related organizations (Schedule R)
- Comparison with SEC 10-K forensic analysis patterns (FASB XBRL taxonomy parallels)

## Data Directories
- `./TEGESchemas/` — IRS schema versions (2018v3.0 through 2024v5.0)
- `./990_xmls/` — real 990 XML filings for testing/auditing
- `./concordance_output/` — builder output
- `./audit_output/` — auditor output (audit_report.md, unknown_xpaths.csv, etc.)

## Development Workflow
1. Run builder → generates concordance
2. Run validator → checks schema coverage, reports missing XSD patterns
3. Run auditor against real filings → finds real-world gaps
4. Fix builder to handle new patterns → repeat until coverage maximized
5. Integrate improved concordance into irs990_agent.py

## Current Status
- Builder handles most patterns but may still miss some — run the validator/auditor loop to find gaps
- ~17,000 real filings available for auditing
- Fuzzy matching identifies probable renames across schema versions
- Concordance integrates into the agentic parser via ConcordanceContext

## Key Technical Notes
- IRS schemas use deeply nested directory structures, NOT flat folders
- Element names changed across versions (e.g., BusinessNameLine1 → BusinessNameLine1Txt, City → CityNm)
- `simpleContent/extension` is NOT a complex type with children — it's a leaf value with added attributes
- Form file detection should use actual top-level xs:element presence, not filename matching
- Container xpaths (groups with children) are not data fields — don't flag them as missing
