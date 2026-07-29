"""
Tenant fiscal calendar and time-grain truncation (DL-SEM-02).

Franchise finance calendars differ from the Gregorian year, so the fiscal-year start is
tenant configuration rather than a constant. Period-over-period comparison operators are
derived from the calendar so "prior year" means the same thing to every consumer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final

from semantic.semantic_model import TimeComparison, TimeGrain

_MONTHS_PER_QUARTER: Final[int] = 3


@dataclass(frozen=True)
class FiscalCalendar:
    """A tenant's fiscal year definition."""

    fiscal_year_start_month: int = 1
    fiscal_week_start_weekday: int = 0

    def __post_init__(self) -> None:
        if not 1 <= self.fiscal_year_start_month <= 12:
            raise ValueError("fiscal_year_start_month must be between 1 and 12.")
        if not 0 <= self.fiscal_week_start_weekday <= 6:
            raise ValueError("fiscal_week_start_weekday must be between 0 (Monday) and 6.")

    def fiscal_year_of(self, moment: date) -> int:
        """
        The fiscal year a date falls in.

        A fiscal year starting in month M > 1 is labelled by its *ending* calendar year,
        which is the convention finance teams use.
        """
        if self.fiscal_year_start_month == 1:
            return moment.year
        return moment.year + 1 if moment.month >= self.fiscal_year_start_month else moment.year

    def fiscal_year_start(self, moment: date) -> date:
        year = moment.year
        if self.fiscal_year_start_month > 1 and moment.month < self.fiscal_year_start_month:
            year -= 1
        return date(year, self.fiscal_year_start_month, 1)

    def fiscal_period_of(self, moment: date) -> int:
        """1-based fiscal month within the fiscal year."""
        start = self.fiscal_year_start(moment)
        return (moment.year - start.year) * 12 + (moment.month - start.month) + 1

    def fiscal_quarter_of(self, moment: date) -> int:
        return (self.fiscal_period_of(moment) - 1) // _MONTHS_PER_QUARTER + 1

    def truncate(self, moment: date, grain: TimeGrain) -> date:
        """Start of the period containing `moment`, honouring the fiscal calendar."""
        if grain is TimeGrain.DAY:
            return moment
        if grain is TimeGrain.WEEK:
            offset = (moment.weekday() - self.fiscal_week_start_weekday) % 7
            return moment - timedelta(days=offset)
        if grain is TimeGrain.MONTH:
            return date(moment.year, moment.month, 1)
        if grain is TimeGrain.QUARTER:
            quarter_index = (self.fiscal_period_of(moment) - 1) // _MONTHS_PER_QUARTER
            start = self.fiscal_year_start(moment)
            return _add_months(start, quarter_index * _MONTHS_PER_QUARTER)
        return self.fiscal_year_start(moment)

    def period_bounds(self, moment: date, grain: TimeGrain) -> tuple[date, date]:
        """Inclusive start and exclusive end of the period containing `moment`."""
        start = self.truncate(moment, grain)
        if grain is TimeGrain.DAY:
            return start, start + timedelta(days=1)
        if grain is TimeGrain.WEEK:
            return start, start + timedelta(days=7)
        if grain is TimeGrain.MONTH:
            return start, _add_months(start, 1)
        if grain is TimeGrain.QUARTER:
            return start, _add_months(start, _MONTHS_PER_QUARTER)
        return start, _add_months(start, 12)

    def comparison_bounds(
        self, moment: date, grain: TimeGrain, comparison: TimeComparison
    ) -> tuple[date, date]:
        """
        Bounds of the comparison period for a period-over-period metric.

        `PERIOD_TO_DATE` deliberately ends at `moment + 1 day` rather than at the period end,
        so a partial period compares like-for-like against a partial prior period.
        """
        start, end = self.period_bounds(moment, grain)
        if comparison is TimeComparison.NONE:
            return start, end
        if comparison is TimeComparison.PERIOD_TO_DATE:
            return start, moment + timedelta(days=1)
        if comparison is TimeComparison.PRIOR_YEAR:
            return self.period_bounds(_shift_years(moment, -1), grain)
        return self._prior_period_bounds(start, grain)

    def _prior_period_bounds(self, start: date, grain: TimeGrain) -> tuple[date, date]:
        if grain is TimeGrain.DAY:
            previous = start - timedelta(days=1)
        elif grain is TimeGrain.WEEK:
            previous = start - timedelta(days=7)
        elif grain is TimeGrain.MONTH:
            previous = _add_months(start, -1)
        elif grain is TimeGrain.QUARTER:
            previous = _add_months(start, -_MONTHS_PER_QUARTER)
        else:
            previous = _add_months(start, -12)
        return self.period_bounds(previous, grain)


