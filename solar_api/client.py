"""Authenticated HTTP client for the APsystems OpenAPI."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid
from typing import Any

import requests

from .config import get_today
from .reports import get_inverter_energy as build_inverter_report
from .reports import get_system_energy as build_system_report


BASE_URL = "https://api.apsystemsema.com:9282"
SIGNATURE_METHOD = "HmacSHA256"


class APsystemsClient:
    """Client for querying system, inverter, and energy data from APsystems."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        system_id: str,
        *,
        base_url: str = BASE_URL,
        timezone: str | None = None,
        timeout: float = 30,
    ) -> None:
        """Create a client using explicit credentials and system configuration."""
        if not app_id or not app_secret or not system_id:
            raise ValueError("app_id, app_secret, and system_id must not be empty")
        self.app_id = app_id
        self.app_secret = app_secret
        self.system_id = system_id
        self.base_url = base_url.rstrip("/")
        self.timezone = timezone
        self.timeout = timeout

    def _request(
        self, path: str, query: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Send a signed GET request and return APsystems' validated JSON."""
        timestamp = str(int(time.time() * 1000))
        nonce = uuid.uuid4().hex

        # APsystems signs only the final path segment, not the complete URL path.
        signature_path = path.rsplit("/", 1)[-1]
        string_to_sign = (
            f"{timestamp}/{nonce}/{self.app_id}/{signature_path}/GET/"
            f"{SIGNATURE_METHOD}"
        )
        signature = base64.b64encode(
            hmac.new(
                self.app_secret.encode("utf-8"),
                string_to_sign.encode("utf-8"),
                hashlib.sha256,
            ).digest()
        ).decode("ascii")

        try:
            response = requests.get(
                self.base_url + path,
                params=query,
                headers={
                    "X-CA-AppId": self.app_id,
                    "X-CA-Timestamp": timestamp,
                    "X-CA-Nonce": nonce,
                    "X-CA-Signature-Method": SIGNATURE_METHOD,
                    "X-CA-Signature": signature,
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.HTTPError as error:
            details = error.response.text if error.response is not None else str(error)
            status = (
                error.response.status_code
                if error.response is not None
                else "unknown"
            )
            raise RuntimeError(f"APsystems HTTP error {status}: {details}") from error
        except requests.RequestException as error:
            raise RuntimeError(
                f"Could not complete APsystems request: {error}"
            ) from error

        try:
            result = response.json()
        except requests.JSONDecodeError as error:
            raise RuntimeError("APsystems returned invalid JSON") from error
        if not isinstance(result, dict):
            raise RuntimeError("APsystems response is not a JSON object")
        if result.get("code") != 0:
            raise RuntimeError(f"APsystems API returned: {result}")
        return result

    def check_system(self) -> dict[str, Any]:
        """Check system access and return the validated raw details response."""
        result = self._request(
            f"/user/api/v2/systems/details/{self.system_id}"
        )
        data = result.get("data")
        if not isinstance(data, dict) or data.get("sid") != self.system_id:
            raise RuntimeError(
                "System-details response did not match the configured system ID"
            )
        return result

    def get_today_energy(self, *, raw_response: bool = False) -> dict[str, Any]:
        """Return today's system production summary, or the API response if requested."""
        return build_system_report(
            self.system_id,
            self._request,
            raw_json=raw_response,
            today=get_today(self.timezone),
        )

    def get_inverter_energy(
        self,
        *,
        ecu_id: str | None = None,
        raw_response: bool = False,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """Return today's per-inverter production, optionally including API responses."""
        return build_inverter_report(
            self.system_id,
            self._request,
            ecu_id_filter=ecu_id,
            raw_json=raw_response,
            today=get_today(self.timezone),
        )
