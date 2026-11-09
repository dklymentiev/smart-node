"""Pure validation tests for ProjectsRepo input."""

from __future__ import annotations

import pytest

from planner.repos.projects import PROJECT_SLUG_RE, _validate_slug, ProjectError


@pytest.mark.parametrize(
    "slug",
    ["planner", "planner-oss", "site-2026", "a", "x" * 64],
)
def test_slug_valid(slug: str) -> None:
    assert PROJECT_SLUG_RE.match(slug)
    _validate_slug(slug)


@pytest.mark.parametrize(
    "slug",
    [
        "",
        "1leading-digit",
        "-leading-dash",
        "Capital",
        "with_underscore",
        "with space",
        "with/slash",
        "x" * 65,
    ],
)
def test_slug_invalid(slug: str) -> None:
    assert not PROJECT_SLUG_RE.match(slug)
    with pytest.raises(ProjectError):
        _validate_slug(slug)
