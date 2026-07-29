from unittest.mock import Mock, call, patch

import pytest
import requests

from src.tf_module_upgrade import github


def test_get_file_contents():
    content = github.get_file_contents(
        "onscale-terraform-acm",
        "README.md",
    )

    assert content, "README.md not found"

    with pytest.raises(requests.exceptions.HTTPError):
        github.get_file_contents(
            "onscale-terraform-acm",
            "DOES_NOT_EXISTS",
        )


def test_get_latest_version():
    version = github.get_latest_version(
        "onscale-terraform-acm"
    )

    assert len(version) >= 5, "Version not found"

    version = github.get_latest_version(
        "DOES_NOT_EXISTS"
    )

    assert version is None, "Version should be None"


def test_search_fragments():
    results = github.search_fragments(
        ["onscale-terraform-eks-portal"]
    )

    assert len(results) >= 1, (
        "search_fragments did not return any results"
    )


def test_search_code():
    results = github.search_code(
        [
            "onscale-terraform-s3-bucket",
            "onscale-terraform-account",
        ],
        ["onscale-terraform-eks-portal"],
    )

    expected_results = {
        "onscale-terraform-eks-portal": [
            {
                "module_name": (
                    "onscale-terraform-s3-bucket"
                ),
                "file_path": "s3.tf",
                "version": "anything",
            }
        ]
    }

    for repo_name, modules in results.items():
        for module in modules:
            expected_module = next(
                (
                    expected
                    for expected in expected_results[
                        repo_name
                    ]
                    if (
                        expected["module_name"]
                        == module["module_name"]
                        and expected["file_path"]
                        == module["file_path"]
                    )
                ),
                None,
            )

            assert expected_module is not None, (
                f"Module {module['module_name']} "
                f"in repo {repo_name} not found "
                "in expected results"
            )

            assert len(module["version"]) >= 5, (
                f"Version length for module "
                f"{module['module_name']} "
                f"in repo {repo_name} is less than 6"
            )


def test_get_response():
    expected_response = Mock()

    with patch(
        "src.tf_module_upgrade.github.requests.get",
        return_value=expected_response,
    ) as mocked_get:
        response = github.get_response(
            "https://api.github.com/test",
            params={"state": "open"},
        )

    assert response is expected_response

    mocked_get.assert_called_once_with(
        "https://api.github.com/test",
        headers=github.github_headers,
        params={"state": "open"},
        verify=False,
    )

    expected_response.raise_for_status.assert_called_once_with()


def test_get_response_raises_http_error():
    expected_response = Mock()
    expected_response.raise_for_status.side_effect = (
        requests.exceptions.HTTPError(
            "404 Client Error"
        )
    )

    with patch(
        "src.tf_module_upgrade.github.requests.get",
        return_value=expected_response,
    ):
        with pytest.raises(
            requests.exceptions.HTTPError
        ):
            github.get_response(
                "https://api.github.com/missing"
            )


def test_get_paginated_items():
    first_response = Mock()
    first_response.json.return_value = [
        {"number": 1},
        {"number": 2},
    ]
    first_response.links = {
        "next": {
            "url": (
                "https://api.github.com/test?page=2"
            )
        }
    }

    second_response = Mock()
    second_response.json.return_value = [
        {"number": 3}
    ]
    second_response.links = {}

    with patch(
        "src.tf_module_upgrade.github.get_response",
        side_effect=[
            first_response,
            second_response,
        ],
    ) as mocked_get_response:
        items = list(
            github.get_paginated_items(
                "https://api.github.com/test",
                params={"per_page": 100},
            )
        )

    assert items == [
        {"number": 1},
        {"number": 2},
        {"number": 3},
    ]

    assert mocked_get_response.call_args_list == [
        call(
            "https://api.github.com/test",
            params={"per_page": 100},
        ),
        call(
            "https://api.github.com/test?page=2",
            params=None,
        ),
    ]


def test_get_paginated_items_requires_list_response():
    response = Mock()
    response.json.return_value = {
        "message": "Unexpected response"
    }
    response.links = {}

    with patch(
        "src.tf_module_upgrade.github.get_response",
        return_value=response,
    ):
        with pytest.raises(
            ValueError,
            match=(
                "Expected GitHub API response "
                "to be a list"
            ),
        ):
            list(
                github.get_paginated_items(
                    "https://api.github.com/test"
                )
            )
