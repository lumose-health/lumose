"""CareLink Personal HTTP client -- data methods (clean-room).

Built from our own observation of the CareLink Personal web API. Covers the
read/download surface this feature needs:

- ``get_patient_id()``  -> GET /patient/users/me
- ``get_availability()`` -> GET /patient/reports/snapshotTimelinesRange
- ``export_csv(...)``    -> the async CSV-export job:
      POST /patient/reports/generateReport  (returns a job uuid)
   -> GET  /patient/reports/reportStatus?uuid=...  (poll until ready)
   -> GET  /patient/reports/reportCsv?uuid=...      (download the CSV text)

**Auth is injected** as an async ``bearer_provider`` callable, so these data
methods are independent of (and testable without) the session-capture flow,
which is the genuinely uncertain part of this integration. The provider
returns a valid bearer for each call; refreshing it is the provider's job.

Confirmed against a live US account: the auth model, the availability shape
``{"start", "end"}``, the generateReport request body, and the
generateReport->reportStatus->reportCsv sequence. NOT yet confirmed live (so
parsed tolerantly here, to be pinned during the integration spike): the exact
field names in the /users/me and generateReport *responses* and the
reportStatus *response* body. These are marked inline.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlparse

import httpx

from src.logging_config import get_logger
from src.observability import scrub_text

logger = get_logger(__name__)

#: Max retries on HTTP 429 (rate limit) before giving up.
_MAX_RETRIES_429 = 3

#: Reject an absurdly large report rather than parsing it into memory. A real
#: 31-day CGM-dense CSV is well under 1 MB; this is a generous safety net.
_MAX_CSV_BYTES = 25 * 1024 * 1024

#: US CareLink host (confirmed). EU uses a different regional host; pass
#: base_url explicitly for it rather than guessing here.
US_BASE_URL = "https://carelink.minimed.com"

#: Allowed base-url host suffixes. base_url is otherwise injectable, so without
#: this an attacker-influenced region/url could make the client POST the
#: bearer to an arbitrary host (SSRF / credential exfiltration). Medtronic
#: CareLink hosts live under minimed.com (US) / minimed.eu (EU regional).
_ALLOWED_HOST_SUFFIXES = ("minimed.com", "minimed.eu")

# CareLink error bodies can explain a rejected request, but they are controlled
# by a third party and may echo submitted account or report data. Keep only a
# small diagnostic vocabulary and redact every other value before logging.
_ERROR_BODY_READ_LIMIT = 4096
_ERROR_BODY_LOG_LIMIT = 500
_DIAGNOSTIC_ERROR_KEYS = frozenset(
    {"code", "detail", "error", "field", "message", "reason", "status", "title", "type"}
)
_SENSITIVE_ERROR_KEY_PARTS = (
    "account",
    "authorization",
    "cookie",
    "data",
    "email",
    "id",
    "name",
    "patient",
    "report",
    "token",
)
_SENSITIVE_ERROR_TEXT = re.compile(
    r"(?i)\b(?:account|authorization|cookie|email|name|patient|token)\b"
)
_SAFE_DIAGNOSTIC_WORDS = frozenset(
    {
        "access",
        "body",
        "clienttime",
        "denied",
        "error",
        "field",
        "forbidden",
        "format",
        "in",
        "invalid",
        "is",
        "json",
        "malformed",
        "missing",
        "not",
        "request",
        "required",
        "unauthorized",
        "unsupported",
        "value",
    }
)

BearerProvider = Callable[[], Awaitable[str]]

# reportStatus "ready" signals. We observed the generateReport ->
# reportStatus -> reportCsv sequence but not the status response body, so we
# accept a tolerant set of completion markers; to be confirmed live.
_READY_TOKENS = {
    "COMPLETE",
    "COMPLETED",
    "READY",
    "DONE",
    "SUCCESS",
    "SUCCEEDED",
    "FINISHED",
}


class CareLinkError(Exception):
    """Base error for CareLink client calls."""


class CareLinkAuthError(CareLinkError):
    """401/403 from CareLink -- the session/bearer is invalid or expired."""


class CareLinkTransportError(CareLinkError):
    """A network/transport-level failure reaching CareLink (DNS, TLS, connection
    timeout, etc.). Distinguished from ``CareLinkError`` so callers can tell a
    true connectivity failure from a reachable-host that returned a bad
    response (4xx/5xx, unexpected body shape, etc.)."""


class CareLinkReportTimeoutError(CareLinkError):
    """The CSV-export job did not become ready within the poll budget."""


@dataclass
class CareLinkAvailability:
    """Date range of pump data available in the user's CareLink cloud."""

    start: datetime
    end: datetime


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _first(d: dict, *keys: str) -> object | None:
    """First present, truthy value among ``keys`` (tolerant response parsing)."""
    for k in keys:
        v = d.get(k)
        if v:
            return v
    return None


