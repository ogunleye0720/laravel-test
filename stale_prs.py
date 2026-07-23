import csv
import json
import sys

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

from . import github, hcl
from .types import (
    StalePullRequest,
    StaleScanError,
    StaleScanResult,
)


default_stale_days = 30
github_api_url = "https://api.github.com"


def get_current_utc_time() -> datetime:
    """
    Return the current UTC date and time.

    GitHub timestamps are returned in UTC, so the scanner also performs its
    date calculations in UTC.
    """
    return datetime.now(timezone.utc)


def parse_github_datetime(value: str) -> datetime:
    """
    Convert a GitHub date string into a Python datetime.

    GitHub returns dates such as:

        2026-07-01T10:30:00Z

    Python understands +00:00 as the UTC timezone, so Z is replaced with
    +00:00 before conversion.
    """
    return datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )


def get_devops_controlled_repositories() -> List[str]:
    """
    Return repositories controlled by the DevOps/SRE team.

    hcl.py already reads locals.tf from:

        OnScale/onscale-terraform-github-repositories

    Therefore, this scanner must reuse hcl.py instead of downloading or
    parsing locals.tf again.
    """
    modules = hcl.get_modules()
    root_configurations = hcl.get_root_configurations()
    sre_applications = hcl.get_sre_applications()

    repositories = (
        modules
        + root_configurations
        + sre_applications
    )

    # A set removes duplicates. sorted() gives a predictable order.
    unique_repositories = {
        repository
        for repository in repositories
        if repository
    }

    return sorted(unique_repositories)


def get_organization_repositories(
    organization: str,
    allowed_repositories: List[str],
    include_archived: bool = False,
    include_forks: bool = False,
) -> List[Dict[str, Any]]:
    """
    Retrieve repositories from GitHub and keep only DevOps-controlled ones.

    The allowed repository names come from hcl.py.
    """
    allowed_repository_names = {
        repository.lower()
        for repository in allowed_repositories
    }

    url = (
        f"{github_api_url}/orgs/"
        f"{organization}/repos"
    )

    params = {
        "type": "all",
        "sort": "full_name",
        "direction": "asc",
        "per_page": 100,
    }

    repositories: List[Dict[str, Any]] = []

    for repository in github.get_paginated_items(
        url,
        params=params,
    ):
        repository_name = repository.get("name")

        if not repository_name:
            continue

        if (
            repository.get("archived", False)
            and not include_archived
        ):
            continue

        if (
            repository.get("fork", False)
            and not include_forks
        ):
            continue

        if (
            repository_name.lower()
            not in allowed_repository_names
        ):
            continue

        repositories.append(repository)

    return repositories


def get_stale_pull_requests(
    organization: str,
    repository_name: str,
    stale_days: int,
    current_time: datetime,
    exclude_drafts: bool = False,
) -> List[StalePullRequest]:
    """
    Return stale open PRs from one repository.

    A PR is stale when its updated_at value is older than the configured
    cutoff date.
    """
    cutoff = current_time - timedelta(days=stale_days)

    url = (
        f"{github_api_url}/repos/"
        f"{organization}/{repository_name}/pulls"
    )

    params = {
        "state": "open",
        "sort": "updated",
        "direction": "asc",
        "per_page": 100,
    }

    stale_pull_requests: List[StalePullRequest] = []

    for pull_request in github.get_paginated_items(
        url,
        params=params,
    ):
        updated_at_value = pull_request.get("updated_at")

        if not updated_at_value:
            continue

        updated_at = parse_github_datetime(
            updated_at_value
        )

        # PRs are sorted by oldest update first.
        #
        # Once a fresh PR is reached, every PR after it should also be fresh,
        # so no additional PRs need to be checked.
        if updated_at > cutoff:
            break

        if (
            exclude_drafts
            and pull_request.get("draft", False)
        ):
            continue

        user = pull_request.get("user") or {}
        actor = user.get("login") or "unknown"

        assignees = []

        for assignee in pull_request.get(
            "assignees",
            [],
        ):
            login = assignee.get("login")

            if login:
                assignees.append(login)

        requested_reviewers = []

        for reviewer in pull_request.get(
            "requested_reviewers",
            [],
        ):
            login = reviewer.get("login")

            if login:
                requested_reviewers.append(login)

        stale_pull_request: StalePullRequest = {
            "actor": actor,
            "repository": (
                f"{organization}/{repository_name}"
            ),
            "number": pull_request["number"],
            "title": pull_request["title"],
            "url": pull_request["html_url"],
            "created_at": pull_request["created_at"],
            "updated_at": updated_at_value,
            "stale_days": max(
                0,
                (current_time - updated_at).days,
            ),
            "draft": pull_request.get(
                "draft",
                False,
            ),
            "assignees": sorted(assignees),
            "requested_reviewers": sorted(
                requested_reviewers
            ),
        }

        stale_pull_requests.append(
            stale_pull_request
        )

    return stale_pull_requests


