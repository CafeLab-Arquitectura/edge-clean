import threading
from datetime import datetime, timezone

import requests
from dateutil.parser import parse

from shared.infrastructure.config import BackendConfig


class BackendError(Exception):
    """Base error for backend interactions."""


class BackendUnavailableError(BackendError):
    """Network failure or 5xx; the operation should be retried later."""


class BackendAuthError(BackendError):
    """Sign-in failed (bad service-account credentials)."""


class BackendRejectedError(BackendError):
    """The backend rejected the payload with a 4xx; retrying won't help."""


def _to_backend_timestamp(value) -> str:
    """Render a datetime as a Jackson ``LocalDateTime`` (UTC, no offset).

    The backend maps ``timestamp`` to ``java.time.LocalDateTime``, which does
    not accept a trailing ``Z``/offset, so we normalize to UTC and drop tzinfo.
    """
    if value is None:
        dt = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        dt = value
    else:
        dt = parse(str(value))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat(timespec="seconds")


class BackendClient:
    """Outbound client to the CafeLab Java backend (service-account / JWT)."""

    def __init__(self, config: BackendConfig | None = None):
        self.config = config or BackendConfig.resolve()
        self._token: str | None = None
        self._user_id: int | None = None
        self._profile_id: int | None = None
        self._lock = threading.Lock()

    def _url(self, path: str) -> str:
        return f"{self.config.base_url.rstrip('/')}{path}"

    def sign_in(self) -> str:
        try:
            response = requests.post(
                self._url("/api/v1/authentication/sign-in"),
                json={
                    "email": self.config.service_email,
                    "password": self.config.service_password,
                },
                timeout=self.config.timeout_seconds,
            )
        except requests.RequestException as error:
            raise BackendUnavailableError(f"sign-in request failed: {error}")

        if response.status_code != 200:
            raise BackendAuthError(
                f"sign-in rejected ({response.status_code}): {response.text}"
            )

        payload = response.json() or {}
        token = payload.get("token")
        if not token:
            raise BackendAuthError("sign-in response did not contain a token")

        raw_user_id = payload.get("id") or payload.get("userId")
        if raw_user_id in (None, ""):
            raise BackendAuthError("sign-in response did not contain a user id")
        try:
            user_id = int(raw_user_id)
        except (TypeError, ValueError):
            raise BackendAuthError(f"sign-in response returned a non-numeric user id: {raw_user_id!r}")

        with self._lock:
            self._token = token
            self._user_id = user_id
        return token

    def _profile_user_id(self) -> int:
        with self._lock:
            profile_id = self._profile_id
        if profile_id is not None:
            return profile_id

        email = self.config.service_email
        if not email:
            raise BackendAuthError("service account email is missing")

        response = self._request("GET", "/api/v1/profiles", params={"email": email})
        if response.status_code != 200:
            if 400 <= response.status_code < 500:
                raise BackendRejectedError(
                    f"profile lookup rejected ({response.status_code}): {response.text}"
                )
            raise BackendUnavailableError(
                f"profile lookup failed ({response.status_code}): {response.text}"
            )

        payload = response.json() or {}
        raw_profile_id = payload.get("id") or payload.get("userId")
        if raw_profile_id in (None, ""):
            raise BackendAuthError("profile lookup did not return a profile id")
        try:
            profile_id = int(raw_profile_id)
        except (TypeError, ValueError):
            raise BackendAuthError(
                f"profile lookup returned a non-numeric profile id: {raw_profile_id!r}"
            )

        with self._lock:
            self._profile_id = profile_id
        return profile_id

    def _auth_headers(self) -> dict:
        with self._lock:
            token = self._token
        if token is None:
            token = self.sign_in()
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        """Send an authenticated request, re-signing once on a 401."""
        try:
            response = requests.request(
                method,
                self._url(path),
                headers=self._auth_headers(),
                timeout=self.config.timeout_seconds,
                **kwargs,
            )
        except requests.RequestException as error:
            raise BackendUnavailableError(f"{method} {path} failed: {error}")

        if response.status_code == 401:
            self.sign_in()
            try:
                response = requests.request(
                    method,
                    self._url(path),
                    headers=self._auth_headers(),
                    timeout=self.config.timeout_seconds,
                    **kwargs,
                )
            except requests.RequestException as error:
                raise BackendUnavailableError(f"{method} {path} retry failed: {error}")

        return response

    def post_telemetry(
        self,
        coffee_lot_id: int,
        temperature,
        humidity,
        recorded_at,
        device_id: str | None = None,
        sequence: int | None = None,
    ) -> dict:
        timestamp_str = _to_backend_timestamp(recorded_at)
        payload = {
            "coffeeLotId": coffee_lot_id,
            "temperature": temperature,
            "humidity": humidity,
            "timestamp": timestamp_str,
            "measuredAt": timestamp_str,
            "sensorId": device_id or "UNKNOWN",
            "sequence": sequence if sequence is not None else 1,
        }
        response = self._request("POST", "/api/v1/telemetry-records", json=payload)

        if response.status_code in (200, 201):
            return response.json()
        if 400 <= response.status_code < 500:
            raise BackendRejectedError(
                f"telemetry rejected ({response.status_code}): {response.text}"
            )
        raise BackendUnavailableError(
            f"telemetry failed ({response.status_code}): {response.text}"
        )

    def get_coffee_lots(self) -> list:
        """List the coffee lots owned by the service account (for lot assignment)."""
        profile_id = self._profile_user_id()
        response = self._request("GET", f"/api/v1/coffee-lots/user/{profile_id}")
        if response.status_code == 200:
            return response.json() or []
        if 400 <= response.status_code < 500:
            raise BackendRejectedError(
                f"coffee-lots rejected ({response.status_code}): {response.text}"
            )
        raise BackendUnavailableError(
            f"coffee-lots failed ({response.status_code}): {response.text}"
        )

    def get_thresholds(self, coffee_lot_id: int) -> dict | None:
        response = self._request(
            "GET", f"/api/v1/environment-thresholds/coffee-lot/{coffee_lot_id}"
        )
        if response.status_code == 404:
            return None
        if response.status_code == 200:
            return response.json()
        if 400 <= response.status_code < 500:
            raise BackendRejectedError(
                f"thresholds rejected ({response.status_code}): {response.text}"
            )
        raise BackendUnavailableError(
            f"thresholds failed ({response.status_code}): {response.text}"
        )
