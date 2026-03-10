"""Query helpers for the all_fields SQLite database.

Wraps common queries so you never need to write raw SQL.

Usage:
    from data_utilities.query import FieldsDB

    db = FieldsDB("data/extracted/all_fields.db")
    df = db.org("123456789")                        # all fields for an EIN
    df = db.schedule("IRS990ScheduleI")              # all fields in a schedule
    df = db.field("/IRS990/TotalRevenueGrp/TotalRevenueColumnAmt")  # one xpath across all orgs
    df = db.search("GrantAmt")                       # xpaths containing a substring
    df = db.orgs()                                   # filing-level metadata
"""
import os
import sqlite3
from typing import List, Optional

import pandas as pd


DEFAULT_DB = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "data", "extracted", "all_fields.db",
)


# All queries go through the fields_view which joins filings + xpaths + fields
_VIEW = "fields_view"


class FieldsDB(object):
    """Convenience wrapper around the all_fields SQLite database."""

    def __init__(self, db_path=None):
        # type: (Optional[str]) -> None
        self.db_path = db_path or DEFAULT_DB
        if not os.path.isfile(self.db_path):
            raise FileNotFoundError("Database not found: %s" % self.db_path)
        self.conn = sqlite3.connect(self.db_path)

    def close(self):
        # type: () -> None
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ------------------------------------------------------------------
    # Filing-level queries
    # ------------------------------------------------------------------

    def orgs(self, form_type=None):
        # type: (Optional[str]) -> pd.DataFrame
        """All filings. Optionally filter by form_type ('990', '990EZ', etc.)."""
        if form_type:
            return pd.read_sql(
                "SELECT * FROM filings WHERE form_type = ?",
                self.conn, params=(form_type,),
            )
        return pd.read_sql("SELECT * FROM filings", self.conn)

    def org(self, ein):
        # type: (str) -> pd.DataFrame
        """All fields for a given EIN (all tax periods)."""
        return pd.read_sql(
            "SELECT ein, tax_period, org_name, form_type, "
            "       xpath, schedule, field_path, value "
            "FROM %s WHERE ein = ?" % _VIEW,
            self.conn, params=(ein,),
        )

    def filing(self, ein, tax_period):
        # type: (str, str) -> pd.DataFrame
        """All fields for a specific filing (EIN + tax period)."""
        return pd.read_sql(
            "SELECT ein, tax_period, org_name, form_type, "
            "       xpath, schedule, field_path, value "
            "FROM %s WHERE ein = ? AND tax_period = ?" % _VIEW,
            self.conn, params=(ein, tax_period),
        )

    # ------------------------------------------------------------------
    # Field-level queries
    # ------------------------------------------------------------------

    def field(self, xpath):
        # type: (str) -> pd.DataFrame
        """One specific xpath across all filings."""
        return pd.read_sql(
            "SELECT f.ein, f.tax_period, f.org_name, d.value "
            "FROM fields d "
            "JOIN filings f ON d.filing_id = f.filing_id "
            "JOIN xpaths x ON d.xpath_id = x.xpath_id "
            "WHERE x.xpath = ?",
            self.conn, params=(xpath,),
        )

    def schedule(self, schedule_name, limit=None):
        # type: (str, Optional[int]) -> pd.DataFrame
        """All fields for a schedule (e.g., 'IRS990ScheduleI')."""
        sql = (
            "SELECT ein, tax_period, org_name, xpath, field_path, value "
            "FROM %s WHERE schedule = ?" % _VIEW
        )
        if limit:
            sql += " LIMIT %d" % limit
        return pd.read_sql(sql, self.conn, params=(schedule_name,))

    def search(self, pattern, limit=500):
        # type: (str, int) -> pd.DataFrame
        """Find fields whose xpath contains a substring (case-insensitive)."""
        return pd.read_sql(
            "SELECT x.xpath, x.schedule, COUNT(*) as filing_count "
            "FROM fields d "
            "JOIN xpaths x ON d.xpath_id = x.xpath_id "
            "WHERE x.xpath LIKE ? "
            "GROUP BY x.xpath_id "
            "ORDER BY filing_count DESC "
            "LIMIT ?",
            self.conn, params=("%%%s%%" % pattern, limit),
        )

    def field_values(self, xpath, limit=100):
        # type: (str, int) -> pd.DataFrame
        """Top values for a given xpath with counts."""
        return pd.read_sql(
            "SELECT d.value, COUNT(*) as count "
            "FROM fields d "
            "JOIN xpaths x ON d.xpath_id = x.xpath_id "
            "WHERE x.xpath = ? "
            "GROUP BY d.value "
            "ORDER BY count DESC "
            "LIMIT ?",
            self.conn, params=(xpath, limit),
        )

    # ------------------------------------------------------------------
    # Schedule listing
    # ------------------------------------------------------------------

    def schedules(self):
        # type: () -> pd.DataFrame
        """List all schedules with field counts."""
        return pd.read_sql(
            "SELECT x.schedule, "
            "       COUNT(DISTINCT x.xpath_id) as unique_fields, "
            "       COUNT(*) as total_values, "
            "       COUNT(DISTINCT d.filing_id) as filing_count "
            "FROM fields d "
            "JOIN xpaths x ON d.xpath_id = x.xpath_id "
            "GROUP BY x.schedule "
            "ORDER BY filing_count DESC",
            self.conn,
        )

    # ------------------------------------------------------------------
    # Raw SQL escape hatch
    # ------------------------------------------------------------------

    def query(self, sql, params=None):
        # type: (str, Optional[tuple]) -> pd.DataFrame
        """Run arbitrary SQL and return a DataFrame."""
        return pd.read_sql(sql, self.conn, params=params)

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self):
        # type: () -> dict
        """Database summary statistics."""
        filing_count = self.conn.execute(
            "SELECT COUNT(*) FROM filings"
        ).fetchone()[0]
        field_count = self.conn.execute(
            "SELECT COUNT(*) FROM fields"
        ).fetchone()[0]
        unique_xpaths = self.conn.execute(
            "SELECT COUNT(*) FROM xpaths"
        ).fetchone()[0]
        unique_eins = self.conn.execute(
            "SELECT COUNT(DISTINCT ein) FROM filings"
        ).fetchone()[0]
        db_size = os.path.getsize(self.db_path) / (1024 * 1024.0)
        return {
            "filings": filing_count,
            "fields": field_count,
            "unique_xpaths": unique_xpaths,
            "unique_eins": unique_eins,
            "db_size_mb": round(db_size, 1),
        }