def scan_organization(
    organization: Optional[str] = None,
    stale_days: int = default_stale_days,
    include_archived: bool = False,
    include_forks: bool = False,
    exclude_drafts: bool = False,
    current_time: Optional[datetime] = None,
) -> StaleScanResult:
    """
    Scan DevOps-controlled repositories for stale open PRs.
    """
    if stale_days < 0:
        raise ValueError(
            "stale_days must be zero or greater."
        )

    selected_organization = (
        organization or github.github_org_name
    )

    scan_time = (
        current_time or get_current_utc_time()
    )

    controlled_repositories = (
        get_devops_controlled_repositories()
    )

    print(
        f"Loaded {len(controlled_repositories)} "
        "DevOps-controlled repositories from hcl.py.",
        file=sys.stderr,
    )

    repositories = get_organization_repositories(
        selected_organization,
        controlled_repositories,
        include_archived=include_archived,
        include_forks=include_forks,
    )

    print(
        f"Found {len(repositories)} matching "
        "repositories that are visible to the GitHub token.",
        file=sys.stderr,
    )

    pull_requests: List[StalePullRequest] = []
    errors: List[StaleScanError] = []

    for index, repository in enumerate(
        repositories,
        start=1,
    ):
        repository_name = repository["name"]
        repository_full_name = repository["full_name"]

        try:
            repository_pull_requests = (
                get_stale_pull_requests(
                    selected_organization,
                    repository_name,
                    stale_days,
                    scan_time,
                    exclude_drafts=exclude_drafts,
                )
            )

            pull_requests.extend(
                repository_pull_requests
            )

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
            error: StaleScanError = {
                "repository": repository_full_name,
                "error": str(exception),
            }

            errors.append(error)

            print(
                f"[{index}/{len(repositories)}] "
                f"{repository_full_name}: "
                f"scan failed: {exception}",
                file=sys.stderr,
            )

    return {
        "organization": selected_organization,
        "generated_at": scan_time.isoformat(),
        "stale_threshold_days": stale_days,
        "repositories_scanned": len(repositories),
        "pull_requests": pull_requests,
        "errors": errors,
    }


def group_by_actor(
    pull_requests: List[StalePullRequest],
) -> Dict[str, List[StalePullRequest]]:
    """
    Group stale PRs by the GitHub user who created each PR.
    """
    grouped: Dict[
        str,
        List[StalePullRequest],
    ] = {}

    for pull_request in pull_requests:
        actor = pull_request["actor"]

        if actor not in grouped:
            grouped[actor] = []

        grouped[actor].append(pull_request)

    sorted_grouped: Dict[
        str,
        List[StalePullRequest],
    ] = {}

    for actor in sorted(
        grouped.keys(),
        key=str.lower,
    ):
        actor_pull_requests = grouped[actor]

        sorted_grouped[actor] = sorted(
            actor_pull_requests,
            key=lambda pull_request: (
                -pull_request["stale_days"],
                pull_request["repository"].lower(),
                pull_request["number"],
            ),
        )

    return sorted_grouped


