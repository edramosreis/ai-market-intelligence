import io
import struct
import warnings
import zipfile
from datetime import date
from decimal import Decimal

import httpx
import pytest

from market_intelligence.macro import fed
from market_intelligence.macro.fed import (
    COMMON,
    FED_URL,
    FRB,
    MESSAGE,
    SERIES_NS,
    FedClient,
    parse_fed,
)
from market_intelligence.macro.models import MacroError, MacroErrorCode, MonthlyWindow

WINDOW = MonthlyWindow(date(2024, 1, 1), date(2024, 3, 1))


def observation(label: str = "2024-01-31", value: str = "2.125", status: str = "A") -> str:
    return f'<frb:Obs OBS_STATUS="{status}" OBS_VALUE="{value}" TIME_PERIOD="{label}" />'


def series(rows: str | None = None, *, name: str = "RIFSPFF_N.M") -> str:
    rows = observation() + observation("2024-02-29", "0") if rows is None else rows
    return (
        f'<kf:Series SERIES_NAME="{name}" FREQ="129" INSTRUMENT="FF" MATURITY="O" '
        'CURRENCY="NA" UNIT="Percent:_Per_Year" UNIT_MULT="1">'
        "<frb:Annotations><common:Annotation><common:AnnotationType>Long Description"
        "</common:AnnotationType><common:AnnotationText>Synthetic monthly effective rate"
        "</common:AnnotationText></common:Annotation></frb:Annotations>"
        f"{rows}</kf:Series>"
    )


def document(
    selected: str | None = None, *, tail: str = "", prepared: str = "2024-03-01T15:40:04"
) -> bytes:
    selected = series() if selected is None else selected
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<message:MessageGroup xmlns:message="{MESSAGE}" xmlns:frb="{FRB}" '
        f'xmlns:common="{COMMON}" xmlns:kf="{SERIES_NS}">'
        "<message:Header><message:ID>H15</message:ID>"
        f"<message:Prepared>{prepared}</message:Prepared></message:Header>"
        '<frb:DataSet id="H15">'
        # Unsupported missing semantics in an unrelated daily series are ignored.
        f"{series(observation(value='-9999', status='ND'), name='RIFSPFF_N.B')}"
        f"{selected}{tail}</frb:DataSet></message:MessageGroup>"
    ).encode()


def archive(xml: bytes, extras: tuple[tuple[str, bytes], ...] = ()) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as target:
        target.writestr("H15_data.xml", xml)
        for name, body in extras:
            target.writestr(name, body)
    return out.getvalue()


def test_native_month_end_rate_prepared_text_and_unrelated_series() -> None:
    rows, prepared, annotations = parse_fed(
        archive(document(), (("H15_struct.xml", b"<!DOCTYPE unsafe><not-parsed/>"),)),
        WINDOW,
    )
    assert [(row.month, row.native_period, row.value) for row in rows] == [
        (date(2024, 1, 1), "2024-01-31", Decimal("2.125")),
        (date(2024, 2, 1), "2024-02-29", Decimal("0")),
    ]
    assert prepared == "2024-03-01T15:40:04" and not rows[0].footnotes
    assert annotations == (("Long Description", "Synthetic monthly effective rate"),)


def test_full_history_is_validated_before_window_filter_and_sorted() -> None:
    selected = series(observation("2024-02-29") + observation() + observation("2023-12-31"))
    rows, _, _ = parse_fed(archive(document(selected)), WINDOW)
    assert [row.month for row in rows] == [date(2024, 1, 1), date(2024, 2, 1)]
    outside = MonthlyWindow(date(1990, 1, 1), date(1991, 1, 1))
    assert parse_fed(archive(document(selected)), outside)[0] == ()
    with pytest.raises(MacroError, match="^invalid_payload$"):
        parse_fed(archive(document(series(observation("2023-12-30")))), WINDOW)


@pytest.mark.parametrize(
    "old,new",
    [
        ('FREQ="129"', 'FREQ="9"'),
        ('INSTRUMENT="FF"', 'INSTRUMENT="T"'),
        ('MATURITY="O"', 'MATURITY="Y1"'),
        ('CURRENCY="NA"', 'CURRENCY="USD"'),
        ('UNIT="Percent:_Per_Year"', 'UNIT="Index"'),
        ('UNIT_MULT="1"', 'UNIT_MULT="100"'),
        ('SERIES_NAME="RIFSPFF_N.M"', 'SERIES_NAME="FEDFUNDS"'),
        ("<kf:Series ", '<kf:Series EXTRA="unverified" '),
    ],
)
def test_metadata_changes_require_review(old: str, new: str) -> None:
    with pytest.raises(MacroError, match="^invalid_payload$"):
        parse_fed(archive(document(series().replace(old, new))), WINDOW)


