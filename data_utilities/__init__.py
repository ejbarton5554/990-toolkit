"""data_utilities — checks, profiling, and quick visualization for data products."""

from data_utilities.initial_checks import (
    read_file,
    detect_structure,
    detect_index,
)
from data_utilities.completeness import (
    completeness_report,
    check_missing_markers,
)
from data_utilities.information import (
    classify_columns,
    column_entropy,
    information_report,
)
from data_utilities.at_a_glance import (
    at_a_glance,
    histogram,
    category_summary,
)
from data_utilities.xml_audit import (
    extract_all_fields,
    extract_all_fields_flat,
    xml_tree_text,
    check_against_extracted,
    audit_report,
)
from data_utilities.query import FieldsDB
