#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path


BLOCK_PATTERN = re.compile(r"(?m)^[ \t]*(moved|import)[ \t]*\{")

# Generated, metadata, and dependency directories that should not normally
# contribute to a repository's Terraform source scan.
SKIPPED_DIRECTORIES = {
    ".git",
    ".terraform",
    ".terragrunt-cache",
    "__pycache__",
    "node_modules",
    "vendor",
}


@dataclass
class RepositoryResult:
    repository: str
    moved_count: int
    import_count: int
    matching_files: list[str]

    @property
    def moved_found(self) -> bool:
        return self.moved_count > 0

    @property
    def import_found(self) -> bool:
        return self.import_count > 0


def strip_hcl_comments(content: str) -> str:
    """
    Remove Terraform/HCL comments while preserving quoted strings.

    Supported comments:
      - # line comments
      - // line comments
      - /* block comments */

    This prevents commented-out `moved` or `import` blocks from being reported.
    """
    output: list[str] = []
    index = 0
    length = len(content)
    in_string = False
    escaped = False

    while index < length:
        current = content[index]
        next_char = content[index + 1] if index + 1 < length else ""

        if in_string:
            output.append(current)

            if escaped:
                escaped = False
            elif current == "\\":
                escaped = True
            elif current == '"':
                in_string = False

            index += 1
            continue

        if current == '"':
            in_string = True
            output.append(current)
            index += 1
            continue

        if current == "#":
            while index < length and content[index] != "\n":
                index += 1
            continue

        if current == "/" and next_char == "/":
            index += 2
            while index < length and content[index] != "\n":
                index += 1
            continue

        if current == "/" and next_char == "*":
            index += 2
            while index < length - 1:
                if content[index] == "*" and content[index + 1] == "/":
                    index += 2
                    break

                # Preserve newlines so line-oriented matching remains reliable.
                if content[index] == "\n":
                    output.append("\n")

                index += 1
            continue

        output.append(current)
        index += 1

    return "".join(output)


def find_terraform_files(repository_path: Path) -> list[Path]:
    """Return Terraform files recursively, excluding generated directories."""
    terraform_files: list[Path] = []

    for current_root, directory_names, file_names in os.walk(
        repository_path,
        followlinks=False,
    ):
        directory_names[:] = [
            name
            for name in directory_names
            if name not in SKIPPED_DIRECTORIES
        ]

        current_path = Path(current_root)

        for file_name in file_names:
            if file_name.endswith(".tf"):
                terraform_files.append(current_path / file_name)

    return sorted(terraform_files)


def scan_repository(repository_path: Path) -> RepositoryResult:
    """Scan one repository and return all detected block information."""
    moved_count = 0
    import_count = 0
    matching_files: list[str] = []

    for terraform_file in find_terraform_files(repository_path):
        try:
            content = terraform_file.read_text(
                encoding="utf-8",
                errors="replace",
            )
        except OSError as error:
            print(
                f"Warning: could not read {terraform_file}: {error}",
                file=sys.stderr,
            )
            continue

        uncommented_content = strip_hcl_comments(content)
        matches = BLOCK_PATTERN.findall(uncommented_content)

        file_moved_count = matches.count("moved")
        file_import_count = matches.count("import")

        if file_moved_count or file_import_count:
            moved_count += file_moved_count
            import_count += file_import_count
            matching_files.append(
                terraform_file.relative_to(repository_path).as_posix()
            )

    return RepositoryResult(
        repository=repository_path.name,
        moved_count=moved_count,
        import_count=import_count,
        matching_files=matching_files,
    )


def discover_repositories(root_directory: Path) -> list[Path]:
    """
    Treat every immediate subdirectory under the root directory as a repository.
    """
    return sorted(
        path
        for path in root_directory.iterdir()
        if path.is_dir() and path.name not in SKIPPED_DIRECTORIES
    )


def write_csv(results: list[RepositoryResult], output_file: Path) -> None:
    """Write only repositories containing at least one target block."""
    output_file.parent.mkdir(parents=True, exist_ok=True)

    with output_file.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(
            csv_file,
            fieldnames=[
                "repository",
                "moved_block_found",
                "import_block_found",
                "moved_block_count",
                "import_block_count",
                "matching_files",
            ],
        )
        writer.writeheader()

        for result in results:
            writer.writerow(
                {
                    "repository": result.repository,
                    "moved_block_found": result.moved_found,
                    "import_block_found": result.import_found,
                    "moved_block_count": result.moved_count,
                    "import_block_count": result.import_count,
                    "matching_files": "; ".join(result.matching_files),
                }
            )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Scan repository folders for Terraform moved and import blocks."
        )
    )
    parser.add_argument(
        "root",
        nargs="?",
        default="account-upgrade",
        help=(
            "Directory containing the cloned repositories "
            "(default: account-upgrade)."
        ),
    )
    parser.add_argument(
        "--output",
        "-o",
        default="terraform_moved_import_report.csv",
        help=(
            "CSV output path "
            "(default: terraform_moved_import_report.csv)."
        ),
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()

    root_directory = Path(arguments.root).expanduser().resolve()
    output_file = Path(arguments.output).expanduser().resolve()

    if not root_directory.exists():
        print(
            f"Error: root directory does not exist: {root_directory}",
            file=sys.stderr,
        )
        return 1

    if not root_directory.is_dir():
        print(
            f"Error: root path is not a directory: {root_directory}",
            file=sys.stderr,
        )
        return 1

    try:
        repositories = discover_repositories(root_directory)
    except OSError as error:
        print(
            f"Error: could not inspect {root_directory}: {error}",
            file=sys.stderr,
        )
        return 1

    if not repositories:
        print(
            f"No subdirectories were found under {root_directory}.",
            file=sys.stderr,
        )
        return 1

    matching_results: list[RepositoryResult] = []

    for repository in repositories:
        result = scan_repository(repository)

        if result.moved_found or result.import_found:
            matching_results.append(result)

    try:
        write_csv(matching_results, output_file)
    except OSError as error:
        print(
            f"Error: could not write CSV file {output_file}: {error}",
            file=sys.stderr,
        )
        return 1

    print(f"Repositories scanned: {len(repositories)}")
    print(f"Repositories with matching blocks: {len(matching_results)}")
    print(f"CSV report: {output_file}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
