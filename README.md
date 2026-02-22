# IRS 990 Schema Concordance Builder

Parses IRS 990 `.xsd` schema files across multiple version-years and produces
cross-version concordance files — a "rosetta stone" for navigating the IRS's
changing XML field names over time.

## The problem this solves

The IRS has released 30+ versions of the 990 XML schema since 2009. Between
versions, they renamed many fields — for example, what was
`BaseCompensationFilingOrgAmt` in 2013 became `BsCmpnstnFlngOrgAmt` in 2016.
Same data, different name. If you're analyzing filings across years, you need
to know that these refer to the same thing.

This tool reads the raw `.xsd` schema files, catalogs every field in every
version, automatically matches renamed fields using their descriptions, and
outputs three files.

## Output files

### `concordance.csv` — Machine-readable concordance

One row per canonical field. Key columns:

| Column | Purpose |
|--------|---------|
| `canonical_name` | Stable identifier you can use as a database column name |
| `schedule` | Which form/schedule (IRS990, IRS990ScheduleJ, etc.) |
| `xpath` | XPath in the most recent schema version |
| `human_label` | Plain-English short name |
| `data_type` | What kind of value to expect (dollar amount, yes/no, text...) |
| `description` | Explanation of what this field contains |
| `parent_group` | Repeating group name, if inside one (e.g., officer comp table) |
| `is_repeating` | True if the field appears once per entity in a group |
| `version_start` | Earliest schema version with this field |
| `version_end` | Latest schema version with this field |
| `xpath_<version>` | The actual xpath in each specific version (may differ due to renames) |

### `field_lookup.json` — JSON lookup for code integration

Structured for quick programmatic access:

```python
import json

with open("concordance_output/field_lookup.json") as f:
    concordance = json.load(f)

# Given an xpath from any version's XML, find its canonical identity:
canonical_name = concordance["xpath_index"]["/IRS990ScheduleJ/RltdOrgOfficerTrstKeyEmplGrp/BsCmpnstnFlngOrgAmt"]
field_info = concordance["fields"][canonical_name]

print(field_info["label"])        # "Bs Compensation Filing Organization Amount"
print(field_info["description"])  # "Base compensation from filing organization"
print(field_info["type"])         # "xsd:integer"
print(field_info["xpaths"])       # {"2013v3.0": "/.../BaseCompensationFilingOrgAmt",
                                  #  "2016v3.0": "/.../BsCmpnstnFlngOrgAmt"}
```

The `xpath_index` maps **every xpath from every version** to its canonical
name, so you can feed it any xpath you encounter in any filing and get back
the stable identifier.

### `field_reference.md` — Human-readable reference

A Markdown document organized by schedule and repeating group, explaining
every field in plain language. Useful for browsing, printing, or sharing
with non-technical colleagues. Flags fields whose xpaths changed across
versions.

## Setup

### Requirements

- Python 3.9+
- `lxml` (`pip install lxml`)

### Get the IRS schema files

Download the `.xsd` schema files from the IRS:
https://www.irs.gov/e-file-providers/current-valid-xml-schemas-and-business-rules-for-exempt-organizations-and-other-tax-exempt-entities-modernized-e-file

Organize them by version:

