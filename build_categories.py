#!/usr/bin/env python3
from __future__ import annotations
"""
IRS 990 Field Category Builder
================================
Uses Claude to classify concordance fields into analyst-friendly semantic
categories. Reads field_lookup.json and optionally field_frequency.json,
then outputs category_mapping.json.

Usage:
  python build_categories.py \
      --concordance ./concordance_output/field_lookup.json \
      --frequency ./concordance_output/field_frequency.json \
      --output ./concordance_output/category_mapping.json

Requires ANTHROPIC_API_KEY environment variable.
"""

import argparse
import json
import os
import sys
from datetime import datetime

try:
    import anthropic
except ImportError:
    sys.exit("Missing dependency: pip install anthropic")


SYSTEM_PROMPT = """\
You are an expert on IRS Form 990 nonprofit tax filings. Your task is to classify \
fields from the 990 concordance into analyst-friendly semantic categories.

You will receive a batch of fields from a specific schedule/form. For each field, \
you get its canonical name, human label, data type, description, full xpath (showing \
where on the IRS form it appears), and population frequency (how often it's filled \
in across real filings).

Classify each field into a category hierarchy with 2-3 levels. Use categories that \
would make sense to a nonprofit analyst or fraud investigator, such as:

- Organization Overview (identification, address, mission, formation)
- Revenue (contributions, program service, investment income, other)
- Expenses (program, management, fundraising, compensation)
- Compensation & Governance (officers, key employees, board, policies)
- Assets & Liabilities (balance sheet, investments, receivables)
- Public Support (public charity tests, support schedules)
- Related Parties (transactions, relationships, controlled entities)
- Activities (program accomplishments, lobbying, political)
- Tax Compliance (unrelated business income, excess benefit, excise tax)
- International (foreign activities, grants, offices)
- Other / Technical (rarely-used fields, IRS processing)

These are suggestions — use your judgment. Adapt depth per branch: some areas \
need 3 levels (e.g., Expenses > Compensation > Officer Detail), others only 2. \
Fields with very low population frequency (<1%) can go into a "Rarely Used" \
subcategory within their domain.

Fields may appear in multiple categories if semantically relevant (e.g., an officer \
compensation field relates to both "Compensation & Governance" and "Expenses").

Respond with valid JSON only. Format:
{
  "classifications": [
    {
      "field": "canonical_field_name",
      "categories": [["Level1", "Level2", "Level3"], ...]
    },
    ...
  ]
}

Include ALL fields from the input. Do not skip any.\
"""


