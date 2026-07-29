import csv

from datetime import datetime, timezone
from unittest.mock import call, patch

import pytest
import requests

from src.tf_module_upgrade import stale_prs


def test_parse_github_datetime():
    result = stale_prs.parse_github_datetime(
        "2026-07-01T10:30:00Z"
    )

    assert result == datetime(
        2026,
        7,
        1,
        10,
        30,
        tzinfo=timezone.utc,
    )


def test_get_devops_controlled_repositories():
    with patch(
        "src.tf_module_upgrade."
        "stale_prs.hcl.get_modules",
        return_value=[
            "onscale-terraform-vpc",
            "onscale-terraform-account",
            "",
        ],
    ):
        with patch(
            "src.tf_module_upgrade."
            "stale_prs.hcl.get_root_configurations",
            return_value=[
                "onscale-terraform-awsansys1",
                "onscale-terraform-vpc",
            ],
        ):
            with patch(
                "src.tf_module_upgrade."
                "stale_prs.hcl.get_sre_applications",
                return_value=[
                    "onscale-sre-application-blueprint",
                ],
            ):
                repositories = (
                    stale_prs
                    .get_devops_controlled_repositories()
                )

    assert repositories == [
        "onscale-sre-application-blueprint",
        "onscale-terraform-account",
        "onscale-terraform-awsansys1",
        "onscale-terraform-vpc",
    ]


def test_get_stale_pull_requests_excludes_drafts():
    pull_requests = [
        {
            "number": 10,
            "title": "Old pull request",
            "html_url": (
                "https://github.com/OnScale/"
                "onscale-terraform-vpc/pull/10"
            ),
            "created_at": (
                "2026-04-01T10:00:00Z"
            ),
            "updated_at": (
                "2026-05-01T10:00:00Z"
            ),
            "draft": False,
            "user": {
                "login": "github-user-one",
            },
        },
        {
            "number": 11,
            "title": "Old draft pull request",
            "html_url": (
                "https://github.com/OnScale/"
                "onscale-terraform-vpc/pull/11"
            ),
            "created_at": (
                "2026-04-02T10:00:00Z"
            ),
            "updated_at": (
                "2026-05-02T10:00:00Z"
            ),
            "draft": True,
            "user": {
                "login": "github-user-two",
            },
        },
        {
            "number": 12,
            "title": "Recent pull request",
            "html_url": (
                "https://github.com/OnScale/"
                "onscale-terraform-vpc/pull/12"
            ),
            "created_at": (
                "2026-07-01T10:00:00Z"
            ),
            "updated_at": (
                "2026-07-20T10:00:00Z"
            ),
            "draft": False,
            "user": {
                "login": "github-user-three",
            },
        },
    ]

    current_time = datetime(
        2026,
        7,
        23,
        10,
        0,
        tzinfo=timezone.utc,
    )

    with patch(
        "src.tf_module_upgrade."
        "stale_prs.github.get_paginated_items",
        return_value=pull_requests,
    ) as mocked_get_paginated_items:
        results = (
            stale_prs.get_stale_pull_requests(
                "onscale-terraform-vpc",
                30,
                current_time,
            )
        )

    assert len(results) == 1
    assert results[0]["actor"] == (
        "github-user-one"
    )
    assert results[0]["repository"] == (
        "OnScale/onscale-terraform-vpc"
    )
    assert results[0][
        "pull_request_number"
    ] == 10
    assert results[0]["stale_days"] == 83
    assert results[0]["url"].endswith(
        "/pull/10"
    )
    assert "draft" not in results[0]

    mocked_get_paginated_items.assert_called_once_with(
        (
            "https://api.github.com/repos/"
            "OnScale/"
            "onscale-terraform-vpc/pulls"
        ),
        params={
            "state": "open",
            "sort": "updated",
            "direction": "asc",
            "per_page": 100,
        },
    )