def escape_markdown(value: str) -> str:
    """
    Prevent PR titles from breaking Markdown table formatting.
    """
    return (
        value.replace("|", "\\|")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def get_report_format(
    output: str,
    report_format: Optional[str] = None,
) -> str:
    """
    Determine the report format from --format or the output extension.
    """
    if report_format:
        selected_format = report_format.lower()

        if selected_format not in [
            "markdown",
            "json",
            "csv",
        ]:
            raise ValueError(
                "Supported formats are markdown, json and csv."
            )

        return selected_format

    extension = Path(output).suffix.lower()

    formats = {
        ".md": "markdown",
        ".markdown": "markdown",
        ".json": "json",
        ".csv": "csv",
    }

    selected_format = formats.get(extension)

    if not selected_format:
        raise ValueError(
            "The output filename must end in "
            ".md, .markdown, .json or .csv."
        )

    return selected_format


def write_markdown_report(
    result: StaleScanResult,
    output_path: Path,
) -> None:
    """
    Write a human-readable Markdown report with clickable PR links.
    """
    grouped = group_by_actor(
        result["pull_requests"]
    )

    lines = [
        "# Stale Open Pull Requests",
        "",
        (
            f"- **Organization:** "
            f"`{result['organization']}`"
        ),
        (
            f"- **Generated:** "
            f"`{result['generated_at']}`"
        ),
        (
            f"- **Stale threshold:** "
            f"`{result['stale_threshold_days']} days`"
        ),
        (
            f"- **Repositories scanned:** "
            f"`{result['repositories_scanned']}`"
        ),
        (
            f"- **Actors with stale PRs:** "
            f"`{len(grouped)}`"
        ),
        (
            f"- **Total stale PRs:** "
            f"`{len(result['pull_requests'])}`"
        ),
        "",
    ]

    if not grouped:
        lines.extend(
            [
                "No stale open pull requests were found.",
                "",
            ]
        )

    for actor, pull_requests in grouped.items():
        lines.extend(
            [
                (
                    f"## {escape_markdown(actor)} "
                    f"({len(pull_requests)})"
                ),
                "",
                (
                    "| Age | Repository | Pull request | "
                    "Title | Last updated | Draft |"
                ),
                "|---:|---|---|---|---|:---:|",
            ]
        )

        for pull_request in pull_requests:
            title = escape_markdown(
                pull_request["title"]
            )

            url = pull_request["url"]

            draft_value = (
                "Yes"
                if pull_request["draft"]
                else "No"
            )

            lines.append(
                f"| {pull_request['stale_days']} days "
                f"| `{pull_request['repository']}` "
                f"| [#{pull_request['number']}]({url}) "
                f"| [{title}]({url}) "
                f"| `{pull_request['updated_at']}` "
                f"| {draft_value} |"
            )

        lines.append("")

    if result["errors"]:
        lines.extend(
            [
                "## Scan Errors",
                "",
                (
                    "The following repositories could "
                    "not be scanned:"
                ),
                "",
            ]
        )

        for error in result["errors"]:
            lines.append(
                f"- `{error['repository']}`: "
                f"{escape_markdown(error['error'])}"
            )

        lines.append("")

    output_path.write_text(
        "\n".join(lines),
        encoding="utf-8",
    )


def write_json_report(
    result: StaleScanResult,
    output_path: Path,
) -> None:
    """
    Write a JSON report grouped by PR author.
    """
    grouped = group_by_actor(
        result["pull_requests"]
    )

    document = {
        "metadata": {
            "organization": result["organization"],
            "generated_at": result["generated_at"],
            "stale_threshold_days": (
                result["stale_threshold_days"]
            ),
            "repositories_scanned": (
                result["repositories_scanned"]
            ),
            "actors_with_stale_prs": len(grouped),
            "total_stale_prs": len(
                result["pull_requests"]
            ),
            "scan_errors": len(result["errors"]),
        },
        "actors": grouped,
        "errors": result["errors"],
    }

    output_path.write_text(
        json.dumps(
            document,
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def write_csv_report(
    result: StaleScanResult,
    output_path: Path,
) -> None:
    """
    Write one stale PR per CSV row.
    """
    fieldnames = [
        "actor",
        "repository",
        "number",
        "title",
        "url",
        "created_at",
        "updated_at",
        "stale_days",
        "draft",
        "assignees",
        "requested_reviewers",
    ]

    grouped = group_by_actor(
        result["pull_requests"]
    )

    with output_path.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=fieldnames,
        )

        writer.writeheader()

        for pull_requests in grouped.values():
            for pull_request in pull_requests:
                row = dict(pull_request)

                row["assignees"] = ",".join(
                    pull_request["assignees"]
                )

                row["requested_reviewers"] = ",".join(
                    pull_request["requested_reviewers"]
                )

                writer.writerow(row)


def write_report(
    result: StaleScanResult,
    output: str,
    report_format: Optional[str] = None,
) -> Path:
    """
    Create the requested Markdown, JSON or CSV report.
    """
    output_path = Path(output).expanduser()

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    selected_format = get_report_format(
        output,
        report_format,
    )

    if selected_format == "json":
        write_json_report(
            result,
            output_path,
        )

    elif selected_format == "csv":
        write_csv_report(
            result,
            output_path,
        )

    else:
        write_markdown_report(
            result,
            output_path,
        )

    return output_path.resolve()


def generate_report(
    organization: Optional[str] = None,
    stale_days: int = default_stale_days,
    output: str = "stale-prs.md",
    report_format: Optional[str] = None,
    include_archived: bool = False,
    include_forks: bool = False,
    exclude_drafts: bool = False,
) -> Tuple[Path, StaleScanResult]:
    """
    Scan the organization and generate the requested report.
    """
    result = scan_organization(
        organization=organization,
        stale_days=stale_days,
        include_archived=include_archived,
        include_forks=include_forks,
        exclude_drafts=exclude_drafts,
    )

    output_path = write_report(
        result,
        output,
        report_format=report_format,
    )

    return output_path, result