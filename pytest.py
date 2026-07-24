import time
from typing import Any, Dict

import requests


def _base_url(terraform_output: Dict[str, Any]) -> str:
    cloudfront_domain_name = terraform_output["cloudfront_domain_name"]["value"]
    return f"https://{cloudfront_domain_name}"


def test_serves_index_html(terraform_output: Dict[str, Any]) -> None:
    url = _base_url(terraform_output)

    for _ in range(40):
        response = requests.get(
            url,
            timeout=30,
            allow_redirects=True,
        )

        if response.status_code == 200:
            assert "Hello from CloudFront" in response.text
            return

        time.sleep(15)

    raise AssertionError(f"Never got 200 from {url}")


def test_spa_403_redirects_to_200(
    terraform_output: Dict[str, Any],
) -> None:
    url = f"{_base_url(terraform_output)}/nonexistent-path"

    response = requests.get(
        url,
        timeout=30,
        allow_redirects=True,
    )

    assert response.status_code == 200


def test_cors_header_present(
    terraform_output: Dict[str, Any],
) -> None:
    url = _base_url(terraform_output)

    response = requests.get(
        url,
        headers={"Origin": "https://example.onscale.com"},
        timeout=30,
    )

    assert "access-control-allow-origin" in response.headers