@pytest.mark.parametrize(
    "label,value,status",
    [
        ("2024-01-30", "1", "A"),
        ("2024-02-28", "1", "A"),
        ("2024-13-31", "1", "A"),
        ("2024-1-31", "1", "A"),
        ("1954-06-30", "1", "A"),
        ("2024-01-31", "-9999", "ND"),
        ("2024-01-31", "-9999", "A"),
        ("2024-01-31", "-", "ND"),
        ("2024-01-31", "1", "P"),
        ("2024-01-31", "NaN", "A"),
        ("2024-01-31", "1e2", "A"),
        ("2024-01-31", "1.0000000000000000001", "A"),
    ],
)
def test_unverified_labels_missing_statuses_and_bad_values_fail(
    label: str, value: str, status: str
) -> None:
    with pytest.raises(MacroError, match="^invalid_payload$"):
        parse_fed(archive(document(series(observation(label, value, status)))), WINDOW)


@pytest.mark.parametrize(
    "case",
    [
        "duplicate_series",
        "duplicate_month",
        "empty_series",
        "missing_series",
        "wrong_namespace",
        "wrong_dataset",
        "wrong_release",
        "wrong_root",
        "extra_obs",
        "unknown_child",
        "nested_obs",
        "duplicate_header",
        "duplicate_prepared",
        "missing_prepared",
        "truncated_tail",
        "malformed_tail",
        "bad_annotation",
    ],
)
def test_ambiguous_or_incomplete_documents_fail(case: str) -> None:
    xml = document().decode()
    if case == "duplicate_series":
        xml = document(tail=series()).decode()
    elif case == "duplicate_month":
        xml = document(series(observation() * 2)).decode()
    elif case == "empty_series":
        xml = document(series("")).decode()
    elif case == "missing_series":
        xml = document("").decode()
    elif case == "wrong_namespace":
        xml = xml.replace(SERIES_NS, "https://unverified.test/series")
    elif case == "wrong_dataset":
        xml = xml.replace('id="H15"', 'id="Other"')
    elif case == "wrong_release":
        xml = xml.replace("<message:ID>H15", "<message:ID>Other")
    elif case == "wrong_root":
        xml = xml.replace("message:MessageGroup", "message:Other")
    elif case == "extra_obs":
        xml = document(series(observation().replace("<frb:Obs ", '<frb:Obs EXTRA="1" '))).decode()
    elif case == "unknown_child":
        xml = document(series("<frb:Unknown/>")).decode()
    elif case == "nested_obs":
        xml = document(
            series(
                '<frb:Obs OBS_STATUS="A" OBS_VALUE="1" TIME_PERIOD="2024-01-31">'
                "<frb:Nested/></frb:Obs>"
            )
        ).decode()
    elif case == "duplicate_header":
        xml = xml.replace("</message:Header>", "</message:Header><message:Header/>")
    elif case == "duplicate_prepared":
        xml = xml.replace(
            "</message:Header>",
            "<message:Prepared>2024-03-01T15:40:04</message:Prepared></message:Header>",
        )
    elif case == "missing_prepared":
        xml = xml.replace("<message:Prepared>2024-03-01T15:40:04</message:Prepared>", "")
    elif case == "truncated_tail":
        xml = xml[:-10]
    elif case == "malformed_tail":
        xml = document(tail="<unclosed>").decode()
    else:
        xml = document(series().replace("common:AnnotationText", "common:Unknown")).decode()
    with pytest.raises(MacroError, match="^invalid_payload$"):
        parse_fed(archive(xml.encode()), WINDOW)


@pytest.mark.parametrize(
    "prepared",
    ["2024-03-01T15:40:04Z", "2024-03-01T15:40:04+01:00", "2024-02-30T15:40:04", "2024-03-01"],
)
def test_prepared_is_source_text_with_reviewed_semantics(prepared: str) -> None:
    with pytest.raises(MacroError, match="^invalid_payload$"):
        parse_fed(archive(document(prepared=prepared)), WINDOW)


