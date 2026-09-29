import pytest

from cnc_warmup.issues import Issue


@pytest.mark.parametrize(
    ("issue", "expected"),
    [
        (
            Issue("machines.M1.max_feed", "must be > 0", source="machines.toml"),
            "machines.toml: machines.M1.max_feed: must be > 0",
        ),
        (Issue("machines.M1.max_feed", "must be > 0"), "machines.M1.max_feed: must be > 0"),
        (Issue("", "file not found", source="machines.toml"), "machines.toml: file not found"),
        (Issue("", "something went wrong"), "something went wrong"),
    ],
)
def test_issue_text_includes_whatever_location_is_known(issue: Issue, expected: str) -> None:
    assert str(issue) == expected
