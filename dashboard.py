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
# Configuration
# ---------------------------------------------------------------------------

CONCORDANCE_DIR = "./concordance_output"
CONCORDANCE_PATH = os.path.join(CONCORDANCE_DIR, "field_lookup.json")
CATEGORY_PATH = os.path.join(CONCORDANCE_DIR, "category_mapping.json")
FREQUENCY_PATH = os.path.join(CONCORDANCE_DIR, "field_frequency.json")

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
# Main app
# ---------------------------------------------------------------------------

def main():
    concordance = load_concordance()
    if not concordance:
        st.error("Concordance not found at `{}`".format(CONCORDANCE_PATH))
        return

    all_fields = concordance.get("fields", {})
    metadata = concordance.get("metadata", {})
    frequency = load_frequency()
    cat_data = load_categories()
    field_to_cats = cat_data.get("field_to_categories", {}) if cat_data else {}

    # --- Sidebar: page selector + quick stats ---
    with st.sidebar:
        st.title("990 Explorer")
        page = st.radio(
            "Page",
            ["Schedule Browser", "Field Finder"],
            label_visibility="collapsed",
        )

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
    else:
        page_field_finder(all_fields, metadata, frequency, field_to_cats)


if __name__ == "__main__":
    main()
