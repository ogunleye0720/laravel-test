from datetime import datetime, timezone
from unittest.mock import patch

from src.tf_module_upgrade import stale_prs


def test_get_devops_controlled_repositories():
    with patch(
        "src.tf_module_upgrade.stale_prs.hcl.get_modules",
        return_value=[
            "onscale-terraform-vpc",
            "onscale-terraform-account",
        ],
    ):
        with patch(
            "src.tf_module_upgrade.stale_prs."
            "hcl.get_root_configurations",
            return_value=[
                "onscale-terraform-awsansys1",
            ],
        ):
            with patch(
                "src.tf_module_upgrade.stale_prs."
                "hcl.get_sre_applications",
                return_value=[
                    "onscale-sre-example",
                    "onscale-terraform-vpc",
                ],
            ):
                repositories = (
                    stale_prs
                    .get_devops_controlled_repositories()
                )

    assert repositories == [
        "onscale-sre-example",
        "onscale-terraform-account",
        "onscale-terraform-awsansys1",
        "onscale-terraform-vpc",
    ]


def test_get_organization_repositories_filters_results():
    github_repositories = [
        {
            "name": "onscale-terraform-vpc",
            "full_name": "OnScale/onscale-terraform-vpc",
            "archived": False,
            "fork": False,
        },
        {
            "name": "application-owned-repository",
            "full_name": (
                "OnScale/application-owned-repository"
            ),
            "archived": False,
            "fork": False,
        },
        {
            "name": "onscale-terraform-account",
            "full_name": (
                "OnScale/onscale-terraform-account"
            ),
            "archived": True,
            "fork": False,
        },
    ]

    with patch(
        "src.tf_module_upgrade.stale_prs."
        "github.get_paginated_items",
        return_value=github_repositories,
    ):
        repositories = (
            stale_prs.get_organization_repositories(
                "OnScale",
                [
                    "onscale-terraform-vpc",
                    "onscale-terraform-account",
                ],
            )
        )

    assert repositories == [
        {
            "name": "onscale-terraform-vpc",
            "full_name": "OnScale/onscale-terraform-vpc",
            "archived": False,
            "fork": False,
        },
    ]


def test_get_stale_pull_requests():
    pull_requests = [
        {
            "number": 10,
            "title": "Old pull request",
            "html_url": (
                "https://github.com/OnScale/"
                "onscale-terraform-vpc/pull/10"
            ),
            "created_at": "2026-04-01T10:00:00Z",
            "updated_at": "2026-05-01T10:00:00Z",
            "draft": False,
            "user": {
                "login": "github-user-one",
            },
            "assignees": [],
            "requested_reviewers": [],
        },
        {
            "number": 11,
            "title": "Recent pull request",
            "html_url": (
                "https://github.com/OnScale/"
                "onscale-terraform-vpc/pull/11"
            ),
            "created_at": "2026-07-01T10:00:00Z",
            "updated_at": "2026-07-20T10:00:00Z",
            "draft": False,
            "user": {
                "login": "github-user-two",
            },
            "assignees": [],
            "requested_reviewers": [],
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
        "src.tf_module_upgrade.stale_prs."
        "github.get_paginated_items",
        return_value=pull_requests,
    ):
        results = stale_prs.get_stale_pull_requests(
            "OnScale",
            "onscale-terraform-vpc",
            30,
            current_time,
        )

    assert len(results) == 1
    assert results[0]["number"] == 10
    assert results[0]["actor"] == "github-user-one"
    assert results[0]["stale_days"] == 83


def test_markdown_report_contains_pr_link(tmp_path):
    output_path = tmp_path / "stale-prs.md"

    result = {
        "organization": "OnScale",
        "generated_at": "2026-07-23T10:00:00+00:00",
        "stale_threshold_days": 30,
        "repositories_scanned": 1,
        "pull_requests": [
            {
                "actor": "github-user-one",
                "repository": (
                    "OnScale/onscale-terraform-vpc"
                ),
                "number": 10,
                "title": "Old pull request",
                "url": (
                    "https://github.com/OnScale/"
                    "onscale-terraform-vpc/pull/10"
                ),
                "created_at": "2026-04-01T10:00:00Z",
                "updated_at": "2026-05-01T10:00:00Z",
                "stale_days": 83,
                "draft": False,
                "assignees": [],
                "requested_reviewers": [],
            }
        ],
        "errors": [],
    }

    stale_prs.write_markdown_report(
        result,
        output_path,
    )

    content = output_path.read_text(
        encoding="utf-8"
    )

    assert (
        "[#10](https://github.com/OnScale/"
        "onscale-terraform-vpc/pull/10)"
        in content
    )

    assert (
        "[Old pull request]"
        "(https://github.com/OnScale/"
        "onscale-terraform-vpc/pull/10)"
        in content
    )