from __future__ import annotations

import sys
import time
from typing import Any

import requests


class SecClient:
    """SEC HTTP client with rate limiting and retries."""

    def __init__(
        self,
        *,
        name: str,
        email: str,
        max_requests_per_second: float = 6.0,
        max_retries: int = 5,
        timeout: float = 60.0,
    ) -> None:

        if not name.strip():
            raise ValueError(
                "SEC client name cannot be empty."
            )

        if "@" not in email:
            raise ValueError(
                "SEC client email appears invalid."
            )

        if max_requests_per_second <= 0:
            raise ValueError(
                "max_requests_per_second must be positive."
            )

        self.min_interval_seconds = (
            1.0 / max_requests_per_second
        )

        self.max_retries = max_retries
        self.timeout = timeout

        self.last_request_time = 0.0

        self.session = requests.Session()

        self.session.headers.update(
            {
                "User-Agent": (
                    f"DisclosureDelta/0.1 "
                    f"{name} "
                    f"{email}"
                ),
                "Accept-Encoding": "gzip, deflate",
                "Accept": (
                    "application/json,"
                    "text/html,"
                    "*/*"
                ),
            }
        )

    def _wait_if_needed(self) -> None:
        elapsed = (
            time.monotonic()
            - self.last_request_time
        )

        remaining = (
            self.min_interval_seconds
            - elapsed
        )

        if remaining > 0:
            time.sleep(remaining)

    def get(
        self,
        url: str,
    ) -> requests.Response:

        last_exception: Exception | None = None

        for attempt in range(
            self.max_retries
        ):
            self._wait_if_needed()

            try:
                response = self.session.get(
                    url,
                    timeout=self.timeout,
                )

                self.last_request_time = (
                    time.monotonic()
                )

                retryable = (
                    response.status_code == 429
                    or 500 <= response.status_code < 600
                )

                if retryable:
                    wait_seconds = min(
                        2**attempt,
                        30,
                    )

                    print(
                        f"[retry] HTTP "
                        f"{response.status_code} "
                        f"for {url}; "
                        f"retrying in "
                        f"{wait_seconds}s.",
                        file=sys.stderr,
                    )

                    time.sleep(
                        wait_seconds
                    )

                    continue

                response.raise_for_status()

                return response

            except requests.RequestException as exc:

                last_exception = exc

                wait_seconds = min(
                    2**attempt,
                    30,
                )

                print(
                    f"[retry] {exc}; "
                    f"retrying in "
                    f"{wait_seconds}s.",
                    file=sys.stderr,
                )

                time.sleep(
                    wait_seconds
                )

        raise RuntimeError(
            f"Request failed after "
            f"{self.max_retries} attempts: "
            f"{url}"
        ) from last_exception

    def get_json(
        self,
        url: str,
    ) -> dict[str, Any]:

        response = self.get(url)

        try:
            payload = response.json()

        except ValueError as exc:
            raise RuntimeError(
                f"Invalid JSON response: {url}"
            ) from exc

        if not isinstance(
            payload,
            dict,
        ):
            raise RuntimeError(
                f"Unexpected JSON structure: "
                f"{url}"
            )

        return payload

    def close(self) -> None:
        self.session.close()