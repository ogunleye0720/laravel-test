def get_response(
    url: str,
    params: Optional[Dict[str, Any]] = None,
) -> requests.Response:
    """
    Send an authenticated GET request to GitHub.

    The function uses the existing GitHub headers and retries temporary
    GitHub or network failures.
    """
    maximum_attempts = 4
    temporary_status_codes = [429, 500, 502, 503, 504]

    for attempt in range(1, maximum_attempts + 1):
        try:
            response = requests.get(
                url,
                headers=github_headers,
                params=params,
                timeout=30,
                verify=github_verify_ssl,
            )
        except requests.RequestException:
            if attempt == maximum_attempts:
                raise

            time.sleep(2 ** (attempt - 1))
            continue

        if (
            response.status_code not in temporary_status_codes
            or attempt == maximum_attempts
        ):
            response.raise_for_status()
            return response

        retry_after = response.headers.get("Retry-After")

        if retry_after:
            delay = int(retry_after)
        else:
            delay = 2 ** (attempt - 1)

        time.sleep(min(delay, 30))

    raise RuntimeError(f"GitHub request failed: {url}")


def get_paginated_items(
    url: str,
    params: Optional[Dict[str, Any]] = None,
) -> Iterator[Dict[str, Any]]:
    """
    Return every item from a paginated GitHub API endpoint.

    GitHub normally returns a maximum of 100 items per page. This function
    follows GitHub's next-page links until all pages have been read.
    """
    next_url: Optional[str] = url
    next_params = params

    while next_url:
        response = get_response(
            next_url,
            params=next_params,
        )

        items = response.json()

        if not isinstance(items, list):
            raise ValueError(
                f"Expected a list from GitHub but received "
                f"{type(items).__name__}."
            )

        for item in items:
            yield item

        next_url = (
            response.links.get("next", {}).get("url")
        )

        # GitHub's next URL already contains its query parameters.
        next_params = None
