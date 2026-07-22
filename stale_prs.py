from __future__ import annotations

import csv
import json
import os
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

import requests

DEFAULT_API_URL = "https://api.github.com"
DEFAULT_STALE_DAYS = 30
REQUEST_TIMEOUT_SECONDS = 30
MAX_REQUEST_ATTEMPTS = 4
TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}


class GitHubAPIError(RuntimeError):
    """Raised when GitHub returns an unsuccessful API response."""


@dataclass(frozen=True)
class StalePullRequest:
    actor: str
    repository: str
    number: int
    title: str
    url: str
    created_at: str
    updated_at: str
    stale_days: int
    draft: bool
    assignees: list[str]
    requested_reviewers: list[str]


@dataclass(frozen=True)
class ScanError:
    repository: str
    error: str


@dataclass(frozen=True)
class ScanResult:
    organization: str
    generated_at: datetime
    stale_threshold_days: int
    repositories_scanned: int
    pull_requests: list[StalePullRequest]
    errors: list[ScanError]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_github_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def get_github_token() -> str:
    """Return the configured GitHub token.

    The existing utilities repository uses GITHUB_TOKEN, so this command keeps
    the same convention. GH_TOKEN is accepted as a compatibility fallback.
    """
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if not token:
        raise RuntimeError(
            "GITHUB_TOKEN is not configured. Export it in your shell or add it "
            "to the repository .env file."
        )
    return token.strip()