def load_concordance(path: str) -> dict:
    """Load field_lookup.json."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_frequency(path: str) -> dict:
    """Load field_frequency.json, or return empty dict if not available."""
    if not path or not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("fields", {})


def group_fields_by_schedule(fields: dict) -> dict[str, list[str]]:
    """Group canonical field names by their schedule."""
    groups: dict[str, list[str]] = {}
    for name, info in fields.items():
        sched = info.get("schedule", "Unknown")
        if sched not in groups:
            groups[sched] = []
        groups[sched].append(name)
    return groups


def build_field_batch(fields: dict, field_names: list[str],
                      frequency: dict, max_fields: int = 200) -> list[list[dict]]:
    """Build batches of field descriptions for the LLM prompt."""
    batches = []
    current = []
    for name in field_names:
        info = fields[name]
        # Get first xpath as representative
        xpaths = info.get("xpaths", {})
        first_xpath = next(iter(xpaths.values()), "") if xpaths else ""

        freq_info = frequency.get(name, {})
        present_pct = freq_info.get("present_pct", None)
        nontrivial_pct = freq_info.get("nontrivial_pct", None)

        entry = {
            "field": name,
            "label": info.get("label", ""),
            "type": info.get("type", ""),
            "description": info.get("description", ""),
            "xpath": first_xpath,
            "schedule": info.get("schedule", ""),
        }
        if present_pct is not None:
            entry["present_pct"] = present_pct
        if nontrivial_pct is not None:
            entry["nontrivial_pct"] = nontrivial_pct

        current.append(entry)
        if len(current) >= max_fields:
            batches.append(current)
            current = []

    if current:
        batches.append(current)
    return batches


def classify_batch(client: anthropic.Anthropic, batch: list[dict],
                   schedule: str, model: str) -> list[dict]:
    """Send a batch to Claude for classification."""
    user_msg = (
        f"Classify these {len(batch)} fields from schedule/form '{schedule}'.\n\n"
        f"Fields:\n{json.dumps(batch, indent=2)}"
    )

    response = client.messages.create(
        model=model,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )

    # Extract text content
    text = ""
    for block in response.content:
        if block.type == "text":
            text = block.text
            break

    if not text:
        print(f"  WARNING: Empty response for schedule {schedule}")
        return []

    # Parse JSON from response (handle markdown code blocks)
    text = text.strip()
    if text.startswith("```"):
        # Remove ```json ... ``` wrapper
        lines = text.split("\n")
        text = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])
        text = text.strip()

    try:
        result = json.loads(text)
        return result.get("classifications", [])
    except json.JSONDecodeError as e:
        print(f"  WARNING: Failed to parse JSON for schedule {schedule}: {e}")
        print(f"  Response preview: {text[:200]}...")
        return []


def build_category_tree(all_classifications: list[dict]) -> tuple[dict, dict]:
    """Build the nested category tree and field_to_categories mapping."""
    categories = {}
    field_to_categories = {}

    for item in all_classifications:
        field_name = item["field"]
        cat_paths = item.get("categories", [])

        if field_name not in field_to_categories:
            field_to_categories[field_name] = []

        for path in cat_paths:
            if not path:
                continue
            field_to_categories[field_name].append(path)

            # Build nested tree
            node = categories
            for i, level in enumerate(path):
                if level not in node:
                    node[level] = {}
                if i == len(path) - 1:
                    # Leaf category — add fields list
                    if "fields" not in node[level]:
                        node[level]["fields"] = []
                    if field_name not in node[level]["fields"]:
                        node[level]["fields"].append(field_name)
                else:
                    node = node[level]

    return categories, field_to_categories


def main():
    parser = argparse.ArgumentParser(
        description="Use Claude to classify 990 concordance fields into semantic categories.",
    )
    parser.add_argument("--concordance", required=True,
                        help="Path to field_lookup.json")
    parser.add_argument("--frequency",
                        help="Path to field_frequency.json (optional)")
    parser.add_argument("--output", default="./concordance_output/category_mapping.json",
                        help="Output path for category_mapping.json")
    parser.add_argument("--model", default="claude-sonnet-4-6",
                        help="Claude model to use (default: claude-sonnet-4-6)")
    parser.add_argument("--batch-size", type=int, default=150,
                        help="Fields per API call (default: 150)")
    parser.add_argument("--schedules",
                        help="Comma-separated list of schedules to process (default: all)")
    parser.add_argument("--resume",
                        help="Path to partial category_mapping.json to resume from")
    parser.add_argument("--verbose", action="store_true")

    args = parser.parse_args()

    # Validate API key
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ERROR: ANTHROPIC_API_KEY environment variable is required.")

    print("=" * 60)
    print("IRS 990 Field Category Builder")
    print("=" * 60)

    # Load data
    concordance = load_concordance(args.concordance)
    fields = concordance.get("fields", {})
    frequency = load_frequency(args.frequency)

    print(f"\nConcordance: {len(fields)} fields")
    if frequency:
        print(f"Frequency data: {len(frequency)} fields")
    else:
        print("Frequency data: not provided (categories will lack population context)")

    # Group by schedule
    schedule_groups = group_fields_by_schedule(fields)
    print(f"Schedules: {len(schedule_groups)}")

    # Filter schedules if specified
    if args.schedules:
        selected = set(s.strip() for s in args.schedules.split(","))
        schedule_groups = {k: v for k, v in schedule_groups.items() if k in selected}
        print(f"Processing {len(schedule_groups)} selected schedules")

    # Load resume data if provided
    already_classified = set()
    all_classifications = []
    if args.resume and os.path.exists(args.resume):
        with open(args.resume, "r", encoding="utf-8") as f:
            resume_data = json.load(f)
        existing_f2c = resume_data.get("field_to_categories", {})
        for field_name, cat_paths in existing_f2c.items():
            already_classified.add(field_name)
            all_classifications.append({
                "field": field_name,
                "categories": cat_paths,
            })
        print(f"Resuming: {len(already_classified)} fields already classified")

    # Initialize Claude client
    client = anthropic.Anthropic()

    # Process each schedule
    total_fields = sum(len(v) for v in schedule_groups.values())
    processed = len(already_classified)
    api_calls = 0

    for sched_name, field_names in sorted(schedule_groups.items()):
        # Filter out already-classified fields
        remaining = [f for f in field_names if f not in already_classified]
        if not remaining:
            if args.verbose:
                print(f"  {sched_name}: all {len(field_names)} fields already classified, skipping")
            continue

        print(f"\n  {sched_name}: {len(remaining)} fields to classify")

        batches = build_field_batch(fields, remaining, frequency, args.batch_size)

        for bi, batch in enumerate(batches):
            if args.verbose:
                print(f"    Batch {bi+1}/{len(batches)} ({len(batch)} fields)...", end="", flush=True)

            try:
                classifications = classify_batch(client, batch, sched_name, args.model)
                all_classifications.extend(classifications)
                api_calls += 1

                classified_in_batch = len(classifications)
                processed += classified_in_batch

                if args.verbose:
                    print(f" {classified_in_batch} classified")
                else:
                    print(f"    Batch {bi+1}/{len(batches)}: {classified_in_batch} fields classified "
                          f"({processed}/{total_fields} total)")

            except anthropic.APIError as e:
                print(f"\n  ERROR in batch {bi+1} for {sched_name}: {e}")
                print("  Saving partial results...")
                break

        # Save intermediate results after each schedule
        _save_output(all_classifications, args.output, args.model, api_calls, partial=True)

    # Build final output
    print(f"\n{'='*60}")
    print(f"Building category tree...")
    _save_output(all_classifications, args.output, args.model, api_calls, partial=False)

    print(f"\nDone! {processed} fields classified in {api_calls} API calls.")
    print(f"Output: {args.output}")
    print(f"{'='*60}")


def _save_output(all_classifications: list[dict], output_path: str,
                 model: str, api_calls: int, partial: bool = False):
    """Save category mapping to disk."""
    categories, field_to_categories = build_category_tree(all_classifications)

    output = {
        "categories": categories,
        "field_to_categories": field_to_categories,
        "metadata": {
            "generated_by": "build_categories.py",
            "model": model,
            "timestamp": datetime.now().isoformat(),
            "total_fields_classified": len(field_to_categories),
            "api_calls": api_calls,
            "status": "partial" if partial else "complete",
        },
    }

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
