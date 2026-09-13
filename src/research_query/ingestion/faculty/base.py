"""Institutional source contract."""

from typing import Protocol, runtime_checkable

from research_query.data.models import FacultySourceSnapshot


@runtime_checkable
class FacultySource(Protocol):
    source_name: str

    def fetch_faculty(self) -> FacultySourceSnapshot: ...