class GitHubClient:
    def __init__(
        self,
        token: str,
        api_url: str = DEFAULT_API_URL,
        session: requests.Session | None = None,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "Authorization": f"token {token}",
                "User-Agent": "scale-terraform-utilities/stale-prs",
            }
        )
        self.rate_limit_remaining: str | None = None
        self.rate_limit_limit: str | None = None

    def _url(self, path_or_url: str) -> str:
        if path_or_url.startswith(("https://", "http://")):
            return path_or_url
        return f"{self.api_url}/{path_or_url.lstrip('/')}"

    def get(
        self,
        path_or_url: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> requests.Response:
        url = self._url(path_or_url)
        response: requests.Response | None = None

        for attempt in range(1, MAX_REQUEST_ATTEMPTS + 1):
            try:
                response = self.session.get(
                    url,
                    params=params,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            except requests.RequestException as exc:
                if attempt == MAX_REQUEST_ATTEMPTS:
                    raise GitHubAPIError(f"Request to {url} failed: {exc}") from exc
                time.sleep(float(2 ** (attempt - 1)))
                continue

            self.rate_limit_remaining = response.headers.get("X-RateLimit-Remaining")
            self.rate_limit_limit = response.headers.get("X-RateLimit-Limit")

            should_retry = response.status_code in TRANSIENT_STATUS_CODES
            if response.status_code == 403 and response.headers.get("Retry-After"):
                should_retry = True

            if response.ok or not should_retry or attempt == MAX_REQUEST_ATTEMPTS:
                break

            retry_after = response.headers.get("Retry-After")
            delay = float(retry_after) if retry_after else float(2 ** (attempt - 1))
            time.sleep(min(delay, 30.0))

        if response is None:
            raise GitHubAPIError(f"No response received from {url}.")

        if response.ok:
            return response

        try:
            payload = response.json()
            message = payload.get("message", response.text)
        except ValueError:
            message = response.text or response.reason

        notes: list[str] = []
        if response.status_code in (403, 429):
            reset_epoch = response.headers.get("X-RateLimit-Reset")
            if reset_epoch and self.rate_limit_remaining == "0":
                reset_time = datetime.fromtimestamp(
                    int(reset_epoch), tz=timezone.utc
                ).isoformat()
                notes.append(f"Rate limit resets at {reset_time}")

        if response.headers.get("X-GitHub-SSO"):
            notes.append(
                "The token may need SAML SSO authorization for the organization"
            )

        note_text = f" {'; '.join(notes)}." if notes else ""
        raise GitHubAPIError(
            f"GitHub API returned {response.status_code} for {url}: "
            f"{message}.{note_text}"
        )

    def iter_items(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        next_url: str | None = path
        next_params = dict(params or {})
        next_params.setdefault("per_page", 100)

        while next_url:
            response = self.get(next_url, params=next_params)
            payload = response.json()
            if not isinstance(payload, list):
                raise GitHubAPIError(
                    f"Expected a list response from {response.url}, "
                    f"received {type(payload).__name__}."
                )

            yield from payload
            next_url = response.links.get("next", {}).get("url")
            next_params = None


def list_organization_repositories(
    client: GitHubClient,
    organization: str,
    *,
    include_archived: bool = False,
    include_forks: bool = False,
) -> list[dict[str, Any]]:
    org = quote(organization, safe="")
    repositories: list[dict[str, Any]] = []

    for repository in client.iter_items(
        f"/orgs/{org}/repos",
        params={
            "type": "all",
            "sort": "full_name",
            "direction": "asc",
        },
    ):
        if repository.get("archived") and not include_archived:
            continue
        if repository.get("fork") and not include_forks:
            continue
        repositories.append(repository)

    return repositories


def list_stale_pull_requests(
    client: GitHubClient,
    repository_full_name: str,
    *,
    cutoff: datetime,
    now: datetime,
    exclude_drafts: bool = False,
) -> list[StalePullRequest]:
    encoded_repo = quote(repository_full_name, safe="/")
    stale_pull_requests: list[StalePullRequest] = []
    next_url: str | None = f"/repos/{encoded_repo}/pulls"
    params: dict[str, Any] | None = {
        "state": "open",
        "sort": "updated",
        "direction": "asc",
        "per_page": 100,
    }

    while next_url:
        response = client.get(next_url, params=params)
        payload = response.json()
        if not isinstance(payload, list):
            raise GitHubAPIError(
                f"Expected a list of pull requests for {repository_full_name}."
            )

        reached_fresh_pull_request = False
        for pull_request in payload:
            updated_at = parse_github_datetime(pull_request["updated_at"])
            if updated_at > cutoff:
                reached_fresh_pull_request = True
                break

            if exclude_drafts and pull_request.get("draft", False):
                continue

            actor = (pull_request.get("user") or {}).get("login") or "unknown"
            assignees = sorted(
                assignee["login"]
                for assignee in pull_request.get("assignees", [])
                if assignee.get("login")
            )
            requested_reviewers = sorted(
                reviewer["login"]
                for reviewer in pull_request.get("requested_reviewers", [])
                if reviewer.get("login")
            )

            stale_pull_requests.append(
                StalePullRequest(
                    actor=actor,
                    repository=repository_full_name,
                    number=int(pull_request["number"]),
                    title=pull_request.get("title") or "",
                    url=pull_request["html_url"],
                    created_at=pull_request["created_at"],
                    updated_at=pull_request["updated_at"],
                    stale_days=max(0, (now - updated_at).days),
                    draft=bool(pull_request.get("draft", False)),
                    assignees=assignees,
                    requested_reviewers=requested_reviewers,
                )
            )

        if reached_fresh_pull_request:
            break

        next_url = response.links.get("next", {}).get("url")
        params = None

    return stale_pull_requests


def scan_organization(
    organization: str,
    *,
    stale_days: int = DEFAULT_STALE_DAYS,
    include_archived: bool = False,
    include_forks: bool = False,
    exclude_drafts: bool = False,
    client: GitHubClient | None = None,
    now: datetime | None = None,
) -> ScanResult:
    if stale_days < 0:
        raise ValueError("stale_days must be zero or greater.")

    generated_at = now or utc_now()
    cutoff = generated_at - timedelta(days=stale_days)
    github_client = client or GitHubClient(get_github_token())

    repositories = list_organization_repositories(
        github_client,
        organization,
        include_archived=include_archived,
        include_forks=include_forks,
    )

    stale_pull_requests: list[StalePullRequest] = []
    errors: list[ScanError] = []

    for index, repository in enumerate(repositories, start=1):
        full_name = repository["full_name"]
        try:
            repository_pull_requests = list_stale_pull_requests(
                github_client,
                full_name,
                cutoff=cutoff,
                now=generated_at,
                exclude_drafts=exclude_drafts,
            )
            stale_pull_requests.extend(repository_pull_requests)
            print(
                f"[{index}/{len(repositories)}] {full_name}: "
                f"{len(repository_pull_requests)} stale open PR(s)",
                file=sys.stderr,
            )
        except GitHubAPIError as exc:
            errors.append(ScanError(repository=full_name, error=str(exc)))
            print(
                f"[{index}/{len(repositories)}] {full_name}: ERROR: {exc}",
                file=sys.stderr,
            )

    return ScanResult(
        organization=organization,
        generated_at=generated_at,
        stale_threshold_days=stale_days,
        repositories_scanned=len(repositories),
        pull_requests=stale_pull_requests,
        errors=errors,
    )


def group_by_actor(
    pull_requests: list[StalePullRequest],
) -> dict[str, list[StalePullRequest]]:
    grouped: dict[str, list[StalePullRequest]] = defaultdict(list)
    for pull_request in pull_requests:
        grouped[pull_request.actor].append(pull_request)

    return {
        actor: sorted(
            actor_pull_requests,
            key=lambda item: (
                -item.stale_days,
                item.repository.lower(),
                item.number,
            ),
        )
        for actor, actor_pull_requests in sorted(
            grouped.items(), key=lambda item: item[0].lower()
        )
    }


def markdown_escape(value: str) -> str:
    return value.replace("|", r"\|").replace("\r", " ").replace("\n", " ")


def determine_report_format(output_path: Path, report_format: str | None) -> str:
    if report_format:
        normalized_format = report_format.lower()
        if normalized_format not in {"markdown", "json", "csv"}:
            raise ValueError("format must be one of: markdown, json, csv.")
        return normalized_format

    extension_map = {
        ".json": "json",
        ".csv": "csv",
        ".md": "markdown",
        ".markdown": "markdown",
    }
    try:
        return extension_map[output_path.suffix.lower()]
    except KeyError as exc:
        raise ValueError(
            "Could not infer output format. Use a .json, .csv, .md file or "
            "provide --format."
        ) from exc


def write_json_report(output_path: Path, result: ScanResult) -> None:
    grouped = group_by_actor(result.pull_requests)
    document = {
        "metadata": {
            "organization": result.organization,
            "generated_at": result.generated_at.isoformat(),
            "stale_threshold_days": result.stale_threshold_days,
            "repositories_scanned": result.repositories_scanned,
            "actors_with_stale_prs": len(grouped),
            "total_stale_prs": len(result.pull_requests),
            "scan_errors": len(result.errors),
        },
        "actors": {
            actor: [asdict(pull_request) for pull_request in pull_requests]
            for actor, pull_requests in grouped.items()
        },
        "errors": [asdict(error) for error in result.errors],
    }
    output_path.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def write_csv_report(output_path: Path, result: ScanResult) -> None:
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
    grouped = group_by_actor(result.pull_requests)

    with output_path.open("w", newline="", encoding="utf-8") as file_handle:
        writer = csv.DictWriter(file_handle, fieldnames=fieldnames)
        writer.writeheader()
        for actor, pull_requests in grouped.items():
            for pull_request in pull_requests:
                row = asdict(pull_request)
                row["actor"] = actor
                row["assignees"] = ",".join(pull_request.assignees)
                row["requested_reviewers"] = ",".join(
                    pull_request.requested_reviewers
                )
                writer.writerow(row)


def write_markdown_report(output_path: Path, result: ScanResult) -> None:
    grouped = group_by_actor(result.pull_requests)
    lines = [
        "# Stale Open Pull Requests",
        "",
        f"- **Organization:** `{result.organization}`",
        f"- **Generated:** `{result.generated_at.isoformat()}`",
        (
            "- **Stale threshold:** "
            f"`{result.stale_threshold_days} days since last update`"
        ),
        f"- **Repositories scanned:** `{result.repositories_scanned}`",
        f"- **Actors with stale PRs:** `{len(grouped)}`",
        f"- **Total stale PRs:** `{len(result.pull_requests)}`",
        "",
    ]

    if not grouped:
        lines.extend(["No stale open pull requests were found.", ""])
    else:
        for actor, pull_requests in grouped.items():
            lines.extend(
                [
                    f"## {markdown_escape(actor)} ({len(pull_requests)})",
                    "",
                    (
                        "| Age | Repository | Pull request | Title | "
                        "Last updated | Draft |"
                    ),
                    "|---:|---|---|---|---|:---:|",
                ]
            )
            for pull_request in pull_requests:
                lines.append(
                    "| "
                    f"{pull_request.stale_days} days | "
                    f"`{markdown_escape(pull_request.repository)}` | "
                    f"[#{pull_request.number}]({pull_request.url}) | "
                    f"[{markdown_escape(pull_request.title)}]({pull_request.url}) | "
                    f"`{pull_request.updated_at}` | "
                    f"{'Yes' if pull_request.draft else 'No'} |"
                )
            lines.append("")

    if result.errors:
        lines.extend(
            [
                "## Scan Errors",
                "",
                "The following repositories could not be scanned:",
                "",
            ]
        )
        for error in result.errors:
            lines.append(
                f"- `{markdown_escape(error.repository)}`: "
                f"{markdown_escape(error.error)}"
            )
        lines.append("")

    output_path.write_text("\n".join(lines), encoding="utf-8")


def write_report(
    result: ScanResult,
    output: str,
    *,
    report_format: str | None = None,
) -> Path:
    output_path = Path(output).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    selected_format = determine_report_format(output_path, report_format)

    if selected_format == "json":
        write_json_report(output_path, result)
    elif selected_format == "csv":
        write_csv_report(output_path, result)
    else:
        write_markdown_report(output_path, result)

    return output_path.resolve()


def generate_report(
    *,
    organization: str | None = None,
    stale_days: int = DEFAULT_STALE_DAYS,
    output: str = "stale-prs.md",
    report_format: str | None = None,
    include_archived: bool = False,
    include_forks: bool = False,
    exclude_drafts: bool = False,
) -> tuple[Path, ScanResult]:
    selected_organization = organization or os.getenv("GITHUB_ORG_NAME", "scale")
    result = scan_organization(
        selected_organization,
        stale_days=stale_days,
        include_archived=include_archived,
        include_forks=include_forks,
        exclude_drafts=exclude_drafts,
    )
    output_path = write_report(result, output, report_format=report_format)
    return output_path, result
