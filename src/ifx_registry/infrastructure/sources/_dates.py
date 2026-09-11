"""Date parsing helpers shared by source infrastructure."""

from datetime import date, datetime
from email.utils import parsedate_to_datetime

from ifx_registry.domain.errors import SourceValidationError


def parse_http_date(value: str, *, field_name: str) -> date:
    """Parse an RFC-compliant HTTP date into a calendar date."""
    try:
        return parsedate_to_datetime(value).date()
    except (TypeError, ValueError) as error:
        raise SourceValidationError(f"Invalid {field_name} date {value!r}") from error


def parse_flexible_date(value: str, *, field_name: str) -> date:
    """Parse ISO or human-readable release dates used by upstream APIs."""
    text = value.strip()
    for date_format in ("%d-%B-%Y", "%d-%B-%y", "%d-%b-%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            continue
    try:
        return date.fromisoformat(text[:10])
    except ValueError as error:
        raise SourceValidationError(f"Invalid {field_name} date {value!r}") from error
