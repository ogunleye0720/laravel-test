# Add stale_prs to the existing import near the top of bin/cli.py:
#
# from src.tf_module_upgrade import (
#     hcl,
#     repos,
#     stale_prs as stale_prs_report,
#     terraform_version,
#     upgrade as upg,
#     jira,
# )


@app.command("stale-prs")
def stale_prs(
    stale_days: int = typer.Option(
        30,
        min=0,
        help="Days without pull-request activity before a PR is stale.",
    ),
    output: str = typer.Option(
        "stale-prs.md",
        "--output",
        "-o",
        help="Report file. Supported extensions: .md, .json, and .csv.",
    ),
    report_format: str | None = typer.Option(
        None,
        "--format",
        help="Optional output format: markdown, json, or csv.",
    ),
    org: str | None = typer.Option(
        None,
        "--org",
        help="GitHub organization. Defaults to GITHUB_ORG_NAME or OnScale.",
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
    """Generate a stale open PR report grouped by PR author."""
    try:
        output_path, result = stale_prs_report.generate_report(
            organization=org,
            stale_days=stale_days,
            output=output,
            report_format=report_format,
            include_archived=include_archived,
            include_forks=include_forks,
            exclude_drafts=exclude_drafts,
        )
    except (stale_prs_report.GitHubAPIError, RuntimeError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    actors = stale_prs_report.group_by_actor(result.pull_requests)
    typer.echo(
        f"Report written to {output_path}. "
        f"Found {len(result.pull_requests)} stale PR(s) "
        f"across {len(actors)} author(s)."
    )

    if result.errors:
        typer.echo(
            f"Warning: {len(result.errors)} repository scan(s) failed. "
            "See the report for details.",
            err=True,
        )
        raise typer.Exit(code=2)
