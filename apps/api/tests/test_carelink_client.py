"""Tests for the CareLink HTTP client data methods (mocked, no network)."""

import json
import logging
from datetime import UTC, date, datetime, timedelta, timezone

import httpx
import pytest

from src.services.integrations.medtronic.client import (
    US_BASE_URL,
    CareLinkAuthError,
    CareLinkClient,
    CareLinkError,
    CareLinkReportTimeoutError,
    CareLinkTransportError,
)


async def _bearer() -> str:
    return "test-bearer-123"


def _make_client(handler, **kwargs) -> CareLinkClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return CareLinkClient(
        bearer_provider=_bearer,
        client=http,
        poll_interval_seconds=0,  # don't actually sleep in tests
        **kwargs,
    )


async def test_get_availability_parses_range():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/patient/reports/snapshotTimelinesRange"
        assert request.headers["authorization"] == "Bearer test-bearer-123"
        return httpx.Response(
            200,
            json={
                "start": "2012-01-01T00:02:34.000Z",
                "end": "2025-01-31T15:05:47.000Z",
            },
        )

    async with _make_client(handler) as client:
        avail = await client.get_availability()
    assert avail.start == datetime.fromisoformat("2012-01-01T00:02:34+00:00")
    assert avail.end == datetime.fromisoformat("2025-01-31T15:05:47+00:00")


async def test_get_patient_id_tolerant_field():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/patient/users/me"
        return httpx.Response(200, json={"patientId": "50497203", "firstName": "X"})

    async with _make_client(handler) as client:
        assert await client.get_patient_id() == "50497203"