@pytest.mark.parametrize("case", ["doctype", "entity", "utf16", "encoding", "invalid_utf8"])
def test_data_xml_is_safe_utf8(case: str, monkeypatch: pytest.MonkeyPatch) -> None:
    xml = document()
    if case == "doctype":
        xml = xml.replace(
            b"<message:MessageGroup", b"<!DOCTYPE message:MessageGroup><message:MessageGroup", 1
        )
    elif case == "entity":
        xml = xml.replace(
            b"<message:MessageGroup",
            b'<!ENTITY external SYSTEM "https://private.test"><message:MessageGroup',
            1,
        )
    elif case == "utf16":
        xml = xml.decode().replace("UTF-8", "UTF-16").encode("utf-16")
    elif case == "encoding":
        xml = xml.replace(b"UTF-8", b"ISO-8859-1")
    else:
        xml += b"\xff"
    monkeypatch.setattr(fed, "READ_BYTES", 7)  # Declarations span decompression chunks.
    with pytest.raises(MacroError, match="^invalid_payload$"):
        parse_fed(archive(xml), WINDOW)


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "duplicate",
        "path",
        "members",
        "size",
        "crc",
        "truncated",
        "non_zip",
        "encrypted",
        "compression",
    ],
)
def test_archive_inventory_and_integrity(case: str, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = archive(document())
    if case == "missing":
        payload = payload.replace(b"H15_data.xml", b"H15_fake.xml")
    elif case == "duplicate":
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            payload = archive(document(), (("H15_data.xml", document()),))
    elif case == "path":
        payload = archive(document(), (("../unsafe.xml", b"ignored"),))
    elif case == "members":
        monkeypatch.setattr(fed, "MAX_MEMBERS", 1)
        payload = archive(document(), (("H15_struct.xml", b"ignored"),))
    elif case == "size":
        monkeypatch.setattr(fed, "MAX_ARCHIVE_BYTES", 100)
    elif case == "crc":
        damaged = bytearray(payload)
        central = payload.index(b"PK\x01\x02")
        struct.pack_into("<I", damaged, central + 16, 0)
        payload = bytes(damaged)
    elif case == "truncated":
        payload = payload[:-30]
    elif case == "non_zip":
        payload = b"Private upstream error"
    elif case in {"encrypted", "compression"}:
        damaged = bytearray(payload)
        central = payload.index(b"PK\x01\x02")
        offset, value = (8, 1) if case == "encrypted" else (10, 99)
        struct.pack_into("<H", damaged, central + offset, value)
        payload = bytes(damaged)
    with pytest.raises(MacroError, match="^invalid_payload$"):
        parse_fed(payload, WINDOW)


@pytest.mark.parametrize("limit", ["MAX_ZIP_BYTES", "MAX_XML_BYTES", "MAX_ELEMENTS"])
def test_parser_resource_bounds(limit: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fed, limit, 10)
    with pytest.raises(MacroError, match="^invalid_payload$"):
        parse_fed(archive(document()), WINDOW)


def test_parse_deadline_is_not_success_or_payload_failure() -> None:
    checks = 0

    def deadline() -> None:
        nonlocal checks
        checks += 1
        if checks >= 5:
            raise MacroError(MacroErrorCode.DEADLINE_EXCEEDED)

    with pytest.raises(MacroError, match="^deadline_exceeded$"):
        parse_fed(archive(document()), WINDOW, check_deadline=deadline)


def test_deep_xml_and_symlink_archive_members_fail() -> None:
    deep = document(tail="<frb:Nested>" * 40 + "</frb:Nested>" * 40)
    with pytest.raises(MacroError, match="^invalid_payload$"):
        parse_fed(archive(deep), WINDOW)
    out = io.BytesIO()
    link = zipfile.ZipInfo("linked.xml")
    link.external_attr = 0o120777 << 16
    with zipfile.ZipFile(out, "w") as target:
        target.writestr("H15_data.xml", document())
        target.writestr(link, b"destination")
    with pytest.raises(MacroError, match="^invalid_payload$"):
        parse_fed(out.getvalue(), WINDOW)


def test_client_uses_full_xml_route_and_local_receipt() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and str(request.url) == FED_URL
        assert not request.content and "authorization" not in request.headers
        return httpx.Response(200, content=archive(document()))

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        result = FedClient(http, monotonic=lambda: 0).fetch(WINDOW, deadline=100)
    assert len(result.observations) == 2 and result.prepared_text == "2024-03-01T15:40:04"
    assert result.source_annotations == (("Long Description", "Synthetic monthly effective rate"),)
    assert result.received_at >= result.fetch_started_at