```
schemas/
    2013v3.0/
        IRS990.xsd
        IRS990EZ.xsd
        IRS990PF.xsd
        IRS990ScheduleA.xsd
        ...
    2014v5.0/
        ...
    2016v3.0/
        ...
```
## Betsy added:  Easy to find: '2018v3.0 2018v3.1 2018v3.2 2018v3.3 2019v5.0 2019v5.1 2020v4.0 2020v4.1 2020v4.2 2021v4.0 2021v4.1 2021v4.2 2021v4.3 2022v4.0 2022v4.1 2022v5.0 2024v5.0'
mylist='2018v3.0 2018v3.1 2018v3.2 2018v3.3 2019v5.0 2019v5.1 2020v4.0 2020v4.1 2020v4.2 2021v4.0 2021v4.1 2021v4.2 2021v4.3 2022v4.0 2022v4.1 2022v5.0 2024v5.0'
schedules='A B C D E F H I J K L M N O P Q R'
for m in $mylist; do cd $m; cp "../../UnzipSchemas/$m/TEGE/TEGE990/IRS990/IRS990.xsd" .; cd ..; done
for m in $mylist; do cd $m; cp "../../UnzipSchemas/$m/TEGE/TEGE990EZ/IRS990EZ/IRS990EZ.xsd" .; cd ..; done
for m in $mylist; do cd $m; cp "../../UnzipSchemas/$m/TEGE/TEGE990PF/IRS990PF/IRS990PF.xsd" .; cd ..; done
for m in $mylist; for s in $schedules; do cd $m; cp "../../UnzipSchemas/$m/TEGE/Common/IRS990Schedule$s/IRS990Schedule$s.xsd" .; cd ..; done
for m in $mylist; do for s in $schedules; do cd $m; ls "../../UnzipSchemas/$m/TEGE/Common/IRS990Schedule$s/IRS990Schedule$s.xsd" .; cd ..; done; done
    

### Run

```bash
# Process all versions
python concordance_builder.py --schema-dir ./schemas --output-dir ./concordance_output

# Process one version only
python concordance_builder.py --schema-dir ./schemas --output-dir ./output --single-version 2016v3.0

# Verbose mode (shows per-file element counts)
python concordance_builder.py --schema-dir ./schemas --output-dir ./output --verbose
```

### Test with sample schemas

This repo includes sample schemas for 2013v3.0 and 2016v3.0 that cover
Form 990, Schedule J (compensation), and Schedule R (related organizations).
They're simplified but demonstrate the key features:

```bash
python concordance_builder.py --schema-dir ./sample_schemas --output-dir ./concordance_output --verbose
```

## How cross-version matching works

The tool uses three strategies to link fields across schema versions:

1. **Exact xpath match** — If the full xpath is identical across versions,
   it's the same field. Covers ~90% of fields.

2. **Leaf name match** — If only one field in a given schedule has a
   particular element name, match it across versions even if parent elements
   changed.

3. **Description match** — If two fields in the same schedule and repeating
   group have identical `xs:documentation` text but appear in non-overlapping
   version sets, they're the same field renamed. This catches the IRS's
   abbreviation convention changes (e.g., `BaseCompensationFilingOrgAmt` →
   `BsCmpnstnFlngOrgAmt` — different name, same description: "Base
   compensation from filing organization").

Fields that can't be matched are recorded as version-specific entries.

## Integration with your parsing code

### With IRSx (990-xml-reader)

```python
import json
from irsx.xmlrunner import XMLRunner

with open("field_lookup.json") as f:
    concordance = json.load(f)

runner = XMLRunner()
filing = runner.run_filing(OBJECT_ID)

# Look up any field IRSx returns
for sked in filing.get_result():
    for part_name, part_data in sked["schedule_parts"].items():
        for var_name, value in part_data.items():
            # Build the xpath IRSx used and resolve it
            # via concordance["xpath_index"]
            pass
```

### With the agentic 990 parser

Load the concordance as context so the parser knows what fields exist:

```python
import json

with open("field_lookup.json") as f:
    concordance = json.load(f)

# List all fields for a schedule
schedule = "IRS990ScheduleJ"
fields = {
    name: info for name, info in concordance["fields"].items()
    if info["schedule"] == schedule
}
```

## Limitations

- **Description matching depends on IRS consistency.** If the IRS changes
  both the element name AND the description text, automatic matching won't
  catch it. Review single-version fields in the output for potential manual
  merges.

- **Pre-2013 schemas** had a significantly different structure. Cross-version
  matching between pre-2013 and post-2013 eras will produce more unmatched
  fields.

- **Inline documentation varies.** Some schema elements have detailed
  `xs:documentation` annotations; others have none. Fields without
  documentation can't be matched by description and appear as
  version-specific entries.
# 990-toolkit