async def test_get_patient_id_is_region_aware_when_both_ids_exist():
    response = {
        "patientId": None,
        "id": "eu-report-id",
        "accountId": "account-record-id",
        "loginId": "login-record-id",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/patient/users/me"
        return httpx.Response(200, json=response)

    async with _make_client(
        handler, base_url="https://carelink.minimed.eu"
    ) as eu_client:
        assert await eu_client.get_patient_id() == "eu-report-id"

    async with _make_client(handler, base_url=US_BASE_URL) as us_client:
        # Preserve the established US fallback order exactly.
        assert await us_client.get_patient_id() == "account-record-id"


async def test_export_csv_full_job_flow():
    """generateReport -> reportStatus (pending then ready) -> reportCsv."""
    captured = {}
    status_calls = {"n": 0}
    csv_payload = "Index,Date,Time\n0,2025/01/31,12:00:00\n"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/patient/reports/generateReport":
            captured["body"] = json.loads(request.content)
            captured["content_type"] = request.headers["content-type"]
            return httpx.Response(200, json={"uuid": "abc-123"})
        if path == "/patient/reports/reportStatus":
            assert request.url.params["uuid"] == "abc-123"
            status_calls["n"] += 1
            # pending on the first poll, ready on the second
            ready = status_calls["n"] >= 2
            return httpx.Response(
                200, json={"status": "COMPLETE" if ready else "GENERATING"}
            )
        if path == "/patient/reports/reportCsv":
            assert request.url.params["uuid"] == "abc-123"
            return httpx.Response(200, text=csv_payload)
        return httpx.Response(404)

    async with _make_client(handler) as client:
        csv_text = await client.export_csv(
            patient_id="50497203",
            start_date=date(2025, 1, 18),
            end_date=date(2025, 1, 31),
        )

    assert csv_text == csv_payload
    assert status_calls["n"] == 2  # polled until ready
    # generateReport body mirrors the observed shape
    body = captured["body"]
    assert body["patientId"] == "50497203"
    assert body["reportFileFormat"] == "CSV"
    assert body["aggregatedCsvEnabled"] is True
    assert body["startDate"] == "2025-01-18"
    assert body["endDate"] == "2025-01-31"
    assert body["reportShowLogbook"] is False
    assert body["dailyDetailReportDays"] == []
    assert "startDatePeriodB" not in body
    assert "endDatePeriodB" not in body
    assert captured["content_type"] == "application/json"


def test_generate_report_body_matches_eu_browser_shape():
    local_time = datetime(
        2026, 11, 2, 23, 2, 7, 123456, tzinfo=timezone(timedelta(hours=1))
    )
    body = CareLinkClient._build_generate_report_body(
        patient_id="eu-report-id",
        start_date=date(2026, 10, 30),
        end_date=date(2026, 11, 2),
        client_time=local_time,
        eu_format=True,
    )

    assert body["clientTime"] == "2026-11-02T23:02:07+01:00"
    assert body["dailyDetailReportDays"] == [
        "2026-11-02",
        "2026-11-01",
        "2026-10-31",
        "2026-10-30",
    ]
    assert body["startDatePeriodB"] == "2026-10-26"
    assert body["endDatePeriodB"] == "2026-10-29"
    assert {
        key: value for key, value in body.items() if key.startswith("reportShow")
    } == {
        "reportShowAdherence": True,
        "reportShowAssessmentAndProgress": True,
        "reportShowBolusWizardFoodBolus": True,
        "reportShowDashBoard": True,
        "reportShowDataTable": False,
        "reportShowDeviceSettings": True,
        "reportShowEpisodeSummary": True,
        "reportShowLogbook": True,
        "reportShowOverview": True,
        "reportShowWeeklyReview": True,
        "reportShowSettingsHistory": False,
        "reportShowInsulinAssessment": False,
    }


def test_generate_report_body_handles_one_day_eu_range():
    body = CareLinkClient._build_generate_report_body(
        patient_id="eu-report-id",
        start_date=date(2026, 11, 2),
        end_date=date(2026, 11, 2),
        client_time=datetime(2026, 11, 2, 12, tzinfo=UTC),
        eu_format=True,
    )

    assert body["dailyDetailReportDays"] == ["2026-11-02"]
    assert body["startDatePeriodB"] == "2026-11-01"
    assert body["endDatePeriodB"] == "2026-11-01"


def test_generate_report_body_defaults_to_local_second_precision_for_eu():
    before = datetime.now().astimezone()
    body = CareLinkClient._build_generate_report_body(
        patient_id="eu-report-id",
        start_date=date(2026, 11, 2),
        end_date=date(2026, 11, 2),
        eu_format=True,
    )
    after = datetime.now().astimezone()

    client_time = datetime.fromisoformat(body["clientTime"])
    assert client_time.microsecond == 0
    assert client_time.utcoffset() == before.utcoffset()
    assert before.replace(microsecond=0) <= client_time <= after.replace(microsecond=0)


async def test_eu_generate_report_uses_charset_content_type():
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/patient/reports/generateReport":
            captured["content_type"] = request.headers["content-type"]
            return httpx.Response(200, json={"uuid": "made-up-job"})
        if request.url.path == "/patient/reports/reportStatus":
            return httpx.Response(204)
        return httpx.Response(200, text="Index;Date;Time\n")

    async with _make_client(handler, base_url="https://carelink.minimed.eu") as client:
        await client.export_csv(
            patient_id="eu-report-id",
            start_date=date(2026, 11, 1),
            end_date=date(2026, 11, 1),
        )

    assert captured["content_type"] == "application/json; charset=UTF-8"


async def test_export_csv_times_out_if_never_ready():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/patient/reports/generateReport":
            return httpx.Response(200, json={"uuid": "abc-123"})
        if request.url.path == "/patient/reports/reportStatus":
            return httpx.Response(200, json={"status": "GENERATING"})
        return httpx.Response(404)

    async with _make_client(handler, poll_max_attempts=3) as client:
        with pytest.raises(CareLinkReportTimeoutError):
            await client.export_csv(
                patient_id="1", start_date=date(2025, 1, 1), end_date=date(2025, 1, 2)
            )


async def test_auth_error_on_401():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    async with _make_client(handler) as client:
        with pytest.raises(CareLinkAuthError):
            await client.get_availability()


async def test_transport_error_is_typed():
    """A true transport failure (DNS/TLS/connection) is wrapped as
    CareLinkTransportError -- the subtype the router maps to 'Unable to reach
    CareLink'. Assert the exact type so a revert to the base CareLinkError (which
    would silently fall through to the 'unexpected response' branch) is caught."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with _make_client(handler) as client:
        with pytest.raises(CareLinkTransportError) as exc_info:
            await client.get_availability()
    assert exc_info.type is CareLinkTransportError


async def test_malformed_compressed_body_is_handled_not_500():
    """A reachable host returning an undecodable body (bad Content-Encoding) is
    always wrapped as a CareLinkError, never an unhandled exception/500. Depending
    on the exact bytes the decode fails either during the response read (-> the
    CareLinkTransportError subclass) or at JSON parse (-> the base CareLinkError);
    both subclass CareLinkError and both map to a 503 at the router, so assert the
    superclass rather than a brittle byte-dependent subtype."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"},
            content=b"\x1f\x8b\x08\x00not-valid-gzip",
        )

    async with _make_client(handler) as client:
        with pytest.raises(CareLinkError):
            await client.get_patient_id()


