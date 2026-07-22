from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import Mock

from src.tf_module_upgrade import stale_prs


class FakeResponse:
    def __init__(
        self,
        payload: Any,
        *,
        status_code: int = 200,
        links: dict[str, dict[str, str]] | None = None,
        headers: dict[str, str] | None = None,
        url: str = "https://api.github.com/test",
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.links = links or {}
        self.headers = headers or {}
        self.url = url
        self.text = ""
        self.reason = ""
        self.ok = 200 <= status_code < 300

    def json(self) -> Any:
        return self._payload


def test_group_by_actor_sorts_authors_and_oldest_pr_first() -> None:
    pull_requests = [
        stale_prs.StalePullRequest(
            actor="zoe",
            repository="OnScale/repo-b",
            number=2,
            title="Second",
            url="https://github.com/OnScale/repo-b/pull/2",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-06-01T00:00:00Z",
            stale_days=50,
            draft=False,
            assignees=[],
            requested_reviewers=[],
        ),
        stale_prs.StalePullRequest(
            actor="adam",
            repository="OnScale/repo-a",
            number=1,
            title="First",
            url="https://github.com/OnScale/repo-a/pull/1",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-06-20T00:00:00Z",
            stale_days=31,
            draft=False,
            assignees=[],
            requested_reviewers=[],
        ),
        stale_prs.StalePullRequest(
            actor="zoe",
            repository="OnScale/repo-a",
            number=3,
            title="Oldest",
            url="https://github.com/OnScale/repo-a/pull/3",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-05-01T00:00:00Z",
            stale_days=81,
            draft=False,
            assignees=[],
            requested_reviewers=[],
        ),
    ]

    grouped = stale_prs.group_by_actor(pull_requests)

    assert list(grouped) == ["adam", "zoe"]
    assert [pull_request.number for pull_request in grouped["zoe"]] == [3, 2]


def test_list_stale_pull_requests_stops_when_results_become_fresh() -> None:
    response = FakeResponse(
        [
            {
                "number": 10,
                "title": "Stale PR",
                "html_url": "https://github.com/OnScale/repo/pull/10",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-05-01T00:00:00Z",
                "draft": False,
                "user": {"login": "alice"},
                "assignees": [],
                "requested_reviewers": [],
            },
            {
                "number": 11,
                "title": "Fresh PR",
                "html_url": "https://github.com/OnScale/repo/pull/11",
                "created_at": "2026-07-01T00:00:00Z",
                "updated_at": "2026-07-15T00:00:00Z",
                "draft": False,
                "user": {"login": "bob"},
                "assignees": [],
                "requested_reviewers": [],
            },
        ]
    )
    client = Mock(spec=stale_prs.GitHubClient)
    client.get.return_value = response

    result = stale_prs.list_stale_pull_requests(
        client,
        "OnScale/repo",
        cutoff=datetime(2026, 6, 21, tzinfo=timezone.utc),
        now=datetime(2026, 7, 21, tzinfo=timezone.utc),
    )

    assert len(result) == 1
    assert result[0].actor == "alice"
    assert result[0].number == 10
    assert result[0].url.endswith("/pull/10")


def test_markdown_report_groups_by_actor_and_contains_clickable_links(
    tmp_path: Path,
) -> None:
    pull_request = stale_prs.StalePullRequest(
        actor="alice",
        repository="OnScale/repo",
        number=42,
        title="Update module",
        url="https://github.com/OnScale/repo/pull/42",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-06-01T00:00:00Z",
        stale_days=50,
        draft=False,
        assignees=[],
        requested_reviewers=["reviewer"],
    )
    result = stale_prs.ScanResult(
        organization="OnScale",
        generated_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
        stale_threshold_days=30,
        repositories_scanned=1,
        pull_requests=[pull_request],
        errors=[],
    )
    output_path = tmp_path / "stale-prs.md"

    stale_prs.write_markdown_report(output_path, result)
    report = output_path.read_text(encoding="utf-8")

    assert "## alice (1)" in report
    assert "[#42](https://github.com/OnScale/repo/pull/42)" in report
    assert "[Update module](https://github.com/OnScale/repo/pull/42)" in report


def test_list_organization_repositories_filters_archived_and_forks() -> None:
    client = Mock(spec=stale_prs.GitHubClient)
    client.iter_items.return_value = iter(
        [
            {"full_name": "OnScale/active", "archived": False, "fork": False},
            {"full_name": "OnScale/archive", "archived": True, "fork": False},
            {"full_name": "OnScale/fork", "archived": False, "fork": True},
        ]
    )

    repositories = stale_prs.list_organization_repositories(client, "OnScale")

    assert [repository["full_name"] for repository in repositories] == [
        "OnScale/active"
    ]
