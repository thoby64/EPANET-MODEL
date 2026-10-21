"""
Timezone-explicit datetime helpers.

This service stores *naive UTC* datetimes (SQLAlchemy ``DateTime`` columns).
Naive ISO strings without a timezone designator are ambiguous to clients
(JavaScript treats them as local time). Every datetime this service publishes
must carry an explicit UTC designator.
"""

from datetime import datetime, timezone
from typing import Annotated

from fastapi.encoders import ENCODERS_BY_TYPE
from pydantic import PlainSerializer


def utc_isoformat(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def to_naive_utc(value: datetime) -> datetime:
    """Normalise an aware datetime to the naive-UTC storage convention."""
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


UTCDateTime = Annotated[
    datetime,
    PlainSerializer(utc_isoformat, return_type=str, when_used="json"),
]


def install_utc_datetime_serialization() -> None:
    """Cover raw dict responses that bypass Pydantic models."""
    ENCODERS_BY_TYPE[datetime] = utc_isoformat
