import csv
import sys

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from . import github, hcl


default_stale_days = 30


def get_current_utc_time() -> datetime:
    """
    Return the current UTC date and time.

    GitHub timestamps use UTC. Using UTC throughout the scanner avoids
    timezone-related errors when calculating how long a PR has been stale.
    """
    return datetime.now(timezone.utc)


def parse_github_datetime(value: str) -> datetime:
    """
    Convert a GitHub timestamp into a Python datetime.

    GitHub returns timestamps such as:

        2026-07-01T10:30:00Z

    Python represents UTC using +00:00, so the Z suffix is replaced
    before conversion.
    """
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def get_devops_controlled_repositories() -> List[str]:
    """
    Return the exact repositories controlled by the DevOps team.

    hcl.py already reads the existing locals.tf catalogue from the
    onscale-terraform-github-repositories repository.

    The stale PR scanner therefore does not download locals.tf, parse
    HCL, or discover repositories from the GitHub organization.
    """
    modules = hcl.get_modules()
    root_configurations = hcl.get_root_configurations()

    repositories = modules + root_configurations

    # A set removes duplicates. Sorting produces a predictable scanning
    # and report order.
    unique_repositories = {
        repository_name
        for repository_name in repositories
        if repository_name
    }

    return sorted(unique_repositories)


def get_stale_pull_requests(
    repository_name: str,
    stale_days: int,
    current_time: datetime,
) -> List[Dict[str, Any]]:
    """
    Return stale, non-draft, open PRs from one repository.

    A PR is stale when its updated_at timestamp is older than the
    configured cutoff time.
    """
    cutoff_time = current_time - timedelta(days=stale_days)

    pull_requests_url = (
        f"{github.github_api_url}/repos/"
        f"{github.github_org_name}/"
        f"{repository_name}/pulls"
    )

    params = {
        "state": "open",
        "sort": "updated",
        "direction": "asc",
        "per_page": 100,
    }

    stale_pull_requests: List[Dict[str, Any]] = []

    for pull_request in github.get_paginated_items(
        pull_requests_url,
        params=params,
    ):
        updated_at_value = pull_request.get("updated_at")

        if not isinstance(updated_at_value, str):
            continue

        updated_at = parse_github_datetime(updated_at_value)

        # Results are requested from oldest update to newest update.
        #
        # Once a recently updated PR is reached, all remaining PRs will
        # also be recent, so the repository scan can stop.
        if updated_at > cutoff_time:
            break

        # Draft PRs are outside the scope of this scanner.
        if pull_request.get("draft", False):
            continue

        pull_request_number = pull_request.get("number")

        if not isinstance(pull_request_number, int):
            continue

        user = pull_request.get("user") or {}
        actor = user.get("login") or "unknown"

        repository_full_name = (
            f"{github.github_org_name}/"
            f"{repository_name}"
        )

        stale_pull_requests.append(
            {
                "actor": actor,
                "repository": repository_full_name,
                "pull_request_number": pull_request_number,
                "title": pull_request.get("title", ""),
                "url": pull_request.get("html_url", ""),
                "created_at": pull_request.get("created_at", ""),
                "updated_at": updated_at_value,
                "stale_days": max(
                    0,
                    (current_time - updated_at).days,
                ),
            }
        )

    return stale_pull_requests


def scan_repositories(
    stale_days: int = default_stale_days,
    current_time: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Scan only the repositories returned by hcl.py.

    This function deliberately does not call:

        GET /orgs/{organization}/repos

    The PR API is called directly for every repository returned by the
    existing HCL module.
    """
    if stale_days < 0:
        raise ValueError(
            "stale_days must be zero or greater."
        )

    scan_time = current_time or get_current_utc_time()

    repositories = get_devops_controlled_repositories()

    print(
        f"Loaded {len(repositories)} "
        "DevOps-controlled repositories from hcl.py.",
        file=sys.stderr,
    )

    pull_requests: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []

    for index, repository_name in enumerate(
        repositories,
        start=1,
    ):
        repository_full_name = (
            f"{github.github_org_name}/"
            f"{repository_name}"
        )

        try:
            repository_pull_requests = get_stale_pull_requests(
                repository_name,
                stale_days,
                scan_time,
            )

            pull_requests.extend(repository_pull_requests)

            print(
                f"[{index}/{len(repositories)}] "
                f"{repository_full_name}: "
                f"{len(repository_pull_requests)} "
                "stale open PR(s)",
                file=sys.stderr,
            )

        except (
            requests.RequestException,
            ValueError,
            KeyError,
        ) as exception:
            errors.append(
                {
                    "repository": repository_full_name,
                    "error": str(exception),
                }
            )

            print(
                f"[{index}/{len(repositories)}] "
                f"{repository_full_name}: "
                f"scan failed: {exception}",
                file=sys.stderr,
            )

    return {
        "organization": github.github_org_name,
        "generated_at": scan_time.isoformat(),
        "stale_threshold_days": stale_days,
        "repositories_scanned": len(repositories),
        "pull_requests": pull_requests,
        "errors": errors,
    }


def group_by_actor(
    pull_requests: List[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Group stale PRs according to the GitHub user who created them.

    Example:

        {
            "user-one": [
                pull_request_one,
                pull_request_two,
            ],
            "user-two": [
                pull_request_three,
            ],
        }
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {}

    for pull_request in pull_requests:
        actor = pull_request["actor"]

        if actor not in grouped:
            grouped[actor] = []

        grouped[actor].append(pull_request)

    sorted_grouped: Dict[str, List[Dict[str, Any]]] = {}

    for actor in sorted(
        grouped.keys(),
        key=str.lower,
    ):
        sorted_grouped[actor] = sorted(
            grouped[actor],
            key=lambda pull_request: (
                -pull_request["stale_days"],
                pull_request["repository"].lower(),
                pull_request["pull_request_number"],
            ),
        )

    return sorted_grouped


def write_csv_report(
    result: Dict[str, Any],
    output: str,
) -> Path:
    """
    Write stale PRs to a CSV report.

    Rows are written actor by actor so that all stale PRs belonging to
    one author remain together in the report.
    """
    output_path = Path(output).expanduser()

    if output_path.suffix.lower() != ".csv":
        raise ValueError(
            "The stale PR report must use "
            "a .csv file extension."
        )

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    fieldnames = [
        "actor",
        "repository",
        "pull_request_number",
        "title",
        "url",
        "created_at",
        "updated_at",
        "stale_days",
    ]

    grouped_pull_requests = group_by_actor(
        result["pull_requests"]
    )

    with output_path.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as report_file:
        writer = csv.DictWriter(
            report_file,
            fieldnames=fieldnames,
        )

        writer.writeheader()

        for actor_pull_requests in grouped_pull_requests.values():
            for pull_request in actor_pull_requests:
                writer.writerow(
                    {
                        "actor": pull_request["actor"],
                        "repository": pull_request["repository"],
                        "pull_request_number": pull_request[
                            "pull_request_number"
                        ],
                        "title": pull_request["title"],
                        "url": pull_request["url"],
                        "created_at": pull_request["created_at"],
                        "updated_at": pull_request["updated_at"],
                        "stale_days": pull_request["stale_days"],
                    }
                )

    return output_path.resolve()


def generate_report(
    output: str,
    stale_days: int = default_stale_days,
) -> Tuple[Path, Dict[str, Any]]:
    """
    Scan DevOps-controlled repositories and create the CSV report.

    The output CSV path must be supplied by the caller.
    """
    result = scan_repositories(
        stale_days=stale_days,
    )

    output_path = write_csv_report(
        result,
        output,
    )

    return output_path, result