def test_get_stale_pull_requests_uses_unknown_actor():
    current_time = datetime(
        2026,
        7,
        23,
        10,
        0,
        tzinfo=timezone.utc,
    )

    pull_requests = [
        {
            "number": 10,
            "title": "Old pull request",
            "html_url": (
                "https://github.com/OnScale/"
                "onscale-terraform-vpc/pull/10"
            ),
            "created_at": (
                "2026-04-01T10:00:00Z"
            ),
            "updated_at": (
                "2026-05-01T10:00:00Z"
            ),
            "draft": False,
            "user": None,
        }
    ]

    with patch(
        "src.tf_module_upgrade."
        "stale_prs.github.get_paginated_items",
        return_value=pull_requests,
    ):
        results = (
            stale_prs.get_stale_pull_requests(
                "onscale-terraform-vpc",
                30,
                current_time,
            )
        )

    assert results[0]["actor"] == "unknown"


def test_scan_repositories_uses_only_hcl_results():
    current_time = datetime(
        2026,
        7,
        23,
        10,
        0,
        tzinfo=timezone.utc,
    )

    repositories = [
        "onscale-terraform-account",
        "onscale-terraform-vpc",
    ]

    with patch(
        "src.tf_module_upgrade.stale_prs."
        "get_devops_controlled_repositories",
        return_value=repositories,
    ):
        with patch(
            "src.tf_module_upgrade.stale_prs."
            "get_stale_pull_requests",
            return_value=[],
        ) as mocked_get_stale_pull_requests:
            result = (
                stale_prs.scan_repositories(
                    stale_days=30,
                    current_time=current_time,
                )
            )

    assert result[
        "repositories_scanned"
    ] == 2
    assert result["organization"] == "OnScale"
    assert result["pull_requests"] == []
    assert result["errors"] == []

    assert (
        mocked_get_stale_pull_requests.call_args_list
        == [
            call(
                "onscale-terraform-account",
                30,
                current_time,
            ),
            call(
                "onscale-terraform-vpc",
                30,
                current_time,
            ),
        ]
    )


def test_scan_repositories_records_repository_error():
    current_time = datetime(
        2026,
        7,
        23,
        10,
        0,
        tzinfo=timezone.utc,
    )

    with patch(
        "src.tf_module_upgrade.stale_prs."
        "get_devops_controlled_repositories",
        return_value=[
            "onscale-terraform-vpc"
        ],
    ):
        with patch(
            "src.tf_module_upgrade.stale_prs."
            "get_stale_pull_requests",
            side_effect=requests.RequestException(
                "Request failed"
            ),
        ):
            result = stale_prs.scan_repositories(
                stale_days=30,
                current_time=current_time,
            )

    assert result["pull_requests"] == []
    assert result["errors"] == [
        {
            "repository": (
                "OnScale/onscale-terraform-vpc"
            ),
            "error": "Request failed",
        }
    ]


def test_scan_repositories_rejects_negative_stale_days():
    with pytest.raises(
        ValueError,
        match=(
            "stale_days must be zero or greater"
        ),
    ):
        stale_prs.scan_repositories(
            stale_days=-1
        )


def test_group_by_actor():
    pull_requests = [
        {
            "actor": "bob",
            "repository": (
                "OnScale/onscale-terraform-vpc"
            ),
            "pull_request_number": 11,
            "title": "Bob pull request",
            "url": (
                "https://github.com/OnScale/"
                "onscale-terraform-vpc/pull/11"
            ),
            "created_at": (
                "2026-04-01T10:00:00Z"
            ),
            "updated_at": (
                "2026-05-01T10:00:00Z"
            ),
            "stale_days": 83,
        },
        {
            "actor": "alice",
            "repository": (
                "OnScale/onscale-terraform-account"
            ),
            "pull_request_number": 20,
            "title": "Alice pull request",
            "url": (
                "https://github.com/OnScale/"
                "onscale-terraform-account/pull/20"
            ),
            "created_at": (
                "2026-03-01T10:00:00Z"
            ),
            "updated_at": (
                "2026-04-01T10:00:00Z"
            ),
            "stale_days": 113,
        },
        {
            "actor": "alice",
            "repository": (
                "OnScale/onscale-terraform-vpc"
            ),
            "pull_request_number": 21,
            "title": "Second Alice pull request",
            "url": (
                "https://github.com/OnScale/"
                "onscale-terraform-vpc/pull/21"
            ),
            "created_at": (
                "2026-04-01T10:00:00Z"
            ),
            "updated_at": (
                "2026-05-15T10:00:00Z"
            ),
            "stale_days": 69,
        },
    ]

    grouped = stale_prs.group_by_actor(
        pull_requests
    )

    assert list(grouped.keys()) == [
        "alice",
        "bob",
    ]
    assert [
        pull_request["pull_request_number"]
        for pull_request in grouped["alice"]
    ] == [
        20,
        21,
    ]