def _add_months(moment: date, months: int) -> date:
    total = moment.month - 1 + months
    year = moment.year + total // 12
    month = total % 12 + 1
    return date(year, month, 1)


def _shift_years(moment: date, years: int) -> date:
    try:
        return moment.replace(year=moment.year + years)
    except ValueError:
        # 29 February shifted into a non-leap year — 28 February is the finance convention.
        return moment.replace(year=moment.year + years, day=28)


# SQL truncation expressions per grain, per dialect. The compiler emits these rather than
# any caller-supplied fragment (OWASP A03).
_TRUNC_EXPRESSIONS: Final[dict[str, dict[TimeGrain, str]]] = {
    "athena": {
        TimeGrain.DAY: "date_trunc('day', {column})",
        TimeGrain.WEEK: "date_trunc('week', {column})",
        TimeGrain.MONTH: "date_trunc('month', {column})",
        TimeGrain.QUARTER: "date_trunc('quarter', {column})",
        TimeGrain.YEAR: "date_trunc('year', {column})",
    },
    "postgresql": {
        TimeGrain.DAY: "date_trunc('day', {column})",
        TimeGrain.WEEK: "date_trunc('week', {column})",
        TimeGrain.MONTH: "date_trunc('month', {column})",
        TimeGrain.QUARTER: "date_trunc('quarter', {column})",
        TimeGrain.YEAR: "date_trunc('year', {column})",
    },
    "redshift": {
        TimeGrain.DAY: "date_trunc('day', {column})",
        TimeGrain.WEEK: "date_trunc('week', {column})",
        TimeGrain.MONTH: "date_trunc('month', {column})",
        TimeGrain.QUARTER: "date_trunc('quarter', {column})",
        TimeGrain.YEAR: "date_trunc('year', {column})",
    },
    "mysql": {
        TimeGrain.DAY: "DATE({column})",
        TimeGrain.WEEK: "DATE_SUB({column}, INTERVAL WEEKDAY({column}) DAY)",
        TimeGrain.MONTH: "DATE_FORMAT({column}, '%Y-%m-01')",
        TimeGrain.QUARTER: "MAKEDATE(YEAR({column}), 1) + INTERVAL QUARTER({column}) QUARTER"
        " - INTERVAL 1 QUARTER",
        TimeGrain.YEAR: "DATE_FORMAT({column}, '%Y-01-01')",
    },
    "sqlserver": {
        TimeGrain.DAY: "CAST({column} AS date)",
        TimeGrain.WEEK: "DATEADD(week, DATEDIFF(week, 0, {column}), 0)",
        TimeGrain.MONTH: "DATEFROMPARTS(YEAR({column}), MONTH({column}), 1)",
        TimeGrain.QUARTER: "DATEADD(quarter, DATEDIFF(quarter, 0, {column}), 0)",
        TimeGrain.YEAR: "DATEFROMPARTS(YEAR({column}), 1, 1)",
    },
}

SUPPORTED_DIALECTS: Final[frozenset[str]] = frozenset(_TRUNC_EXPRESSIONS)


def truncation_sql(column: str, grain: TimeGrain, dialect: str = "athena") -> str:
    """Dialect-specific truncation for a declared grain; isolated per dialect (Builder)."""
    expressions = _TRUNC_EXPRESSIONS.get(dialect)
    if expressions is None:
        raise ValueError(
            f"Dialect {dialect!r} is not supported. Supported: {sorted(SUPPORTED_DIALECTS)}."
        )
    return expressions[grain].format(column=column)
