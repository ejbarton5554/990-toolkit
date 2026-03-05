#!/usr/bin/env python3
"""
IRS 990 Concordance Explorer Dashboard
========================================
Two pages:
  - Schedule Browser: pick a schedule, see fields grouped by category.
  - Field Finder: ask a natural-language question, Claude identifies relevant fields.

Run:
  streamlit run dashboard.py
"""

import csv
import io
import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

import streamlit as st
import pandas as pd
import plotly.express as px

try:
    import anthropic
except ImportError:
    anthropic = None  # handled at page level

# ---------------------------------------------------------------------------
# Compatibility shims for older Streamlit versions
# ---------------------------------------------------------------------------

def _rerun():
    """Compatible rerun for Streamlit < 1.27."""
    if hasattr(st, "rerun"):
        st.rerun()
    else:
        st.experimental_rerun()


def _get_query_params():
    """Compatible query params for Streamlit < 1.30."""
    if hasattr(st, "query_params"):
        return st.query_params
    else:
        params = st.experimental_get_query_params()
        return {k: v[0] if v else "" for k, v in params.items()}


# ---------------------------------------------------------------------------
# Data Explorer — helpers
# ---------------------------------------------------------------------------

def _init_explorer_state():
    """Set default session-state keys for the Data Explorer (de_ prefix)."""
    defaults = {
        "de_csv_path": "",
        "de_df": None,
        "de_state": "columns",
        "de_selected_col": None,
        "de_selected_value": None,
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val


def _reset_explorer_to_columns():
    """Reset navigation state when a new file is loaded."""
    st.session_state["de_state"] = "columns"
    st.session_state["de_selected_col"] = None
    st.session_state["de_selected_value"] = None


@st.cache_data
def _load_csv_cached(path):
    # type: (str) -> Optional[pd.DataFrame]
    """Load a CSV into a DataFrame (cached). Returns None on error."""
    try:
        return pd.read_csv(path, low_memory=False)
    except Exception:
        return None


def _is_numeric_column(series):
    # type: (pd.Series) -> bool
    """Check whether a pandas Series has a numeric dtype."""
    return pd.api.types.is_numeric_dtype(series)


# Patterns that indicate a numeric column is really an identifier, not a measure.
_ID_PATTERNS = re.compile(
    r"(^EIN$|_EIN$|ID$|Id$|_id$|_ID$|Num$|_Num$|Cd$|_Cd$|"
    r"ZIP|Zip|SSN|TIN|DUNS|UEI|NTEE|_Yr$|_yr$|^tax_period$|"
    r"instance_num|_Ind$|_ind$)",
)


def _is_id_column(col_name, series):
    # type: (str, pd.Series) -> bool
    """Heuristic: True if a numeric column is likely an identifier, not a measure."""
    if not _is_numeric_column(series):
        return False
    if _ID_PATTERNS.search(col_name):
        return True
    return False


def _compute_groupby_nontrivial(df, col):
    # type: (pd.DataFrame, str) -> pd.DataFrame
    """For each of the top-20 values in *col*, count nontrivial entries in every other column.

    Returns a DataFrame with columns: _value, _count, plus one column per
    other field (each cell = count of non-null, non-empty values in that group).
    """
    vc = df[col].value_counts(dropna=False).head(20)
    other_cols = [c for c in df.columns if c != col]

    rows = []
    for val, cnt in vc.items():
        if pd.isna(val):
            mask = df[col].isna()
        else:
            mask = df[col] == val
        subset = df.loc[mask, other_cols]
        row = {"_value": val, "_count": cnt}
        for oc in other_cols:
            if _is_numeric_column(subset[oc]):
                row[oc] = int(subset[oc].notna().sum())
            else:
                row[oc] = int(subset[oc].apply(
                    lambda x: pd.notna(x) and str(x).strip() != ""
                ).sum())
        rows.append(row)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONCORDANCE_DIR = "./concordance_output"
CONCORDANCE_PATH = os.path.join(CONCORDANCE_DIR, "field_lookup.json")
CATEGORY_PATH = os.path.join(CONCORDANCE_DIR, "category_mapping.json")
FREQUENCY_PATH = os.path.join(CONCORDANCE_DIR, "field_frequency.json")
EXTRACTED_DIR = "./extracted_output"

st.set_page_config(
    page_title="IRS 990 Concordance Explorer",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ---------------------------------------------------------------------------
# Data loading (cached)
# ---------------------------------------------------------------------------

@st.cache_data
def load_concordance():
    if not os.path.exists(CONCORDANCE_PATH):
        return {}
    with open(CONCORDANCE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


@st.cache_data
def load_frequency():
    if not os.path.exists(FREQUENCY_PATH):
        return {}
    with open(FREQUENCY_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("fields", {})


@st.cache_data
def load_categories():
    if not os.path.exists(CATEGORY_PATH):
        return {}
    with open(CATEGORY_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


@st.cache_data
def load_extracted_orgs():
    """Load org search index from extracted scalar_fields.csv.

    Returns dict keyed by 'EIN|tax_period' with org_name and scalar fields.
    """
    csv_path = os.path.join(EXTRACTED_DIR, "scalar_fields.csv")
    if not os.path.exists(csv_path):
        return {}
    orgs = {}
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ein = row.get("EIN", "")
            tp = row.get("tax_period", "")
            if not ein:
                continue
            key = "{}|{}".format(ein, tp)
            orgs[key] = row
    return orgs


def load_csv_rows(csv_path, ein, tax_period):
    """Load rows from a group CSV matching a specific EIN and tax_period."""
    if not os.path.exists(csv_path):
        return [], []
    rows = []
    headers = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        for row in reader:
            if row.get("EIN") == ein and row.get("tax_period") == tax_period:
                rows.append(row)
    return headers, rows


def shorten_column(col, prefix=""):
    """Strip group/schedule prefix from column name for display."""
    if prefix and col.startswith(prefix + "_"):
        col = col[len(prefix) + 1:]
    # Also strip common nested prefixes
    for p in ("USAddress_", "ForeignAddress_", "BusinessName_"):
        if col.startswith(p):
            col = col[len(p):]
    return col


def _parse_int(val):
    """Safely convert CSV string to int, returning None for empty/non-numeric."""
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def frequency_badge(pct):
    """Return a colored frequency indicator."""
    if pct is None:
        return "N/A"
    if pct >= 50:
        return ":green[{:.0f}%]".format(pct)
    elif pct >= 10:
        return ":orange[{:.0f}%]".format(pct)
    else:
        return ":red[{:.0f}%]".format(pct)


def get_schedule_fields(fields, schedule):
    """Return dict of fields belonging to a schedule."""
    result = {}
    for name, info in fields.items():
        if info.get("schedule", "") == schedule:
            result[name] = info
    return result


def group_by_category(field_names, field_to_cats):
    """Group field names by top-level category > subcategory.

    Returns {"TopCat": {"SubCat": [field_names], ...}, ...}.
    Uncategorized fields go under "Uncategorized".
    """
    groups = {}  # type: Dict[str, Dict[str, List[str]]]
    for fname in field_names:
        paths = field_to_cats.get(fname, [])
        if not paths:
            groups.setdefault("Uncategorized", {}).setdefault(
                "(General)", []
            ).append(fname)
            continue
        for path in paths:
            if len(path) >= 2:
                top = path[0]
                sub = " > ".join(path[1:])
            elif len(path) == 1:
                top = path[0]
                sub = "(General)"
            else:
                top = "Uncategorized"
                sub = "(General)"
            groups.setdefault(top, {}).setdefault(sub, []).append(fname)
    return groups


# ---------------------------------------------------------------------------
# Rendering — Schedule Browser
# ---------------------------------------------------------------------------

def render_schedule_header(schedule, sched_fields, frequency):
    """Render the schedule stats banner."""
    total = len(sched_fields)

    # Collect versions across all fields
    all_versions = set()
    for info in sched_fields.values():
        all_versions.update(info.get("xpaths", {}).keys())

    # Average frequency
    pcts = []
    for name in sched_fields:
        freq = frequency.get(name, {})
        pct = freq.get("present_pct")
        if pct is not None:
            pcts.append(pct)
    avg_freq = sum(pcts) / len(pcts) if pcts else 0

    st.subheader(schedule)
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Fields", "{:,}".format(total))
    with col2:
        st.metric("Schema Versions", len(all_versions))
    with col3:
        st.metric("Avg Frequency", "{:.0f}%".format(avg_freq))


def render_field_row(name, info, freq, field_to_cats, key_suffix=""):
    """Render a compact field row. Checkbox toggles detail panel."""
    pct = freq.get("present_pct")
    badge = frequency_badge(pct)
    label = info.get("label", name)
    ftype = info.get("type", "")

    # Compact summary line
    st.markdown(
        "{badge} **{label}** — `{name}` — {ftype}".format(
            badge=badge, label=label, name=name, ftype=ftype,
        )
    )

    # Toggle detail via checkbox (key_suffix prevents duplicates for multi-category fields)
    widget_key = "detail_{}_{}".format(name, key_suffix) if key_suffix else "detail_{}".format(name)
    if st.checkbox("details", key=widget_key, value=False):
        col1, col2 = st.columns([3, 1])
        with col1:
            desc = info.get("description", "")
            if desc:
                st.write("**Description:** {}".format(desc))

            st.write("**Schedule:** {}".format(info.get("schedule", "")))
            st.write("**Raw type:** {}".format(info.get("raw_type", "")))
            if info.get("group"):
                st.write("**Group:** {}".format(info["group"]))
            if info.get("repeating"):
                st.write("**Repeating:** Yes")

            # XPaths grouped by unique path
            xpaths = info.get("xpaths", {})
            if xpaths:
                xpath_versions = {}  # type: Dict[str, List[str]]
                for ver, xp in sorted(xpaths.items()):
                    xpath_versions.setdefault(xp, []).append(ver)
                for xp, versions in xpath_versions.items():
                    if len(versions) > 1:
                        v_range = "{} .. {}".format(versions[0], versions[-1])
                    else:
                        v_range = versions[0]
                    st.code(xp, language=None)
                    st.caption("{} versions: {}".format(len(versions), v_range))

            # Categories
            cats = field_to_cats.get(name, [])
            if cats:
                cat_strs = [" > ".join(p) for p in cats]
                st.write("**Categories:** {}".format(" | ".join(cat_strs)))

        with col2:
            nt_pct = freq.get("nontrivial_pct")
            st.metric(
                "Present",
                "{:.0f}%".format(pct) if pct is not None else "N/A",
            )
            st.metric(
                "Nontrivial",
                "{:.0f}%".format(nt_pct) if nt_pct is not None else "N/A",
            )
            xpaths = info.get("xpaths", {})
            if xpaths:
                versions = sorted(xpaths.keys())
                st.write("**Versions:** {} .. {}".format(versions[0], versions[-1]))
                st.write("**Version count:** {}".format(len(versions)))


def render_category_group(cat_name, subcats, fields, frequency, field_to_cats):
    """Render a top-level category expander with nested subcategories."""
    # Count total fields in this category (may include duplicates across subcats)
    all_names = set()
    for fnames in subcats.values():
        all_names.update(fnames)
    total = len(all_names)

    with st.expander("**{}** ({} fields)".format(cat_name, total)):
        # Sort subcategories; put (General) first
        sub_names = sorted(subcats.keys(), key=lambda s: (s != "(General)", s))

        for sub_name in sub_names:
            fnames = subcats[sub_name]
            if sub_name and sub_name != "(General)":
                st.markdown("#### {} ({} fields)".format(sub_name, len(fnames)))

            # Sort fields by frequency descending
            sorted_fields = sorted(
                fnames,
                key=lambda n: -(frequency.get(n, {}).get("present_pct") or 0),
            )

            for fname in sorted_fields:
                info = fields.get(fname, {})
                freq = frequency.get(fname, {})
                suffix = "{}_{}".format(cat_name, sub_name).replace(" ", "_")
                render_field_row(fname, info, freq, field_to_cats, key_suffix=suffix)


# ---------------------------------------------------------------------------
# Field Finder — Data preparation
# ---------------------------------------------------------------------------

def build_schedule_summary(all_fields):
    # type: (Dict[str, Any]) -> str
    """Build compact schedule list with field counts for Stage 1."""
    counts = {}  # type: Dict[str, int]
    for info in all_fields.values():
        s = info.get("schedule", "Unknown")
        counts[s] = counts.get(s, 0) + 1
    lines = []
    for sched, cnt in sorted(counts.items(), key=lambda x: -x[1]):
        lines.append("{} ({} fields)".format(sched, cnt))
    return "\n".join(lines)


def build_category_summary(field_to_cats):
    # type: (Dict[str, List[List[str]]]) -> str
    """Build compact category hierarchy with field counts for Stage 1."""
    path_counts = {}  # type: Dict[str, int]
    for paths in field_to_cats.values():
        for path in paths:
            key = " > ".join(path)
            path_counts[key] = path_counts.get(key, 0) + 1
    lines = []
    for path_str, cnt in sorted(path_counts.items()):
        lines.append("{} ({} fields)".format(path_str, cnt))
    return "\n".join(lines)


def filter_fields_by_stage1(all_fields, field_to_cats, schedules, categories):
    # type: (Dict, Dict, List[str], List[str]) -> Dict[str, Any]
    """Union filter: field's schedule matches OR any category path matches."""
    schedule_set = set(schedules)
    result = {}
    for name, info in all_fields.items():
        # Check schedule match
        if info.get("schedule", "") in schedule_set:
            result[name] = info
            continue
        # Check category match
        paths = field_to_cats.get(name, [])
        for path in paths:
            path_str = " > ".join(path)
            for cat in categories:
                if path_str == cat or path_str.startswith(cat + " > ") or cat.startswith(path_str + " > "):
                    result[name] = info
                    break
            else:
                continue
            break
    return result


def build_stage2_field_list(filtered_fields, frequency, field_to_cats):
    # type: (Dict[str, Any], Dict, Dict) -> str
    """Build compact field text for Stage 2 prompt."""
    lines = []
    for name, info in sorted(filtered_fields.items()):
        freq = frequency.get(name, {})
        pct = freq.get("present_pct")
        pct_str = "{:.0f}%".format(pct) if pct is not None else "?"
        cats = field_to_cats.get(name, [])
        cat_str = "; ".join(" > ".join(p) for p in cats) if cats else ""
        line = "{name} | {label} | {ftype} | {sched} | {desc} | freq:{pct} | cats:{cats}".format(
            name=name,
            label=info.get("label", ""),
            ftype=info.get("type", ""),
            sched=info.get("schedule", ""),
            desc=info.get("description", ""),
            pct=pct_str,
            cats=cat_str,
        )
        lines.append(line)
    return "\n".join(lines)


def parse_llm_json(text):
    # type: (str) -> Any
    """Extract JSON from Claude response, handling ```json wrappers."""
    # Try direct parse first
    text = text.strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass

    # Strip markdown code fences
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except (json.JSONDecodeError, ValueError):
            pass

    # Try to find JSON object or array
    for start_char, end_char in [("{", "}"), ("[", "]")]:
        start = text.find(start_char)
        if start == -1:
            continue
        # Find matching end by scanning from the end
        end = text.rfind(end_char)
        if end > start:
            try:
                return json.loads(text[start:end + 1])
            except (json.JSONDecodeError, ValueError):
                pass

    return None


# ---------------------------------------------------------------------------
# Field Finder — Two-stage Claude search
# ---------------------------------------------------------------------------

STAGE1_SYSTEM = """You are an IRS 990 nonprofit tax form expert. You help investigators find relevant data fields in the IRS 990 schema concordance.

Given a user's question, identify which schedules and categories are most likely to contain relevant fields. Be thorough — include any schedule or category that might have even tangentially relevant fields.

Return ONLY valid JSON with this structure:
{"schedules": ["IRS990", "IRS990ScheduleI", ...], "categories": ["Compensation > Officer Pay", "Activities > Grants", ...], "reasoning": "Brief explanation of why these areas are relevant"}

Rules:
- Schedule names must match exactly from the provided list (just the name part, not the count)
- Category paths must match exactly from the provided list (just the path part, not the count)
- Include up to 15 schedules and 20 categories
- Cast a wide net — it's better to include too many than miss relevant ones"""

STAGE2_SYSTEM = """You are an IRS 990 nonprofit tax form expert helping an investigator find specific data fields.

Given the user's question and a list of candidate fields, select the ones that are relevant. For each selected field, explain why it's relevant and assign a priority.

Return ONLY valid JSON as an array:
[{"field_name": "ExactFieldName", "relevance": "Why this field matters for the query", "priority": "high"}, ...]

Rules:
- field_name MUST exactly match a name from the provided field list — do not invent names
- priority must be "high", "medium", or "low"
  - high: directly answers the question
  - medium: provides useful supporting context
  - low: tangentially relevant or may contain related info
- Select at most 75 fields
- If a field tracks attachments or document references (referenceDocumentId, etc.), assign low priority unless specifically asked about"""


def run_stage1(client, query, schedule_summary, category_summary):
    # type: (Any, str, str, str) -> Optional[Dict]
    """Stage 1: identify relevant schedules and categories."""
    user_msg = (
        "Question: {query}\n\n"
        "=== SCHEDULES (pick relevant ones) ===\n"
        "{schedules}\n\n"
        "=== CATEGORIES (pick relevant ones) ===\n"
        "{categories}"
    ).format(query=query, schedules=schedule_summary, categories=category_summary)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        system=STAGE1_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )

    text = ""
    for block in response.content:
        if block.type == "text":
            text = block.text
            break

    result = parse_llm_json(text)
    if result is None:
        return None
    return result


def run_stage2(client, query, field_text, stage1_reasoning):
    # type: (Any, str, str, str) -> Optional[List[Dict]]
    """Stage 2: select specific fields from narrowed set."""
    user_msg = (
        "Question: {query}\n\n"
        "Context from initial analysis: {reasoning}\n\n"
        "=== CANDIDATE FIELDS (one per line: name | label | type | schedule | description | freq | categories) ===\n"
        "{fields}"
    ).format(query=query, reasoning=stage1_reasoning, fields=field_text)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8000,
        system=STAGE2_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )

    text = ""
    for block in response.content:
        if block.type == "text":
            text = block.text
            break

    result = parse_llm_json(text)
    if result is None:
        return None
    if not isinstance(result, list):
        return None
    return result


def run_field_search(query, all_fields, frequency, field_to_cats):
    # type: (str, Dict, Dict, Dict) -> Dict[str, Any]
    """Orchestrate the two-stage field search. Returns result dict."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {"error": "missing_api_key"}

    client = anthropic.Client(api_key=api_key)

    # Build summaries for Stage 1
    schedule_summary = build_schedule_summary(all_fields)
    category_summary = build_category_summary(field_to_cats)

    # --- Stage 1 ---
    stage1 = run_stage1(client, query, schedule_summary, category_summary)
    if stage1 is None:
        return {"error": "stage1_parse_fail"}

    schedules = stage1.get("schedules", [])
    categories = stage1.get("categories", [])
    reasoning = stage1.get("reasoning", "")

    if not schedules and not categories:
        return {"error": "stage1_empty", "reasoning": reasoning}

    # --- Filter fields ---
    filtered = filter_fields_by_stage1(all_fields, field_to_cats, schedules, categories)

    # Overflow protection: if too many fields, reduce
    if len(filtered) > 1500:
        # Keep only fields with frequency > 0
        trimmed = {}
        for name, info in filtered.items():
            freq = frequency.get(name, {})
            pct = freq.get("present_pct")
            if pct is not None and pct > 0:
                trimmed[name] = info
        if trimmed:
            filtered = trimmed

    if len(filtered) > 1500:
        # Fall back to intersection instead of union
        intersected = {}
        schedule_set = set(schedules)
        for name, info in filtered.items():
            if info.get("schedule", "") not in schedule_set:
                continue
            paths = field_to_cats.get(name, [])
            for path in paths:
                path_str = " > ".join(path)
                for cat in categories:
                    if path_str == cat or path_str.startswith(cat + " > ") or cat.startswith(path_str + " > "):
                        intersected[name] = info
                        break
                else:
                    continue
                break
        if intersected:
            filtered = intersected

    # --- Stage 2 ---
    field_text = build_stage2_field_list(filtered, frequency, field_to_cats)

    stage2 = run_stage2(client, query, field_text, reasoning)
    if stage2 is None:
        return {"error": "stage2_parse_fail", "reasoning": reasoning,
                "filtered_count": len(filtered)}

    # Validate field names against concordance
    valid_fields = []
    rejected = []
    for item in stage2:
        fname = item.get("field_name", "")
        if fname in all_fields:
            valid_fields.append(item)
        else:
            rejected.append(fname)

    # Group by priority
    by_priority = {"high": [], "medium": [], "low": []}
    for item in valid_fields:
        p = item.get("priority", "low")
        if p not in by_priority:
            p = "low"
        by_priority[p].append(item)

    # Collect unique schedules in results
    result_schedules = set()
    for item in valid_fields:
        info = all_fields.get(item["field_name"], {})
        result_schedules.add(info.get("schedule", ""))

    return {
        "query": query,
        "fields": valid_fields,
        "by_priority": by_priority,
        "reasoning": reasoning,
        "stage1_schedules": schedules,
        "stage1_categories": categories,
        "result_schedules": sorted(result_schedules),
        "filtered_count": len(filtered),
        "rejected_names": rejected,
    }


# ---------------------------------------------------------------------------
# Field Finder — UI rendering
# ---------------------------------------------------------------------------

def render_finder_results(results, all_fields, frequency, field_to_cats):
    # type: (Dict, Dict, Dict, Dict) -> Set[str]
    """Render priority-grouped results with selection checkboxes. Returns selected field names."""
    fields_list = results.get("fields", [])
    by_priority = results.get("by_priority", {})

    total = len(fields_list)
    n_schedules = len(results.get("result_schedules", []))
    reasoning = results.get("reasoning", "")

    st.markdown("**Found {} fields across {} schedules**".format(total, n_schedules))
    if reasoning:
        st.caption(reasoning)

    rejected = results.get("rejected_names", [])
    if rejected:
        st.caption(":orange[{} hallucinated field names filtered out]".format(len(rejected)))

    # Bulk select buttons
    col1, col2, col3 = st.columns([1, 1, 1])
    with col1:
        if st.button("Select All High Priority"):
            for item in by_priority.get("high", []):
                st.session_state["finder_selected"].add(item["field_name"])
            _rerun()
    with col2:
        if st.button("Select All"):
            for item in fields_list:
                st.session_state["finder_selected"].add(item["field_name"])
            _rerun()
    with col3:
        if st.button("Clear Selection"):
            st.session_state["finder_selected"] = set()
            _rerun()

    selected = st.session_state.get("finder_selected", set())

    # Render each priority group
    seen_keys = set()
    for priority, label_text in [("high", "High Priority"), ("medium", "Medium Priority"), ("low", "Low Priority")]:
        items = by_priority.get(priority, [])
        if not items:
            continue

        st.markdown("### {} ({})".format(label_text, len(items)))

        for item in items:
            fname = item["field_name"]
            info = all_fields.get(fname, {})
            freq = frequency.get(fname, {})
            pct = freq.get("present_pct")
            badge = frequency_badge(pct)
            label = info.get("label", fname)
            sched = info.get("schedule", "")
            ftype = info.get("type", "")
            relevance = item.get("relevance", "")

            is_selected = fname in selected
            cb_key = "finder_cb_{}".format(fname)
            if cb_key in seen_keys:
                continue  # skip duplicate field from LLM results
            seen_keys.add(cb_key)
            checked = st.checkbox(
                "{badge} **{label}** — {sched} — {ftype}".format(
                    badge=badge, label=label, sched=sched, ftype=ftype
                ),
                value=is_selected,
                key=cb_key,
            )
            if checked and fname not in selected:
                selected.add(fname)
            elif not checked and fname in selected:
                selected.discard(fname)

            if relevance:
                st.caption("    {}".format(relevance))

    st.session_state["finder_selected"] = selected
    return selected


def export_selected_fields(selected, results, all_fields, frequency, field_to_cats):
    # type: (Set[str], Dict, Dict, Dict, Dict) -> Tuple[str, str]
    """Build JSON and CSV strings for export."""
    query = results.get("query", "")
    # Build lookup from results for relevance/priority
    result_lookup = {}
    for item in results.get("fields", []):
        result_lookup[item["field_name"]] = item

    export_fields = []
    for fname in sorted(selected):
        info = all_fields.get(fname, {})
        freq = frequency.get(fname, {})
        pct = freq.get("present_pct")
        cats = field_to_cats.get(fname, [])
        cat_strs = [" > ".join(p) for p in cats]
        result_item = result_lookup.get(fname, {})
        xpaths = info.get("xpaths", {})
        sample_xpath = ""
        if xpaths:
            # Pick the most recent version's xpath
            latest_ver = sorted(xpaths.keys())[-1]
            sample_xpath = xpaths[latest_ver]

        export_fields.append({
            "field_name": fname,
            "label": info.get("label", ""),
            "schedule": info.get("schedule", ""),
            "type": info.get("type", ""),
            "description": info.get("description", ""),
            "frequency_pct": pct,
            "priority": result_item.get("priority", ""),
            "relevance": result_item.get("relevance", ""),
            "categories": cat_strs,
            "xpaths": xpaths,
        })

    # JSON export
    json_data = {
        "query": query,
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "fields": export_fields,
    }
    json_str = json.dumps(json_data, indent=2)

    # CSV export
    csv_buf = io.StringIO()
    writer = csv.writer(csv_buf)
    writer.writerow([
        "field_name", "label", "schedule", "type", "description",
        "frequency_pct", "priority", "relevance", "categories", "sample_xpath",
    ])
    for ef in export_fields:
        xpaths = ef.get("xpaths", {})
        sample_xpath = ""
        if xpaths:
            latest_ver = sorted(xpaths.keys())[-1]
            sample_xpath = xpaths[latest_ver]
        writer.writerow([
            ef["field_name"],
            ef["label"],
            ef["schedule"],
            ef["type"],
            ef["description"],
            ef["frequency_pct"] if ef["frequency_pct"] is not None else "",
            ef["priority"],
            ef["relevance"],
            " | ".join(ef.get("categories", [])),
            sample_xpath,
        ])
    csv_str = csv_buf.getvalue()

    return json_str, csv_str


# ---------------------------------------------------------------------------
# Page: Schedule Browser
# ---------------------------------------------------------------------------

def page_schedule_browser(all_fields, metadata, frequency, field_to_cats):
    """Schedule-first browser — the original main page."""
    # Build schedule list with field counts
    schedule_counts = {}  # type: Dict[str, int]
    for info in all_fields.values():
        s = info.get("schedule", "Unknown")
        schedule_counts[s] = schedule_counts.get(s, 0) + 1

    # Sort by field count descending
    sorted_schedules = sorted(schedule_counts.items(), key=lambda x: -x[1])
    schedule_labels = [
        "{} ({} fields)".format(s, c) for s, c in sorted_schedules
    ]
    schedule_names = [s for s, c in sorted_schedules]

    # --- Sidebar: schedule selector ---
    with st.sidebar:
        selected_idx = st.selectbox(
            "Schedule",
            range(len(schedule_labels)),
            format_func=lambda i: schedule_labels[i],
        )
    selected_schedule = schedule_names[selected_idx]

    # Get fields for selected schedule
    sched_fields = get_schedule_fields(all_fields, selected_schedule)

    # --- Sidebar: filters ---
    with st.sidebar:
        st.divider()
        st.subheader("Filters")

        min_freq = 0
        if frequency:
            min_freq = st.slider("Min frequency %", 0, 100, 0)

        all_types = sorted(set(
            info.get("type", "")
            for info in sched_fields.values()
            if info.get("type", "")
        ))
        selected_type = st.selectbox("Data type", ["All"] + all_types)

        all_versions = sorted(metadata.get("versions", []))
        selected_version = st.selectbox("Version", ["All"] + all_versions)

    # --- Main area ---
    render_schedule_header(selected_schedule, sched_fields, frequency)

    # Search bar
    search_q = st.text_input(
        "Search fields within schedule...", key="field_search"
    )

    # Apply filters
    filtered_names = list(sched_fields.keys())

    if min_freq > 0:
        filtered_names = [
            n for n in filtered_names
            if (frequency.get(n, {}).get("present_pct") or 0) >= min_freq
        ]

    if selected_type != "All":
        filtered_names = [
            n for n in filtered_names
            if sched_fields[n].get("type", "") == selected_type
        ]

    if selected_version != "All":
        filtered_names = [
            n for n in filtered_names
            if selected_version in sched_fields[n].get("xpaths", {})
        ]

    if search_q:
        query_lower = search_q.lower()
        filtered_names = [
            n for n in filtered_names
            if query_lower in " ".join([
                n,
                sched_fields[n].get("label", ""),
                sched_fields[n].get("description", ""),
            ]).lower()
        ]

    if len(filtered_names) != len(sched_fields):
        st.caption(
            "Showing {} of {} fields".format(len(filtered_names), len(sched_fields))
        )

    # Group by category and render
    groups = group_by_category(filtered_names, field_to_cats)

    # Sort categories alphabetically; Uncategorized always last
    cat_names = sorted(k for k in groups.keys() if k != "Uncategorized")
    if "Uncategorized" in groups:
        cat_names.append("Uncategorized")

    if not cat_names:
        st.info("No fields match the current filters.")
    else:
        for cat_name in cat_names:
            subcats = groups[cat_name]
            render_category_group(
                cat_name, subcats, all_fields, frequency, field_to_cats
            )


# ---------------------------------------------------------------------------
# Page: Field Finder
# ---------------------------------------------------------------------------

def page_field_finder(all_fields, metadata, frequency, field_to_cats):
    """Natural-language field search powered by Claude."""
    # Check dependencies
    if anthropic is None:
        st.warning(
            "The `anthropic` package is not installed.\n\n"
            "Install it with: `pip install anthropic`"
        )
        return

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        st.warning(
            "**ANTHROPIC_API_KEY not set.**\n\n"
            "Set it before launching the dashboard:\n\n"
            "```bash\n"
            "export ANTHROPIC_API_KEY=sk-ant-...\n"
            "streamlit run dashboard.py\n"
            "```"
        )
        return

    st.header("Field Finder")
    st.caption(
        "Describe what you're looking for in plain English. "
        "Claude will search {:,} fields across the concordance.".format(len(all_fields))
    )

    # Initialize session state
    if "finder_results" not in st.session_state:
        st.session_state["finder_results"] = None
    if "finder_selected" not in st.session_state:
        st.session_state["finder_selected"] = set()

    query = st.text_area(
        "What fields are you looking for?",
        placeholder="e.g., What fields tell me about grants given out and to whom?",
        height=100,
        key="finder_query",
    )

    if st.button("Find Fields", type="primary"):
        if not query or not query.strip():
            st.warning("Please enter a question.")
        else:
            with st.spinner("Stage 1: Identifying relevant schedules and categories..."):
                results = run_field_search(
                    query.strip(), all_fields, frequency, field_to_cats
                )

            # Handle errors
            err = results.get("error")
            if err == "missing_api_key":
                st.error("API key not found. Set ANTHROPIC_API_KEY environment variable.")
                return
            elif err == "stage1_parse_fail":
                st.error("Could not parse Stage 1 response from Claude. Try rephrasing your question.")
                return
            elif err == "stage1_empty":
                reasoning = results.get("reasoning", "")
                st.warning("Claude couldn't identify relevant areas. Try rephrasing your question.")
                if reasoning:
                    st.caption(reasoning)
                return
            elif err == "stage2_parse_fail":
                st.error(
                    "Could not parse Stage 2 response from Claude. "
                    "Searched {} candidate fields.".format(results.get("filtered_count", "?"))
                )
                if results.get("reasoning"):
                    st.caption(results["reasoning"])
                return

            st.session_state["finder_results"] = results
            st.session_state["finder_selected"] = set()
            _rerun()

    # Show results if we have them
    results = st.session_state.get("finder_results")
    if results and not results.get("error"):
        st.divider()
        selected = render_finder_results(results, all_fields, frequency, field_to_cats)

        # Export section
        if selected:
            st.divider()
            st.markdown("**Selected: {} fields**".format(len(selected)))

            json_str, csv_str = export_selected_fields(
                selected, results, all_fields, frequency, field_to_cats
            )

            col1, col2 = st.columns(2)
            with col1:
                st.download_button(
                    label="Download JSON",
                    data=json_str,
                    file_name="field_finder_export.json",
                    mime="application/json",
                )
            with col2:
                st.download_button(
                    label="Download CSV",
                    data=csv_str,
                    file_name="field_finder_export.csv",
                    mime="text/csv",
                )


# ---------------------------------------------------------------------------
# Page: Org Lookup
# ---------------------------------------------------------------------------

# Key scalar fields to show in the org summary, in display order.
# Each tuple: (csv_column, display_label)
_ORG_SUMMARY_FIELDS = [
    ("org_name", "Organization"),
    ("EIN", "EIN"),
    ("tax_period", "Tax Period"),
    ("form_type", "Form Type"),
    ("return_version", "Schema Version"),
    ("CYTotalRevenueAmt", "Total Revenue"),
    ("TotalRevenueAmt", "Total Revenue (alt)"),
    ("TotalAssetsEOYAmt", "Total Assets (EOY)"),
    ("EmployeeCnt", "Employees"),
    ("PrincipalOfficerNm", "Principal Officer"),
    ("USAddress_AddressLine1Txt", "Address"),
    ("USAddress_CityNm", "City"),
    ("USAddress_StateAbbreviationCd", "State"),
    ("USAddress_ZIPCd", "ZIP"),
    ("WebsiteAddressTxt", "Website"),
    ("FormationYr", "Formation Year"),
    ("LegalDomicileStateCd", "Legal Domicile"),
    ("ActivityOrMissionDesc", "Mission"),
]

# Top-level group CSVs to display (filename -> display label)
_TOP_GROUPS = [
    ("Form990PartVIISectionAGrp.csv", "Officers & Key Employees (Part VII)"),
    ("RecipientTable.csv", "Grants to Organizations (Schedule I)"),
    ("ProgramSrvcAccomplishmentGrp.csv", "Program Accomplishments"),
    ("OfficerDirTrstKeyEmplGrp.csv", "Officer/Director/Trustee Compensation"),
    ("OfficerDirTrstKeyEmplInfoGrp.csv", "Officer/Director/Trustee Info"),
    ("RltdOrgOfficerTrstKeyEmplGrp.csv", "Related Org Officer Compensation"),
    ("ContractorCompensationGrp.csv", "Contractor Compensation"),
    ("CompensationOfHghstPdCntrctGrp.csv", "Highest Paid Contractors"),
    ("GrantsPayableGrp.csv", "Grants Payable"),
    ("GrantOrContributionPdDurYrGrp.csv", "Grants Paid During Year"),
    ("GrantOrContriApprvForFutGrp.csv", "Grants Approved for Future"),
    ("GrantsToDomesticOrgsGrp.csv", "Grants to Domestic Orgs"),
    ("GrantsToDomesticIndividualsGrp.csv", "Grants to Domestic Individuals"),
    ("GrantsToOrgOutsideUSGrp.csv", "Grants to Orgs Outside US"),
    ("ForeignIndividualsGrantsGrp.csv", "Foreign Individual Grants"),
    ("GrantsOtherAsstToIndivInUSGrp.csv", "Other Assistance to US Individuals"),
    ("TotalRevenueGrp.csv", "Total Revenue Breakdown"),
    ("TotalFunctionalExpensesGrp.csv", "Functional Expenses"),
    ("TotalAssetsGrp.csv", "Total Assets"),
    ("NetAssetsOrFundBalancesGrp.csv", "Net Assets / Fund Balances"),
    ("ChgInNetAssetsFundBalancesGrp.csv", "Change in Net Assets"),
    ("SumOfTotalLiabilitiesGrp.csv", "Total Liabilities"),
    ("TotLiabNetAssetsFundBalanceGrp.csv", "Liabilities + Net Assets"),
    ("Form990TotalAssetsGrp.csv", "Form 990 Total Assets"),
    ("Form990PFBalanceSheetsGrp.csv", "PF Balance Sheets"),
    ("SavingsAndTempCashInvstGrp.csv", "Savings & Temp Cash"),
    ("NoDonorRestrictionNetAssetsGrp.csv", "Unrestricted Net Assets"),
    ("ApplicationSubmissionInfoGrp.csv", "Application Submission Info"),
    ("SupplementaryInformationGrp.csv", "Supplementary Information"),
    ("ExpenditureResponsibilityGrp.csv", "Expenditure Responsibility"),
    ("ContractorCompExplnGrp.csv", "Contractor Comp Explanation"),
    ("FeesForServicesAccountingGrp.csv", "Fees: Accounting"),
    ("FeesForServicesLegalGrp.csv", "Fees: Legal"),
    ("FeesForServicesLobbyingGrp.csv", "Fees: Lobbying"),
    ("FeesForServicesManagementGrp.csv", "Fees: Management"),
    ("FeesForServicesOtherGrp.csv", "Fees: Other"),
    ("FeesForSrvcInvstMgmntFeesGrp.csv", "Fees: Investment Mgmt"),
]

_SCHEDULE_LABELS = {
    "IRS990ScheduleB": "Schedule B (Contributors)",
    "IRS990ScheduleO": "Schedule O (Supplemental Info)",
    "IRS990ScheduleR": "Schedule R (Related Organizations)",
}


def _render_group_table(csv_path, ein, tax_period, group_prefix=""):
    """Render a group CSV as a table for a specific org. Returns row count."""
    headers, rows = load_csv_rows(csv_path, ein, tax_period)
    if not rows:
        return 0
    # Drop EIN, tax_period, instance_num from display
    skip = {"EIN", "tax_period", "instance_num"}
    display_cols = [h for h in headers if h not in skip]
    # Build display rows with shortened column names
    short_cols = [shorten_column(c, group_prefix) for c in display_cols]
    display_rows = []
    for row in rows:
        display_rows.append({
            short: row.get(orig, "") for short, orig in zip(short_cols, display_cols)
        })
    # Remove columns that are entirely empty
    non_empty = [c for c in short_cols if any(r.get(c, "") for r in display_rows)]
    if not non_empty:
        return 0
    trimmed = [{c: r.get(c, "") for c in non_empty} for r in display_rows]
    st.dataframe(trimmed, use_container_width=True)
    return len(rows)


def page_org_lookup():
    """Search by org name and display all extracted data for a filing."""
    orgs = load_extracted_orgs()
    if not orgs:
        st.warning(
            "No extracted data found at `{}`.\n\n"
            "Run the extractor first to populate this directory.".format(EXTRACTED_DIR)
        )
        return

    st.header("Organization Lookup")
    st.caption("{:,} filings loaded from extracted data.".format(len(orgs)))

    # --- Financial filters (sidebar) ---
    with st.sidebar:
        st.subheader("Financial Filters")
        min_rev = st.number_input("Min Revenue", min_value=0, value=0, step=100000, key="filt_min_rev")
        max_rev = st.number_input("Max Revenue (0 = no cap)", min_value=0, value=0, step=100000, key="filt_max_rev")
        min_assets = st.number_input("Min Assets", min_value=0, value=0, step=100000, key="filt_min_assets")
        max_assets = st.number_input("Max Assets (0 = no cap)", min_value=0, value=0, step=100000, key="filt_max_assets")
        min_grants = st.number_input("Min Grants", min_value=0, value=0, step=100000, key="filt_min_grants")
        max_grants = st.number_input("Max Grants (0 = no cap)", min_value=0, value=0, step=100000, key="filt_max_grants")

    has_financial_filter = any([min_rev, max_rev, min_assets, max_assets, min_grants, max_grants])

    # --- Search ---
    search = st.text_input(
        "Search by organization name",
        placeholder="e.g., Ford Foundation",
        key="org_search",
    )

    has_name = search and len(search) >= 2
    if not has_name and not has_financial_filter:
        st.info("Type at least 2 characters to search, or set financial filters in the sidebar.")
        return

    matches = []
    for key, row in orgs.items():
        # Resolve form-type-aware financial values
        # Revenue: 990/990EZ use CYTotalRevenueAmt/TotalRevenueAmt;
        #          990PF uses AnalysisOfRevenueAndExpenses_TotalRevAndExpnssAmt
        rev_val = (_parse_int(row.get("CYTotalRevenueAmt"))
                   or _parse_int(row.get("TotalRevenueAmt"))
                   or _parse_int(row.get("AnalysisOfRevenueAndExpenses_TotalRevAndExpnssAmt")))
        # Assets: 990/990EZ use TotalAssetsEOYAmt; 990PF uses FMVAssetsEOYAmt
        assets_val = _parse_int(row.get("TotalAssetsEOYAmt")) or _parse_int(row.get("FMVAssetsEOYAmt"))
        # Grants: 990/990EZ use CYContributionsGrantsAmt/TotalContributionsAmt
        grants_val = _parse_int(row.get("CYContributionsGrantsAmt")) or _parse_int(row.get("TotalContributionsAmt"))

        # Financial filters
        if min_rev or max_rev:
            if rev_val is None:
                continue
            if min_rev and rev_val < min_rev:
                continue
            if max_rev and rev_val > max_rev:
                continue
        if min_assets or max_assets:
            if assets_val is None:
                continue
            if min_assets and assets_val < min_assets:
                continue
            if max_assets and assets_val > max_assets:
                continue
        if min_grants or max_grants:
            if grants_val is None:
                continue
            if min_grants and grants_val < min_grants:
                continue
            if max_grants and grants_val > max_grants:
                continue

        # Name filter
        if has_name:
            name = row.get("org_name", "")
            if search.lower() not in name.lower():
                continue

        name = row.get("org_name", "")
        matches.append((key, name, row.get("EIN", ""), row.get("tax_period", ""), rev_val, row.get("form_type", "")))

    if not matches:
        st.warning("No organizations found matching your criteria.")
        return

    # Sort by name, then tax_period descending
    matches.sort(key=lambda m: (m[1].lower(), m[3]), reverse=False)

    # Limit display
    if len(matches) > 200:
        st.warning("Showing first 200 of {:,} matches. Narrow your search.".format(len(matches)))
        matches = matches[:200]

    # --- Select org ---
    def _format_label(m):
        lbl = "{} (EIN: {}, {}".format(m[1], m[2], m[3])
        if m[5]:
            lbl += ", {}".format(m[5])
        if m[4] is not None:
            lbl += ", Rev: ${:,}".format(m[4])
        lbl += ")"
        return lbl

    labels = [_format_label(m) for m in matches]
    selected_idx = st.selectbox(
        "Select organization ({:,} matches)".format(len(matches)),
        range(len(labels)),
        format_func=lambda i: labels[i],
    )
    selected_key = matches[selected_idx][0]
    org_row = orgs[selected_key]
    ein = org_row.get("EIN", "")
    tax_period = org_row.get("tax_period", "")

    st.divider()

    # --- Org summary ---
    st.subheader(org_row.get("org_name", "Unknown"))
    if ein:
        st.markdown(
            "[View on ProPublica](https://projects.propublica.org/nonprofits/organizations/{ein})".format(ein=ein)
        )

    # Show key fields in a compact layout
    summary_items = []
    for col_name, label in _ORG_SUMMARY_FIELDS:
        val = org_row.get(col_name, "")
        if val:
            summary_items.append((label, val))

    if summary_items:
        # Display in two columns
        col1, col2 = st.columns(2)
        half = (len(summary_items) + 1) // 2
        for i, (label, val) in enumerate(summary_items):
            target = col1 if i < half else col2
            with target:
                st.markdown("**{}:** {}".format(label, val))

    # --- Top-level group tables ---
    st.divider()
    st.subheader("Form Data")

    rendered_any = False
    for filename, label in _TOP_GROUPS:
        csv_path = os.path.join(EXTRACTED_DIR, filename)
        if not os.path.exists(csv_path):
            continue
        # Peek for matching rows without full parse — use the load function
        headers, rows = load_csv_rows(csv_path, ein, tax_period)
        if not rows:
            continue
        group_prefix = filename.replace(".csv", "")
        with st.expander("**{}** ({} rows)".format(label, len(rows))):
            _render_group_table(csv_path, ein, tax_period, group_prefix)
        rendered_any = True

    if not rendered_any:
        st.info("No group data found for this filing.")

    # --- Schedule sections ---
    schedule_dirs = []
    if os.path.isdir(EXTRACTED_DIR):
        for entry in sorted(os.listdir(EXTRACTED_DIR)):
            entry_path = os.path.join(EXTRACTED_DIR, entry)
            if os.path.isdir(entry_path):
                schedule_dirs.append((entry, entry_path))

    if schedule_dirs:
        st.divider()
        st.subheader("Schedules")

        for sched_name, sched_path in schedule_dirs:
            sched_label = _SCHEDULE_LABELS.get(sched_name, sched_name)

            # Check if any data exists for this org in this schedule
            sched_scalar_path = os.path.join(sched_path, "scalar_fields.csv")
            has_scalars = False
            sched_scalars = {}
            if os.path.exists(sched_scalar_path):
                _, srows = load_csv_rows(sched_scalar_path, ein, tax_period)
                if srows:
                    has_scalars = True
                    sched_scalars = srows[0]

            # Check group CSVs
            group_csvs = []
            for fname in sorted(os.listdir(sched_path)):
                if fname.endswith(".csv") and fname not in ("scalar_fields.csv", "field_reference.csv"):
                    gpath = os.path.join(sched_path, fname)
                    _, grows = load_csv_rows(gpath, ein, tax_period)
                    if grows:
                        group_csvs.append((fname, gpath, len(grows)))

            if not has_scalars and not group_csvs:
                continue

            with st.expander("**{}**".format(sched_label)):
                # Schedule scalar fields
                if has_scalars:
                    skip = {"EIN", "tax_period", "org_name", "return_version", "form_type"}
                    scalar_items = [
                        (shorten_column(k, sched_name), v)
                        for k, v in sched_scalars.items()
                        if k not in skip and v
                    ]
                    if scalar_items:
                        st.markdown("**Scalar Fields**")
                        for label, val in scalar_items:
                            st.markdown("- **{}:** {}".format(label, val))

                # Schedule group tables
                for fname, gpath, row_count in group_csvs:
                    group_prefix = fname.replace(".csv", "")
                    st.markdown("**{}** ({} rows)".format(
                        group_prefix.replace("Grp", " Group").replace("Detail", " Detail"),
                        row_count,
                    ))
                    _render_group_table(gpath, ein, tax_period, group_prefix)


# ---------------------------------------------------------------------------
# Page: Data Explorer
# ---------------------------------------------------------------------------

def _render_explorer_column_list(df):
    """State 1: show all columns as clickable buttons with dtype and non-null count."""
    st.header("Columns")
    st.caption("{:,} rows x {:,} columns".format(len(df), len(df.columns)))

    search = st.text_input("Filter columns", key="de_col_search")
    cols = list(df.columns)
    if search:
        search_lower = search.lower()
        cols = [c for c in cols if search_lower in c.lower()]
    if not cols:
        st.info("No columns match the filter.")
        return

    for c in cols:
        c1, c2, c3, c4, c5 = st.columns([3, 1, 1, 1, 1])
        non_null = int(df[c].notna().sum())
        pct = 100.0 * non_null / len(df) if len(df) > 0 else 0
        dtype_str = str(df[c].dtype)
        n_unique = int(df[c].nunique(dropna=False))
        with c1:
            if st.button(c, key="de_colbtn_{}".format(c)):
                st.session_state["de_selected_col"] = c
                if _is_numeric_column(df[c]) and not _is_id_column(c, df[c]):
                    st.session_state["de_state"] = "numeric"
                else:
                    st.session_state["de_state"] = "groupby"
                _rerun()
        with c2:
            st.caption(dtype_str)
        with c3:
            st.caption("{:,}".format(non_null))
        with c4:
            st.caption("{:.0f}%".format(pct))
        with c5:
            st.caption("{:,} unique".format(n_unique))


def _render_explorer_groupby(df, col):
    """State 2: top-20 values with counts, nontrivial matrix in expander."""
    if st.button("Back to columns"):
        _reset_explorer_to_columns()
        _rerun()

    st.header("Groupby: {}".format(col))
    st.caption("{:,} rows, {:,} unique values".format(len(df), df[col].nunique(dropna=False)))

    vc = df[col].value_counts(dropna=False).head(20)
    total = len(df)

    st.markdown("**Top {} values**".format(len(vc)))
    for val, cnt in vc.items():
        pct = 100.0 * cnt / total if total > 0 else 0
        if pd.isna(val):
            display_val = "(NaN)"
        else:
            display_val = str(val)
        # Truncate very long values for button label
        btn_label = display_val
        if len(btn_label) > 80:
            btn_label = btn_label[:77] + "..."
        c1, c2, c3 = st.columns([4, 1, 1])
        with c1:
            if st.button(btn_label, key="de_valbtn_{}".format(display_val[:60])):
                st.session_state["de_selected_value"] = val
                st.session_state["de_state"] = "drilldown"
                _rerun()
        with c2:
            st.caption("{:,}".format(cnt))
        with c3:
            st.caption("{:.1f}%".format(pct))

    # Nontrivial count matrix
    with st.expander("Nontrivial count matrix"):
        matrix = _compute_groupby_nontrivial(df, col)
        # Format _value for display
        matrix["_value"] = matrix["_value"].apply(
            lambda x: "(NaN)" if pd.isna(x) else str(x)
        )
        st.dataframe(matrix, use_container_width=True)


def _render_explorer_numeric(df, col):
    """Numeric detail: histogram, summary stats, and range-filtered data view."""
    if st.button("Back to columns", key="de_num_back"):
        _reset_explorer_to_columns()
        _rerun()

    st.header(col)
    valid = df[col].dropna()
    st.caption("{:,} values, {:,} null".format(len(valid), int(df[col].isna().sum())))

    if valid.empty:
        st.warning("Column is entirely empty.")
        return

    # Summary stats
    s1, s2, s3, s4, s5 = st.columns(5)
    with s1:
        st.metric("Mean", "{:,.2f}".format(valid.mean()))
    with s2:
        st.metric("Median", "{:,.2f}".format(valid.median()))
    with s3:
        st.metric("Min", "{:,.2f}".format(valid.min()))
    with s4:
        st.metric("Max", "{:,.2f}".format(valid.max()))
    with s5:
        st.metric("Std Dev", "{:,.2f}".format(valid.std()))

    # Full-column histogram
    fig = px.histogram(valid, x=col, nbins=min(50, len(valid)))
    fig.update_layout(height=350, margin=dict(l=40, r=20, t=30, b=40))
    st.plotly_chart(fig, use_container_width=True)

    # Range filter
    st.subheader("Filter by range")
    col_min = float(valid.min())
    col_max = float(valid.max())
    r1, r2 = st.columns(2)
    with r1:
        range_lo = st.number_input(
            "Min", value=col_min, min_value=col_min, max_value=col_max,
            key="de_num_lo",
        )
    with r2:
        range_hi = st.number_input(
            "Max", value=col_max, min_value=col_min, max_value=col_max,
            key="de_num_hi",
        )

    subset = df[(df[col] >= range_lo) & (df[col] <= range_hi)]
    st.caption("{:,} of {:,} rows in range".format(len(subset), len(df)))

    if subset.empty:
        st.info("No rows in this range.")
        return

    # Histogram of filtered subset (only if range differs from full)
    if range_lo > col_min or range_hi < col_max:
        fig2 = px.histogram(subset, x=col, nbins=min(50, len(subset)))
        fig2.update_layout(height=300, margin=dict(l=40, r=20, t=30, b=40))
        st.plotly_chart(fig2, use_container_width=True)

    # Show filtered rows
    with st.expander("View rows ({:,})".format(len(subset))):
        display_limit = 500
        if len(subset) > display_limit:
            st.caption("Showing first {:,} of {:,} rows".format(display_limit, len(subset)))
        st.dataframe(subset.head(display_limit), use_container_width=True)


def _render_explorer_drilldown(df, col, value):
    """State 3: filter to col=value, show histograms / freq tables per column."""
    if st.button("Back to groupby"):
        st.session_state["de_state"] = "groupby"
        st.session_state["de_selected_value"] = None
        _rerun()

    # Filter
    if pd.isna(value):
        subset = df[df[col].isna()]
        display_val = "(NaN)"
    else:
        subset = df[df[col] == value]
        display_val = str(value)

    st.header("Drill-down: {} = {}".format(col, display_val))
    st.caption("{:,} rows".format(len(subset)))

    if subset.empty:
        st.warning("No rows match this filter.")
        return

    other_cols = [c for c in df.columns if c != col]
    for oc in other_cols:
        # Skip columns that are entirely empty in the subset
        if _is_numeric_column(subset[oc]):
            non_null = subset[oc].notna().sum()
        else:
            non_null = subset[oc].apply(
                lambda x: pd.notna(x) and str(x).strip() != ""
            ).sum()
        if non_null == 0:
            continue

        with st.expander("{} ({:,} values)".format(oc, int(non_null))):
            if _is_numeric_column(subset[oc]):
                # Numeric: histogram + summary stats
                valid = subset[oc].dropna()
                if len(valid) > 0:
                    fig = px.histogram(valid, x=oc, nbins=min(50, len(valid)))
                    fig.update_layout(
                        height=300,
                        margin=dict(l=40, r=20, t=30, b=40),
                    )
                    st.plotly_chart(fig, use_container_width=True)

                    s1, s2, s3, s4 = st.columns(4)
                    with s1:
                        st.metric("Mean", "{:,.2f}".format(valid.mean()))
                    with s2:
                        st.metric("Median", "{:,.2f}".format(valid.median()))
                    with s3:
                        st.metric("Min", "{:,.2f}".format(valid.min()))
                    with s4:
                        st.metric("Max", "{:,.2f}".format(valid.max()))
            else:
                # Categorical: frequency table (top 20)
                vc = subset[oc].value_counts(dropna=False).head(20)
                total_non_null = int(non_null)
                freq_rows = []
                for v, cnt in vc.items():
                    pct = 100.0 * cnt / total_non_null if total_non_null > 0 else 0
                    freq_rows.append({
                        "Value": "(NaN)" if pd.isna(v) else str(v),
                        "Count": cnt,
                        "%": "{:.1f}".format(pct),
                    })
                st.dataframe(freq_rows, use_container_width=True)


def page_data_explorer():
    """Entry point for the Data Explorer page."""
    _init_explorer_state()

    # --- Sidebar: file input ---
    with st.sidebar:
        st.subheader("Load CSV")
        csv_path = st.text_input(
            "File path",
            value=st.session_state.get("de_csv_path", ""),
            key="de_csv_path_input",
            placeholder="./extracted_output/combined_grants.csv",
        )
        uploaded = st.file_uploader("or upload", type=["csv"], key="de_uploader")

        if st.button("Load", key="de_load_btn"):
            if uploaded is not None:
                try:
                    new_df = pd.read_csv(uploaded, low_memory=False)
                    st.session_state["de_df"] = new_df
                    st.session_state["de_csv_path"] = uploaded.name
                    _reset_explorer_to_columns()
                    _rerun()
                except Exception as e:
                    st.error("Failed to read uploaded file: {}".format(e))
            elif csv_path and csv_path.strip():
                path = csv_path.strip()
                if not os.path.exists(path):
                    st.error("File not found: {}".format(path))
                else:
                    new_df = _load_csv_cached(path)
                    if new_df is None:
                        st.error("Failed to parse CSV: {}".format(path))
                    else:
                        st.session_state["de_df"] = new_df
                        st.session_state["de_csv_path"] = path
                        _reset_explorer_to_columns()
                        _rerun()
            else:
                st.warning("Enter a file path or upload a CSV.")

    # --- Main area ---
    st.title("Data Explorer")
    df = st.session_state.get("de_df")
    if df is None:
        st.info("Load a CSV file using the sidebar to get started.")
        return

    state = st.session_state.get("de_state", "columns")

    if state == "drilldown":
        col = st.session_state.get("de_selected_col")
        value = st.session_state.get("de_selected_value")
        if col is not None:
            _render_explorer_drilldown(df, col, value)
        else:
            st.session_state["de_state"] = "columns"
            _rerun()
    elif state == "numeric":
        col = st.session_state.get("de_selected_col")
        if col is not None and col in df.columns:
            _render_explorer_numeric(df, col)
        else:
            st.session_state["de_state"] = "columns"
            _rerun()
    elif state == "groupby":
        col = st.session_state.get("de_selected_col")
        if col is not None and col in df.columns:
            _render_explorer_groupby(df, col)
        else:
            st.session_state["de_state"] = "columns"
            _rerun()
    else:
        _render_explorer_column_list(df)


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------

def main():
    with st.sidebar:
        st.title("990 Explorer")
        page = st.radio(
            "Page",
            ["Schedule Browser", "Field Finder", "Org Lookup", "Data Explorer"],
            label_visibility="collapsed",
        )

    # Data Explorer doesn't need the concordance — dispatch early
    if page == "Data Explorer":
        page_data_explorer()
        return

    # --- Concordance-dependent pages ---
    concordance = load_concordance()
    if not concordance:
        st.error("Concordance not found at `{}`".format(CONCORDANCE_PATH))
        return

    all_fields = concordance.get("fields", {})
    metadata = concordance.get("metadata", {})
    frequency = load_frequency()
    cat_data = load_categories()
    field_to_cats = cat_data.get("field_to_categories", {}) if cat_data else {}

    with st.sidebar:
        st.divider()
        st.subheader("Quick Stats")
        schedule_count = len(set(
            info.get("schedule", "Unknown") for info in all_fields.values()
        ))
        st.write("{:,} fields".format(len(all_fields)))
        st.write("{} schedules".format(schedule_count))
        st.write("{} versions".format(len(metadata.get("versions", []))))

    # --- Dispatch to page ---
    if page == "Schedule Browser":
        page_schedule_browser(all_fields, metadata, frequency, field_to_cats)
    elif page == "Field Finder":
        page_field_finder(all_fields, metadata, frequency, field_to_cats)
    elif page == "Org Lookup":
        page_org_lookup()


if __name__ == "__main__":
    main()