def test_write_csv_report_groups_by_actor(
    tmp_path,
):
    output = tmp_path / "stale-prs.csv"

    result = {
        "organization": "OnScale",
        "generated_at": (
            "2026-07-23T10:00:00+00:00"
        ),
        "stale_threshold_days": 30,
        "repositories_scanned": 2,
        "pull_requests": [
            {
                "actor": "bob",
                "repository": (
                    "OnScale/onscale-terraform-vpc"
                ),
                "pull_request_number": 11,
                "title": "Bob pull request",
                "url": (
                    "https://github.com/OnScale/"
                    "onscale-terraform-vpc/"
                    "pull/11"
                ),
                "created_at": (
                    "2026-04-01T10:00:00Z"
                ),
                "updated_at": (
                    "2026-05-01T10:00:00Z"
                ),
                "stale_days": 83,
            },
            {
                "actor": "alice",
                "repository": (
                    "OnScale/"
                    "onscale-terraform-account"
                ),
                "pull_request_number": 20,
                "title": "Alice pull request",
                "url": (
                    "https://github.com/OnScale/"
                    "onscale-terraform-account/"
                    "pull/20"
                ),
                "created_at": (
                    "2026-03-01T10:00:00Z"
                ),
                "updated_at": (
                    "2026-04-01T10:00:00Z"
                ),
                "stale_days": 113,
            },
        ],
        "errors": [],
    }

    output_path = stale_prs.write_csv_report(
        result,
        str(output),
    )

    assert output_path == output.resolve()

    with output.open(
        encoding="utf-8",
    ) as report_file:
        rows = list(
            csv.DictReader(report_file)
        )

    assert [
        row["actor"]
        for row in rows
    ] == [
        "alice",
        "bob",
    ]

    assert rows[0]["url"] == (
        "https://github.com/OnScale/"
        "onscale-terraform-account/"
        "pull/20"
    )

    assert "draft" not in rows[0]


def test_write_csv_report_requires_csv_extension(
    tmp_path,
):
    result = {
        "organization": "OnScale",
        "generated_at": (
            "2026-07-23T10:00:00+00:00"
        ),
        "stale_threshold_days": 30,
        "repositories_scanned": 0,
        "pull_requests": [],
        "errors": [],
    }

    output = tmp_path / "stale-prs.json"

    with pytest.raises(
        ValueError,
        match="must use a .csv file extension",
    ):
        stale_prs.write_csv_report(
            result,
            str(output),
        )


def test_generate_report_requires_output_path(
    tmp_path,
):
    output = tmp_path / "stale-prs.csv"

    result = {
        "organization": "OnScale",
        "generated_at": (
            "2026-07-23T10:00:00+00:00"
        ),
        "stale_threshold_days": 30,
        "repositories_scanned": 0,
        "pull_requests": [],
        "errors": [],
    }

    with patch(
        "src.tf_module_upgrade."
        "stale_prs.scan_repositories",
        return_value=result,
    ) as mocked_scan_repositories:
        with patch(
            "src.tf_module_upgrade."
            "stale_prs.write_csv_report",
            return_value=output.resolve(),
        ) as mocked_write_csv_report:
            output_path, generated_result = (
                stale_prs.generate_report(
                    output=str(output),
                    stale_days=45,
                )
            )

    assert output_path == output.resolve()
    assert generated_result == result

    mocked_scan_repositories.assert_called_once_with(
        stale_days=45
    )
    mocked_write_csv_report.assert_called_once_with(
        result,
        str(output),
    )