def _is_sensitive_error_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return any(part in normalized for part in _SENSITIVE_ERROR_KEY_PARTS)


def _collect_sensitive_error_values(value: object) -> set[str]:
    """Collect values attached to sensitive keys so echoes can be removed too."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if _is_sensitive_error_key(str(key)) and isinstance(
                child, (str, int, float)
            ):
                rendered = str(child).strip()
                if rendered:
                    found.add(rendered)
            else:
                found.update(_collect_sensitive_error_values(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_collect_sensitive_error_values(child))
    return found


def _sanitize_error_text(value: str, sensitive_values: set[str]) -> str:
    text = " ".join(value.split())
    for sensitive in sorted(sensitive_values, key=len, reverse=True):
        text = text.replace(sensitive, "[redacted]")
    text = scrub_text(text)
    if _SENSITIVE_ERROR_TEXT.search(text):
        return "[redacted]"
    # Numeric body values could be glucose/report data or short account ids.
    # The HTTP status is logged separately, so numbers are not needed here.
    text = re.sub(r"\d+(?:[.,]\d+)?", "[number]", text)
    words = re.findall(r"[A-Za-z]+", text)
    if any(word.lower() not in _SAFE_DIAGNOSTIC_WORDS for word in words):
        return "[redacted]"
    return text


def _sanitize_error_value(
    value: object, sensitive_values: set[str], *, depth: int = 0
) -> object:
    if depth >= 3:
        return "[redacted]"
    if isinstance(value, dict):
        sanitized: dict[str, object] = {}
        for key, child in list(value.items())[:20]:
            rendered_key = str(key)
            if _is_sensitive_error_key(rendered_key):
                sanitized[rendered_key] = "[redacted]"
            elif rendered_key.lower() in _DIAGNOSTIC_ERROR_KEYS:
                sanitized[rendered_key] = _sanitize_error_value(
                    child, sensitive_values, depth=depth + 1
                )
            else:
                # Preserve the body shape for diagnosis, never an unknown value.
                sanitized[rendered_key] = "[redacted]"
        return sanitized
    if isinstance(value, list):
        return [
            _sanitize_error_value(child, sensitive_values, depth=depth + 1)
            for child in value[:5]
        ]
    if isinstance(value, str):
        return _sanitize_error_text(value, sensitive_values)
    if value is None or isinstance(value, bool):
        return value
    return "[number]" if isinstance(value, (int, float)) else "[redacted]"


def _safe_error_body(resp: httpx.Response) -> str:
    """Return a bounded, scrubbed representation of a 4xx response body."""
    try:
        raw = resp.text
    except (ValueError, UnicodeDecodeError):
        return "[unreadable body]"
    if not raw.strip():
        return ""
    if len(raw) > _ERROR_BODY_READ_LIMIT:
        return "[oversized body redacted]"

    content_type = resp.headers.get("content-type", "").lower()
    if "text/csv" in content_type or raw.lstrip().startswith(
        ("Index,Date", "Index;Date")
    ):
        return "[report body redacted]"

    try:
        parsed: object = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        # Free text has no field boundary from which to identify a person name
        # or short account id reliably. Fail closed; JSON strings still parse
        # above and pass through the normal scrubber.
        return "[non-JSON body redacted]"
    else:
        sensitive_values = _collect_sensitive_error_values(parsed)
        sanitized = _sanitize_error_value(parsed, sensitive_values)

    rendered = (
        sanitized
        if isinstance(sanitized, str)
        else json.dumps(sanitized, ensure_ascii=True, separators=(",", ":"))
    )
    if len(rendered) > _ERROR_BODY_LOG_LIMIT:
        return rendered[:_ERROR_BODY_LOG_LIMIT] + " [truncated]"
    return rendered


class CareLinkClient:
    def __init__(
        self,
        *,
        bearer_provider: BearerProvider,
        base_url: str = US_BASE_URL,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 30.0,
        poll_interval_seconds: float = 2.0,
        poll_max_attempts: int = 30,
    ) -> None:
        host = (urlparse(base_url).hostname or "").lower()
        if not any(host == s or host.endswith("." + s) for s in _ALLOWED_HOST_SUFFIXES):
            raise ValueError(
                f"Refusing CareLink base_url with untrusted host {host!r}; "
                f"allowed: {_ALLOWED_HOST_SUFFIXES}"
            )
        self._bearer_provider = bearer_provider
        self._base_url = base_url.rstrip("/")
        self._is_eu_host = host == "minimed.eu" or host.endswith(".minimed.eu")
        self._poll_interval = poll_interval_seconds
        self._poll_max_attempts = poll_max_attempts
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=min(5.0, timeout_seconds),
                read=timeout_seconds,
                write=min(5.0, timeout_seconds),
                pool=min(2.0, timeout_seconds),
            )
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> CareLinkClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def _headers(self) -> dict[str, str]:
        bearer = await self._bearer_provider()
        return {
            "Authorization": f"Bearer {bearer}",
            "Accept": "application/json, text/plain, */*",
        }

    def _check(self, resp: httpx.Response) -> None:
        if 400 <= resp.status_code < 500:
            logger.warning(
                "CareLink request rejected",
                method=resp.request.method,
                path=resp.request.url.path,
                status=resp.status_code,
                response_body=_safe_error_body(resp),
            )
        if resp.status_code in (401, 403):
            raise CareLinkAuthError(
                f"CareLink auth failed ({resp.status_code}); session invalid/expired"
            )
        if resp.status_code >= 400:
            raise CareLinkError(
                f"CareLink request to {resp.request.url.path} failed: "
                f"{resp.status_code}"
            )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        extra_headers: dict[str, str] | None = None,
        **kwargs: object,
    ) -> httpx.Response:
        """Issue a request with retry on 429 (backoff, honoring Retry-After)
        and a single retry on 5xx. 401/403 and other 4xx are not retried."""
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES_429 + 1):
            try:
                headers = await self._headers()
                headers.update(extra_headers or {})
                resp = await self._client.request(
                    method,
                    f"{self._base_url}{path}",
                    headers=headers,
                    json=json,
                    **kwargs,
                )
            except httpx.HTTPError as e:
                raise CareLinkTransportError(
                    f"CareLink network error on {method} {path}: {e}"
                ) from e

            if resp.status_code == 429 and attempt < _MAX_RETRIES_429:
                await asyncio.sleep(self._retry_after_seconds(resp, attempt))
                continue
            if 500 <= resp.status_code < 600 and attempt == 0:
                await asyncio.sleep(0.5 + random.random() * 0.5)
                last_exc = CareLinkError(
                    f"CareLink {resp.status_code} on {method} {path}"
                )
                continue
            self._check(resp)
            return resp
        # Exhausted retries on a retryable status.
        raise last_exc or CareLinkError(f"CareLink request to {path} kept failing")

    def _retry_after_seconds(self, resp: httpx.Response, attempt: int) -> float:
        header = resp.headers.get("Retry-After")
        if header and header.isdigit():
            return min(float(header), 30.0)
        return min(self._poll_interval * (2**attempt), 30.0) + random.random()

    async def _get(self, path: str, **kwargs: object) -> httpx.Response:
        return await self._request("GET", path, **kwargs)

    async def _post(self, path: str, *, json: dict) -> httpx.Response:
        extra_headers = (
            {"Content-Type": "application/json; charset=UTF-8"}
            if self._is_eu_host
            else None
        )
        return await self._request("POST", path, json=json, extra_headers=extra_headers)

    async def get_patient_id(self) -> str:
        """Fetch the patient/account id required by generateReport."""
        data = self._json(await self._get("/patient/users/me"))
        # EU/OUS returns both ``id`` and ``accountId``; generateReport requires
        # ``id``. Preserve the confirmed-US fallback order byte-for-byte.
        keys = (
            ("patientId", "id", "loginId", "accountId")
            if self._is_eu_host
            else ("patientId", "accountId", "loginId", "id")
        )
        pid = _first(data, *keys)
        if pid is None:
            raise CareLinkError("Could not find patient id in /patient/users/me")
        return str(pid)

    async def get_availability(self) -> CareLinkAvailability:
        """Date range of data available in the CareLink cloud."""
        data = self._json(await self._get("/patient/reports/snapshotTimelinesRange"))
        start, end = data.get("start"), data.get("end")
        if not start or not end:
            raise CareLinkError("snapshotTimelinesRange missing start/end")
        try:
            return CareLinkAvailability(start=_parse_iso(start), end=_parse_iso(end))
        except (ValueError, TypeError) as e:
            raise CareLinkError(
                f"Could not parse snapshotTimelinesRange dates ({start!r}, {end!r})"
            ) from e

    async def export_csv(
        self,
        *,
        patient_id: str,
        start_date: date,
        end_date: date,
        client_time: datetime | None = None,
    ) -> str:
        """Run the CSV-export job for [start_date, end_date] and return the CSV.

        ``client_time`` should be the user's *local* time (CareLink may use it
        to resolve the start/end dates to instants; sending UTC could shift the
        range by a day at the edges). Defaults to now() in UTC.

        Raises CareLinkReportTimeoutError if the job doesn't finish in the poll
        budget, CareLinkAuthError on 401/403, CareLinkError otherwise.
        """
        body = self._build_generate_report_body(
            patient_id,
            start_date,
            end_date,
            client_time,
            eu_format=self._is_eu_host,
        )
        gen = self._json(await self._post("/patient/reports/generateReport", json=body))
        uuid = _first(gen, "uuid", "reportUuid", "id")
        if uuid is None:
            raise CareLinkError("generateReport did not return a report uuid")
        uuid = str(uuid)

        for _ in range(self._poll_max_attempts):
            status_resp = await self._get(
                "/patient/reports/reportStatus", params={"uuid": uuid}
            )
            if self._is_report_ready(status_resp):
                break
            await asyncio.sleep(self._poll_interval)
        else:
            raise CareLinkReportTimeoutError(
                f"CSV export job {uuid} not ready after {self._poll_max_attempts} polls"
            )

        csv_resp = await self._get(
            "/patient/reports/reportCsv",
            params={"uuid": uuid, "dMInFileName": "false"},
        )
        if len(csv_resp.content) > _MAX_CSV_BYTES:
            raise CareLinkError(
                f"CareLink reportCsv for job {uuid} is too large "
                f"({len(csv_resp.content)} bytes > {_MAX_CSV_BYTES} cap)"
            )
        text = csv_resp.text
        # Validate it actually looks like a CareLink export. Because the
        # reportStatus "ready" shape is unconfirmed, a too-early/partial fetch
        # would otherwise parse to zero rows and silently under-import.
        # Accept both comma and semicolon (EU-locale) delimited exports -- the
        # parser handles either; this guard must not reject a valid one.
        if "Index," not in text and "Index;" not in text:
            raise CareLinkError(
                f"CareLink reportCsv for job {uuid} did not return a CSV "
                "with an 'Index' header (job may not have been ready)"
            )
        return text

    @staticmethod
    def _build_generate_report_body(
        patient_id: str,
        start_date: date,
        end_date: date,
        client_time: datetime | None = None,
        *,
        eu_format: bool = False,
    ) -> dict:
        """Build the confirmed US body or the EU browser's CSV body."""
        now = client_time or (
            datetime.now().astimezone() if eu_format else datetime.now(UTC)
        )
        body = {
            "clientTime": (
                now.isoformat(timespec="seconds") if eu_format else now.isoformat()
            ),
            "dailyDetailReportDays": [],
            "patientId": patient_id,
            "reportFileFormat": "CSV",
            "aggregatedCsvEnabled": True,
            "reportShowAdherence": False,
            "reportShowAssessmentAndProgress": False,
            "reportShowBolusWizardFoodBolus": False,
            "reportShowDashBoard": False,
            "reportShowDataTable": False,
            "reportShowDeviceSettings": False,
            "reportShowEpisodeSummary": False,
            "reportShowLogbook": False,
            "reportShowOverview": False,
            "reportShowWeeklyReview": False,
            "reportShowSettingsHistory": False,
            "reportShowInsulinAssessment": False,
            "startDate": start_date.strftime("%Y-%m-%d"),
            "endDate": end_date.strftime("%Y-%m-%d"),
        }
        if not eu_format:
            return body

        span_days = max((end_date - start_date).days + 1, 1)
        body["dailyDetailReportDays"] = [
            (end_date - timedelta(days=offset)).strftime("%Y-%m-%d")
            for offset in range(span_days)
        ]
        period_b_end = start_date - timedelta(days=1)
        period_b_start = period_b_end - timedelta(days=span_days - 1)
        body.update(
            {
                "startDatePeriodB": period_b_start.strftime("%Y-%m-%d"),
                "endDatePeriodB": period_b_end.strftime("%Y-%m-%d"),
                "reportShowAdherence": True,
                "reportShowAssessmentAndProgress": True,
                "reportShowBolusWizardFoodBolus": True,
                "reportShowDashBoard": True,
                "reportShowDataTable": False,
                "reportShowDeviceSettings": True,
                "reportShowEpisodeSummary": True,
                "reportShowInsulinAssessment": False,
                "reportShowLogbook": True,
                "reportShowOverview": True,
                "reportShowSettingsHistory": False,
                "reportShowWeeklyReview": True,
            }
        )
        return body

    @staticmethod
    def _is_report_ready(resp: httpx.Response) -> bool:
        """Tolerant readiness check (exact reportStatus body unconfirmed live).

        Treats as ready: an explicit ``status``/``state`` token in the ready
        set, a truthy ``ready``/``completed``/``done`` flag, or an empty 204.
        """
        if resp.status_code == 204:
            return True
        try:
            data = resp.json()
        except ValueError:
            return False
        if not isinstance(data, dict):
            return False
        for key in ("status", "state", "reportStatus"):
            token = data.get(key)
            if isinstance(token, str) and token.strip().upper() in _READY_TOKENS:
                return True
        return any(bool(data.get(k)) for k in ("ready", "completed", "done"))

    @staticmethod
    def _json(resp: httpx.Response) -> dict:
        try:
            data = resp.json()
        except ValueError as e:
            raise CareLinkError(
                f"CareLink returned non-JSON on {resp.request.url.path}"
            ) from e
        if not isinstance(data, dict):
            raise CareLinkError(
                f"CareLink returned unexpected JSON shape on {resp.request.url.path}"
            )
        return data
