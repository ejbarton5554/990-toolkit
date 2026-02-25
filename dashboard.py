#!/usr/bin/env python3
"""
IRS 990 Concordance Explorer Dashboard
========================================
Streamlit app for exploring the 990 concordance with semantic categories.

Pages:
  1. Category Editor — review/edit LLM-proposed field categories
  2. Schema Browser — navigate categories, see xpaths + frequency

Run:
  streamlit run dashboard.py
"""

import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

import streamlit as st

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
        # experimental version returns dict of lists; flatten to single values
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
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ---------------------------------------------------------------------------
# Data loading (cached)
# ---------------------------------------------------------------------------

@st.cache_data
def load_concordance() -> dict:
    if not os.path.exists(CONCORDANCE_PATH):
        return {}
    with open(CONCORDANCE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


@st.cache_data
def load_frequency() -> dict:
    if not os.path.exists(FREQUENCY_PATH):
        return {}
    with open(FREQUENCY_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("fields", {})


def load_categories() -> dict:
    """Load categories (NOT cached — user may edit)."""
    if not os.path.exists(CATEGORY_PATH):
        return {}
    with open(CATEGORY_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_categories(data: dict):
    """Save category mapping to disk."""
    data["metadata"]["last_modified"] = __import__("datetime").datetime.now().isoformat()
    os.makedirs(os.path.dirname(CATEGORY_PATH) or ".", exist_ok=True)
    with open(CATEGORY_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def count_fields_in_tree(node: dict) -> int:
    """Recursively count fields in a category tree node."""
    count = len(node.get("fields", []))
    for key, val in node.items():
        if key != "fields" and isinstance(val, dict):
            count += count_fields_in_tree(val)
    return count


def get_all_category_paths(tree, prefix=None):
    """Get all leaf category paths from the tree."""
    if prefix is None:
        prefix = []
    paths = []
    for key, val in tree.items():
        if key == "fields":
            continue
        if isinstance(val, dict):
            current = prefix + [key]
            if "fields" in val:
                paths.append(current)
            paths.extend(get_all_category_paths(val, current))
    return paths


def get_node_at_path(tree, path):
    """Navigate to a node in the category tree."""
    node = tree
    for level in path:
        if level not in node or not isinstance(node[level], dict):
            return None
        node = node[level]
    return node


def frequency_badge(pct):
    """Return a colored frequency indicator."""
    if pct is None:
        return "N/A"
    if pct >= 50:
        return f":green[{pct:.0f}%]"
    elif pct >= 10:
        return f":orange[{pct:.0f}%]"
    else:
        return f":red[{pct:.0f}%]"


def frequency_color(pct):
    """Return a hex color for frequency."""
    if pct is None:
        return "#888888"
    if pct >= 50:
        return "#2e7d32"
    elif pct >= 10:
        return "#f57c00"
    else:
        return "#c62828"


def search_fields(fields, query, field_to_cats, frequency):
    """Search across field names, labels, descriptions."""
    query_lower = query.lower()
    results = []
    for name, info in fields.items():
        searchable = " ".join([
            name,
            info.get("label", ""),
            info.get("description", ""),
        ]).lower()
        if query_lower in searchable:
            freq = frequency.get(name, {})
            results.append({
                "name": name,
                "label": info.get("label", ""),
                "type": info.get("type", ""),
                "description": info.get("description", ""),
                "schedule": info.get("schedule", ""),
                "xpath": next(iter(info.get("xpaths", {}).values()), ""),
                "present_pct": freq.get("present_pct"),
                "nontrivial_pct": freq.get("nontrivial_pct"),
                "categories": field_to_cats.get(name, []),
            })
    return results


# ---------------------------------------------------------------------------
# Page: Category Editor
# ---------------------------------------------------------------------------

def page_category_editor():
    st.title("Category Editor")

    cat_data = load_categories()
    if not cat_data:
        st.error(
            f"No category mapping found at `{CATEGORY_PATH}`.\n\n"
            "Run the category builder first:\n\n"
            "```\npython build_categories.py \\\n"
            "    --concordance ./concordance_output/field_lookup.json \\\n"
            "    --frequency ./concordance_output/field_frequency.json \\\n"
            "    --output ./concordance_output/category_mapping.json\n```"
        )
        return

    concordance = load_concordance()
    fields = concordance.get("fields", {})
    frequency = load_frequency()
    categories = cat_data.get("categories", {})
    field_to_cats = cat_data.get("field_to_categories", {})
    metadata = cat_data.get("metadata", {})

    # Sidebar info
    with st.sidebar:
        st.subheader("Category Mapping Info")
        st.write(f"**Fields classified:** {metadata.get('total_fields_classified', '?')}")
        st.write(f"**Model:** {metadata.get('model', '?')}")
        st.write(f"**Generated:** {metadata.get('timestamp', '?')[:16]}")
        st.write(f"**Status:** {metadata.get('status', '?')}")

        st.divider()
        # Search
        search_q = st.text_input("Search fields", key="editor_search",
                                 placeholder="e.g. compensation, revenue...")

    if search_q:
        results = search_fields(fields, search_q, field_to_cats, frequency)
        st.subheader(f"Search results for \"{search_q}\" ({len(results)} found)")
        for r in results[:50]:
            cat_display = " | ".join(" > ".join(p) for p in r["categories"]) if r["categories"] else "Uncategorized"
            with st.expander(f"**{r['label']}** (`{r['name']}`) — {cat_display}"):
                col1, col2 = st.columns([2, 1])
                with col1:
                    st.write(f"**Type:** {r['type']}")
                    st.write(f"**Schedule:** {r['schedule']}")
                    st.write(f"**XPath:** `{r['xpath']}`")
                    st.write(f"**Description:** {r['description']}")
                with col2:
                    st.write(f"**Present:** {frequency_badge(r['present_pct'])}")
                    st.write(f"**Nontrivial:** {frequency_badge(r['nontrivial_pct'])}")
                    st.write(f"**Categories:**")
                    for p in r["categories"]:
                        st.write(f"  - {' > '.join(p)}")
        if len(results) > 50:
            st.info(f"Showing first 50 of {len(results)} results.")
        return

    # Main view: category tree with editing
    st.subheader("Category Tree")

    # Track changes in session state
    if "cat_data" not in st.session_state:
        st.session_state.cat_data = cat_data
        st.session_state.dirty = False

    cat_data = st.session_state.cat_data
    categories = cat_data.get("categories", {})
    field_to_cats = cat_data.get("field_to_categories", {})

    # Top-level actions
    col_save, col_new, col_reclassify = st.columns([1, 1, 1])
    with col_save:
        if st.button("Save Changes", type="primary",
                     disabled=not st.session_state.dirty):
            save_categories(st.session_state.cat_data)
            st.session_state.dirty = False
            st.success("Saved!")
    with col_new:
        new_cat_name = st.text_input("New top-level category", key="new_cat",
                                     placeholder="Category name")
        if new_cat_name and st.button("Create"):
            if new_cat_name not in categories:
                categories[new_cat_name] = {}
                st.session_state.dirty = True
                st.rerun()
            else:
                st.warning("Category already exists.")

    # Render category tree
    for top_name in sorted(categories.keys()):
        top_node = categories[top_name]
        field_count = count_fields_in_tree(top_node)
        with st.expander(f"**{top_name}** ({field_count} fields)", expanded=False):
            # Rename
            col_r, col_d = st.columns([3, 1])
            with col_r:
                new_name = st.text_input("Rename", value=top_name,
                                         key=f"rename_{top_name}")
                if new_name != top_name and st.button("Apply", key=f"apply_rename_{top_name}"):
                    categories[new_name] = categories.pop(top_name)
                    # Update field_to_categories
                    for fname, paths in field_to_cats.items():
                        for p in paths:
                            if p and p[0] == top_name:
                                p[0] = new_name
                    st.session_state.dirty = True
                    _rerun()
            with col_d:
                if field_count == 0:
                    if st.button("Delete", key=f"del_{top_name}",
                                 type="secondary"):
                        del categories[top_name]
                        st.session_state.dirty = True
                        _rerun()

            # Show subcategories
            _render_subcategories(top_node, [top_name], fields, frequency,
                                 categories, field_to_cats)


def _render_subcategories(node, path, fields, frequency, root_categories,
                          field_to_cats, depth=1):
    """Recursively render subcategories."""
    indent = "  " * depth

    # Show fields at this level
    if "fields" in node and node["fields"]:
        st.markdown(f"{indent}**Fields at this level:** {len(node['fields'])}")
        for fname in sorted(node["fields"]):
            finfo = fields.get(fname, {})
            freq = frequency.get(fname, {})
            pct = freq.get("present_pct")
            badge = frequency_badge(pct)
            label = finfo.get("label", fname)
            st.markdown(f"{indent}- {badge} **{label}** (`{fname}`)")

    # Show child categories
    for key in sorted(node.keys()):
        if key == "fields":
            continue
        child = node[key]
        if not isinstance(child, dict):
            continue
        child_count = count_fields_in_tree(child)
        child_path = path + [key]
        path_str = " > ".join(child_path)
        st.markdown(f"{indent}**{key}** ({child_count} fields)")
        _render_subcategories(child, child_path, fields, frequency,
                              root_categories, field_to_cats, depth + 1)


# ---------------------------------------------------------------------------
# Page: Schema Browser
# ---------------------------------------------------------------------------

def page_schema_browser():
    st.title("Schema Browser")

    concordance = load_concordance()
    if not concordance:
        st.error(f"Concordance not found at `{CONCORDANCE_PATH}`")
        return

    fields = concordance.get("fields", {})
    frequency = load_frequency()
    cat_data = load_categories()
    categories = cat_data.get("categories", {}) if cat_data else {}
    field_to_cats = cat_data.get("field_to_categories", {}) if cat_data else {}

    # Sidebar: filters
    with st.sidebar:
        st.subheader("Filters")
        search_q = st.text_input("Search", key="browser_search",
                                 placeholder="Search fields...")

        # Schedule filter
        all_schedules = sorted(set(
            info.get("schedule", "Unknown") for info in fields.values()))
        selected_schedules = st.multiselect("Schedule", all_schedules)

        # Data type filter
        all_types = sorted(set(
            info.get("type", "Unknown") for info in fields.values()))
        selected_types = st.multiselect("Data Type", all_types)

        # Frequency threshold
        if frequency:
            min_freq = st.slider("Min population %", 0, 100, 0,
                                 help="Only show fields present in at least this % of filings")
        else:
            min_freq = 0

        # Version filter
        all_versions = sorted(concordance.get("metadata", {}).get("versions", []))
        selected_version = st.selectbox("Schema version", ["All"] + all_versions)

    # Search mode
    if search_q:
        results = search_fields(fields, search_q, field_to_cats, frequency)

        # Apply filters
        if selected_schedules:
            results = [r for r in results if r["schedule"] in selected_schedules]
        if selected_types:
            results = [r for r in results if r["type"] in selected_types]
        if min_freq > 0:
            results = [r for r in results
                       if (r.get("present_pct") or 0) >= min_freq]

        st.subheader(f"Search: \"{search_q}\" ({len(results)} results)")
        _render_field_table(results, fields, frequency)
        return

    # Category browse mode
    if categories:
        # Navigation via breadcrumb
        if "browse_path" not in st.session_state:
            st.session_state.browse_path = []

        path = st.session_state.browse_path

        # Breadcrumb
        breadcrumb_parts = ["Home"]
        for i, p in enumerate(path):
            breadcrumb_parts.append(p)

        cols = st.columns(len(breadcrumb_parts))
        for i, label in enumerate(breadcrumb_parts):
            with cols[i]:
                if st.button(label, key=f"bc_{i}"):
                    st.session_state.browse_path = path[:i]
                    _rerun()

        # Get current node
        if path:
            node = get_node_at_path(categories, path)
            if node is None:
                st.warning(f"Category path not found: {' > '.join(path)}")
                st.session_state.browse_path = []
                st.rerun()
                return
            st.subheader(" > ".join(path))
        else:
            node = categories
            st.subheader("All Categories")

        # Show child categories as clickable cards
        child_cats = [(k, v) for k, v in node.items()
                      if k != "fields" and isinstance(v, dict)]

        if child_cats:
            num_cols = min(3, len(child_cats))
            for row_start in range(0, len(child_cats), num_cols):
                cols = st.columns(num_cols)
                for idx, (name, child) in enumerate(child_cats[row_start:row_start + num_cols]):
                    count = count_fields_in_tree(child)
                    with cols[idx]:
                        if st.button(f"**{name}**\n{count} fields",
                                     key=f"cat_{name}_{row_start}",
                                     use_container_width=True):
                            st.session_state.browse_path = path + [name]
                            _rerun()

        # Show fields at this level
        field_names = node.get("fields", [])
        if field_names:
            st.divider()
            st.subheader(f"Fields ({len(field_names)})")

            # Build result list for display
            results = []
            for fname in field_names:
                info = fields.get(fname, {})
                freq = frequency.get(fname, {})
                xpaths = info.get("xpaths", {})
                xpath = next(iter(xpaths.values()), "") if xpaths else ""

                # Version filter
                if selected_version and selected_version != "All":
                    if selected_version not in xpaths:
                        continue

                results.append({
                    "name": fname,
                    "label": info.get("label", ""),
                    "type": info.get("type", ""),
                    "description": info.get("description", ""),
                    "schedule": info.get("schedule", ""),
                    "xpath": xpath,
                    "present_pct": freq.get("present_pct"),
                    "nontrivial_pct": freq.get("nontrivial_pct"),
                    "categories": field_to_cats.get(fname, []),
                })

            # Apply filters
            if selected_schedules:
                results = [r for r in results if r["schedule"] in selected_schedules]
            if selected_types:
                results = [r for r in results if r["type"] in selected_types]
            if min_freq > 0:
                results = [r for r in results
                           if (r.get("present_pct") or 0) >= min_freq]

            _render_field_table(results, fields, frequency)

        if not child_cats and not field_names:
            st.info("This category is empty.")

    else:
        # No categories — show flat browse by schedule
        st.info("No category mapping loaded. Showing all fields grouped by schedule.")

        if not selected_schedules:
            # Show schedule overview
            schedule_counts = {}
            for info in fields.values():
                s = info.get("schedule", "Unknown")
                schedule_counts[s] = schedule_counts.get(s, 0) + 1

            st.subheader(f"Schedules ({len(schedule_counts)})")
            for sched, count in sorted(schedule_counts.items(),
                                       key=lambda x: -x[1]):
                st.write(f"- **{sched}**: {count} fields")
        else:
            results = []
            for fname, info in fields.items():
                if info.get("schedule", "") not in selected_schedules:
                    continue
                freq = frequency.get(fname, {})
                xpaths = info.get("xpaths", {})
                xpath = next(iter(xpaths.values()), "") if xpaths else ""
                results.append({
                    "name": fname,
                    "label": info.get("label", ""),
                    "type": info.get("type", ""),
                    "description": info.get("description", ""),
                    "schedule": info.get("schedule", ""),
                    "xpath": xpath,
                    "present_pct": freq.get("present_pct"),
                    "nontrivial_pct": freq.get("nontrivial_pct"),
                    "categories": [],
                })
            if selected_types:
                results = [r for r in results if r["type"] in selected_types]
            if min_freq > 0:
                results = [r for r in results
                           if (r.get("present_pct") or 0) >= min_freq]

            _render_field_table(results, fields, frequency)


def _render_field_table(results, fields, frequency):
    """Render a list of field results as an interactive table."""
    if not results:
        st.info("No fields match the current filters.")
        return

    # Sort by frequency (most common first), then by name
    results.sort(key=lambda r: (-(r.get("present_pct") or 0), r["name"]))

    for r in results[:200]:
        pct = r.get("present_pct")
        nt_pct = r.get("nontrivial_pct")

        # Build compact display
        freq_display = frequency_badge(pct)
        label = r.get("label", r["name"])

        with st.expander(
            f"{freq_display} **{label}** — `{r['xpath']}` — {r['type']}"
        ):
            col1, col2 = st.columns([3, 1])
            with col1:
                st.write(f"**Canonical name:** `{r['name']}`")
                st.write(f"**Schedule:** {r['schedule']}")
                st.write(f"**XPath:** `{r['xpath']}`")
                st.write(f"**Type:** {r['type']}")
                if r.get("description"):
                    st.write(f"**Description:** {r['description']}")
                if r.get("categories"):
                    cats = " | ".join(" > ".join(p) for p in r["categories"])
                    st.write(f"**Categories:** {cats}")

            with col2:
                st.metric("Present in filings", f"{pct:.0f}%" if pct is not None else "N/A")
                st.metric("Nontrivial values", f"{nt_pct:.0f}%" if nt_pct is not None else "N/A")

                # Version coverage
                finfo = fields.get(r["name"], {})
                xpaths = finfo.get("xpaths", {})
                v_start = finfo.get("version_start", "")
                v_end = finfo.get("version_end", "")
                if v_start or v_end:
                    st.write(f"**Versions:** {v_start} - {v_end}")
                st.write(f"**Version count:** {len(xpaths)}")

    if len(results) > 200:
        st.info(f"Showing first 200 of {len(results)} results. Use filters to narrow down.")


# ---------------------------------------------------------------------------
# Page: Field Detail
# ---------------------------------------------------------------------------

def page_field_detail():
    """Show full detail for a single field (accessed via query param)."""
    params = _get_query_params()
    field_name = params.get("field", "")

    if not field_name:
        st.info("No field selected. Use the Schema Browser to find a field.")
        return

    concordance = load_concordance()
    fields = concordance.get("fields", {})
    frequency = load_frequency()
    cat_data = load_categories()
    field_to_cats = cat_data.get("field_to_categories", {}) if cat_data else {}

    if field_name not in fields:
        st.error(f"Field not found: `{field_name}`")
        return

    info = fields[field_name]
    freq = frequency.get(field_name, {})

    st.title(info.get("label", field_name))
    st.caption(f"`{field_name}`")

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Present in filings",
                  f"{freq.get('present_pct', 0):.1f}%",
                  f"{freq.get('present_count', 0)} filings")
    with col2:
        st.metric("Nontrivial values",
                  f"{freq.get('nontrivial_pct', 0):.1f}%",
                  f"{freq.get('nontrivial_count', 0)} filings")
    with col3:
        xpaths = info.get("xpaths", {})
        st.metric("Schema versions", len(xpaths))

    st.divider()

    st.subheader("Metadata")
    st.write(f"**Schedule:** {info.get('schedule', '')}")
    st.write(f"**Type:** {info.get('type', '')}")
    st.write(f"**Raw type:** {info.get('raw_type', '')}")
    st.write(f"**Description:** {info.get('description', '')}")
    st.write(f"**Group:** {info.get('group', '')}")
    st.write(f"**Repeating:** {info.get('repeating', False)}")

    # Categories
    cats = field_to_cats.get(field_name, [])
    if cats:
        st.subheader("Categories")
        for p in cats:
            st.write(f"- {' > '.join(p)}")

    # XPath per version
    st.subheader("XPaths by Version")
    if xpaths:
        # Group identical xpaths
        xpath_versions = {}
        for ver, xp in sorted(xpaths.items()):
            if xp not in xpath_versions:
                xpath_versions[xp] = []
            xpath_versions[xp].append(ver)

        for xp, versions in xpath_versions.items():
            v_range = f"{versions[0]} - {versions[-1]}" if len(versions) > 1 else versions[0]
            st.code(xp, language=None)
            st.caption(f"{len(versions)} versions: {v_range}")


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------

def main():
    # Sidebar navigation
    with st.sidebar:
        st.title("990 Explorer")
        page = st.radio("Navigate", [
            "Schema Browser",
            "Category Editor",
            "Field Detail",
        ])

        st.divider()

        # Quick stats
        concordance = load_concordance()
        if concordance:
            fields = concordance.get("fields", {})
            meta = concordance.get("metadata", {})
            st.caption(f"{meta.get('total_fields', len(fields))} fields")
            st.caption(f"{len(meta.get('versions', []))} schema versions")

    if page == "Category Editor":
        page_category_editor()
    elif page == "Schema Browser":
        page_schema_browser()
    elif page == "Field Detail":
        page_field_detail()


if __name__ == "__main__":
    main()
