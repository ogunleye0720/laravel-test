@app.command("stale-prs")
def stale_prs(
    stale_days: int = typer.Option(
        30,
        min=0,
        help=(
            "Days without pull-request activity "
            "before a PR is stale."
        ),
    ),
    output: str = typer.Option(
        "stale-prs.md",
        "--output",
        "-o",
        help=(
            "Report file. Supported extensions: "
            ".md, .json and .csv."
        ),
    ),
    report_format: Optional[str] = typer.Option(
        None,
        "--format",
        help=(
            "Optional output format: "
            "markdown, json or csv."
        ),
    ),
    org: Optional[str] = typer.Option(
        None,
        "--org",
        help=(
            "GitHub organization. Defaults to "
            "the GITHUB_ORG_NAME environment variable."
        ),
    ),
    include_archived: bool = typer.Option(
        False,
        help="Include archived repositories.",
    ),
    include_forks: bool = typer.Option(
        False,
        help="Include forked repositories.",
    ),
    exclude_drafts: bool = typer.Option(
        False,
        help="Exclude draft pull requests.",
    ),
):
    """
    Generate a stale open PR report grouped by PR author.

    Only repositories returned by hcl.py are scanned.
    """
    try:
        output_path, result = (
            stale_prs_report.generate_report(
                organization=org,
                stale_days=stale_days,
                output=output,
                report_format=report_format,
                include_archived=include_archived,
                include_forks=include_forks,
                exclude_drafts=exclude_drafts,
            )
        )

    except (
        requests.RequestException,
        RuntimeError,
        ValueError,
    ) as exception:
        typer.echo(
            f"Error: {exception}",
            err=True,
        )

        raise typer.Exit(code=1) from exception

    grouped = stale_prs_report.group_by_actor(
        result["pull_requests"]
    )

    typer.echo(
        f"Report written to {output_path}. "
        f"Found {len(result['pull_requests'])} "
        f"stale PR(s) across {len(grouped)} author(s)."
    )

    if result["errors"]:
        typer.echo(
            f"Warning: {len(result['errors'])} "
            "repository scan(s) failed. "
            "See the report for details.",
            err=True,
        )

        raise typer.Exit(code=2)