async def test_reachable_4xx_is_base_error_not_transport():
    """A reachable host returning a non-auth 4xx raises the BASE CareLinkError
    (router -> 'unexpected response'), NOT the transport subtype -- pinning the
    other direction of the split so neither branch can absorb the other."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    async with _make_client(handler) as client:
        with pytest.raises(CareLinkError) as exc_info:
            await client.get_patient_id()
    assert exc_info.type is CareLinkError


async def test_4xx_logs_diagnostic_body_with_sensitive_values_scrubbed(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "message": "Malformed JSON in request body at position 12",
                "field": "clientTime",
                "detail": "Fixture Person used fixture-token-value",
                "patientId": "patient-fixture-id",
                "accountId": "account-fixture-id",
                "name": "Fixture Person",
                "token": "fixture-token-value",
                "reportData": "Index;Date;Sensor Glucose (mmol/L)",
            },
        )

    caplog.set_level(
        logging.WARNING,
        logger="src.services.integrations.medtronic.client",
    )
    async with _make_client(handler) as client:
        with pytest.raises(CareLinkError):
            await client.get_patient_id()

    record = next(r for r in caplog.records if r.message == "CareLink request rejected")
    logged = record.extra_fields["response_body"]
    assert "Malformed JSON in request body at position [number]" in logged
    assert "clientTime" in logged
    assert "patient-fixture-id" not in logged
    assert "account-fixture-id" not in logged
    assert "Fixture Person" not in logged
    assert "fixture-token-value" not in logged
    assert "Sensor Glucose" not in logged
    assert "[redacted]" in logged


async def test_4xx_deeply_nested_json_does_not_escape_diagnostic_logging(caplog):
    nested_body = "[" * 2000 + "]" * 2000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            content=nested_body,
            headers={"content-type": "application/json"},
        )

    caplog.set_level(
        logging.WARNING,
        logger="src.services.integrations.medtronic.client",
    )
    async with _make_client(handler) as client:
        with pytest.raises(CareLinkError):
            await client.get_patient_id()

    record = next(r for r in caplog.records if r.message == "CareLink request rejected")
    assert record.extra_fields["response_body"] == "[unparseable body]"


async def test_4xx_oversized_body_is_not_logged(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="x" * 5000)

    caplog.set_level(
        logging.WARNING,
        logger="src.services.integrations.medtronic.client",
    )
    async with _make_client(handler) as client:
        with pytest.raises(CareLinkError):
            await client.get_patient_id()

    record = next(r for r in caplog.records if r.message == "CareLink request rejected")
    assert record.extra_fields["response_body"] == "[oversized body redacted]"


async def test_4xx_unstructured_body_is_fully_redacted(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="Fixture Person has account short-id")

    caplog.set_level(
        logging.WARNING,
        logger="src.services.integrations.medtronic.client",
    )
    async with _make_client(handler) as client:
        with pytest.raises(CareLinkError):
            await client.get_patient_id()

    record = next(r for r in caplog.records if r.message == "CareLink request rejected")
    assert record.extra_fields["response_body"] == "[non-JSON body redacted]"


async def test_4xx_json_message_with_unkeyed_identity_is_redacted(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "message": "Fixture Person rejected",
                "fixture-person-id": "invalid",
            },
        )

    caplog.set_level(
        logging.WARNING,
        logger="src.services.integrations.medtronic.client",
    )
    async with _make_client(handler) as client:
        with pytest.raises(CareLinkError):
            await client.get_patient_id()

    record = next(r for r in caplog.records if r.message == "CareLink request rejected")
    assert record.extra_fields["response_body"] == '{"message":"[redacted]"}'


async def test_4xx_json_message_with_non_latin_identity_is_redacted(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "王小明"})

    caplog.set_level(
        logging.WARNING,
        logger="src.services.integrations.medtronic.client",
    )
    async with _make_client(handler) as client:
        with pytest.raises(CareLinkError):
            await client.get_patient_id()

    record = next(r for r in caplog.records if r.message == "CareLink request rejected")
    assert record.extra_fields["response_body"] == '{"message":"[redacted]"}'


async def test_status_204_is_treated_ready():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/patient/reports/generateReport":
            return httpx.Response(200, json={"reportUuid": "u9"})  # alt uuid field
        if request.url.path == "/patient/reports/reportStatus":
            return httpx.Response(204)
        if request.url.path == "/patient/reports/reportCsv":
            return httpx.Response(200, text="Index,Date,Time\n")
        return httpx.Response(404)

    async with _make_client(handler) as client:
        out = await client.export_csv(
            patient_id="1", start_date=date(2025, 1, 1), end_date=date(2025, 1, 2)
        )
    assert out.startswith("Index,")


def test_default_base_url_is_us():
    assert US_BASE_URL == "https://carelink.minimed.com"


async def test_rejects_untrusted_base_url():
    with pytest.raises(ValueError, match="untrusted host"):
        CareLinkClient(bearer_provider=_bearer, base_url="https://evil.example.com")


async def test_eu_host_is_allowed():
    # Should not raise (regional EU host under minimed.eu).
    c = CareLinkClient(bearer_provider=_bearer, base_url="https://carelink.minimed.eu")
    await c.aclose()


async def test_retries_on_429_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={})
        return httpx.Response(
            200, json={"start": "2012-01-01T00:00:00Z", "end": "2025-01-31T00:00:00Z"}
        )

    async with _make_client(handler) as client:
        avail = await client.get_availability()
    assert calls["n"] == 2  # retried once after the 429
    assert avail.end.year == 2025


async def test_export_rejects_csv_without_index_header():
    """A too-early/partial reportCsv (no 'Index,' header) must not be accepted
    as a successful (but empty) import."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/patient/reports/generateReport":
            return httpx.Response(200, json={"uuid": "u1"})
        if request.url.path == "/patient/reports/reportStatus":
            return httpx.Response(200, json={"status": "COMPLETE"})
        if request.url.path == "/patient/reports/reportCsv":
            return httpx.Response(200, text="<html>not ready</html>")
        return httpx.Response(404)

    async with _make_client(handler) as client:
        with pytest.raises(CareLinkError, match="Index"):
            await client.export_csv(
                patient_id="1", start_date=date(2025, 1, 1), end_date=date(2025, 1, 2)
            )
