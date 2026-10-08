"""Full H.15 ZIP/XML reads of the published monthly effective federal funds rate."""

import codecs
import io
import re
import stat
import zipfile
import zlib
from collections.abc import Callable, Iterator
from datetime import date, datetime
from typing import cast
from xml.etree import ElementTree as ET

from market_intelligence.macro._http import MacroTransport
from market_intelligence.macro.models import (
    MacroError,
    MacroErrorCode,
    MacroSeries,
    MonthlyObservation,
    MonthlyWindow,
    ProviderRead,
    decimal_text,
)

FED_URL = "https://www.federalreserve.gov/datadownload/Output.aspx?rel=H15&filetype=zip"
DATA_MEMBER = "H15_data.xml"
MAX_ZIP_BYTES = 20_000_000
MAX_XML_BYTES = 90_000_000
MAX_ARCHIVE_BYTES = 100_000_000
MAX_MEMBERS = 10
MAX_ELEMENTS = 2_000_000
READ_BYTES = 65_536
MESSAGE = "http://www.SDMX.org/resources/SDMXML/schemas/v1_0/message"
COMMON = "http://www.SDMX.org/resources/SDMXML/schemas/v1_0/common"
FRB = "http://www.federalreserve.gov/structure/compact/common"
SERIES_NS = "http://www.federalreserve.gov/structure/compact/H15_H15"
SERIES_ATTRIBUTES = {
    "SERIES_NAME": "RIFSPFF_N.M",
    "FREQ": "129",
    "INSTRUMENT": "FF",
    "MATURITY": "O",
    "CURRENCY": "NA",
    "UNIT": "Percent:_Per_Year",
    "UNIT_MULT": "1",
}


def parse_fed(
    payload: bytes,
    window: MonthlyWindow,
    *,
    check_deadline: Callable[[], None] = lambda: None,
) -> tuple[tuple[MonthlyObservation, ...], str, tuple[tuple[str, str], ...]]:
    """Parse to the closing document; discarded series never become macro observations."""
    try:
        check_deadline()
        if len(payload) > MAX_ZIP_BYTES:
            raise ValueError("Oversized archive")
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = archive.infolist()
            names = [info.filename for info in members]
            if (
                not 1 <= len(members) <= MAX_MEMBERS
                or len(names) != len(set(names))
                or names.count(DATA_MEMBER) != 1
                or sum(info.file_size for info in members) > MAX_ARCHIVE_BYTES
                or any(
                    not re.fullmatch(r"[A-Za-z0-9_.-]+\.(?:xml|xsd)", info.filename)
                    or info.flag_bits & 1
                    or stat.S_ISLNK(info.external_attr >> 16)
                    or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                    for info in members
                )
            ):
                raise ValueError("Unexpected archive inventory")
            if archive.getinfo(DATA_MEMBER).file_size > MAX_XML_BYTES:
                raise ValueError("Oversized data member")
            with archive.open(DATA_MEMBER) as stream:
                parser = ET.XMLPullParser(events=("start", "end"))
                decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
                stack: list[ET.Element] = []
                total = elements = selected_count = header_count = 0
                selected: ET.Element | None = None
                observations: list[MonthlyObservation] = []
                seen: set[date] = set()
                prepared: str | None = None
                release_id: str | None = None
                annotations: list[tuple[str, str]] = []
                carry = prefix = ""
                while True:
                    check_deadline()
                    chunk = stream.read(READ_BYTES)
                    total += len(chunk)
                    if total > MAX_XML_BYTES:
                        raise ValueError("Oversized decompressed XML")
                    text = decoder.decode(chunk, final=not chunk)
                    guarded = carry + text
                    if (
                        "\x00" in text
                        or "<!DOCTYPE" in guarded.upper()
                        or "<!ENTITY" in guarded.upper()
                    ):
                        raise ValueError("Unsafe XML")
                    carry = guarded[-16:]
                    prefix = (prefix + text)[:512]
                    declaration = re.search(
                        r"<\?xml\b[^?]*\bencoding\s*=\s*['\"]([^'\"]+)['\"]", prefix
                    )
                    if declaration and declaration[1].upper() != "UTF-8":
                        raise ValueError("Expected UTF-8 data XML")
                    parser.feed(text)
                    events = cast(Iterator[tuple[str, ET.Element]], parser.read_events())
                    for event, node in events:
                        check_deadline()
                        if event == "start":
                            elements += 1
                            if elements > MAX_ELEMENTS or len(stack) >= 32:
                                raise ValueError("Excessive XML work or depth")
                            if not stack and node.tag != f"{{{MESSAGE}}}MessageGroup":
                                raise ValueError("Wrong release envelope")
                            if stack and stack[-1].tag in {
                                f"{{{MESSAGE}}}Prepared",
                                f"{{{MESSAGE}}}ID",
                            }:
                                raise ValueError("Nested release metadata")
                            if selected is not None:
                                children = {
                                    f"{{{SERIES_NS}}}Series": {
                                        f"{{{FRB}}}Obs",
                                        f"{{{FRB}}}Annotations",
                                    },
                                    f"{{{FRB}}}Annotations": {f"{{{COMMON}}}Annotation"},
                                    f"{{{COMMON}}}Annotation": {
                                        f"{{{COMMON}}}AnnotationType",
                                        f"{{{COMMON}}}AnnotationText",
                                    },
                                }
                                if node.tag not in children.get(stack[-1].tag, set()):
                                    raise ValueError("Unsupported selected-series structure")
                                if node.tag != f"{{{FRB}}}Obs" and node.attrib:
                                    raise ValueError("Unexpected annotation attributes")
                            if node.tag == f"{{{MESSAGE}}}Header":
                                if len(stack) != 1:
                                    raise ValueError("Misplaced header")
                                header_count += 1
                                if header_count != 1:
                                    raise ValueError("Duplicate header")
                            if node.attrib.get("SERIES_NAME") == MacroSeries.FED_FUNDS.value:
                                selected_count += 1
                                if (
                                    selected_count != 1
                                    or node.tag != f"{{{SERIES_NS}}}Series"
                                    or node.attrib != SERIES_ATTRIBUTES
                                    or len(stack) != 2
                                    or stack[-1].tag != f"{{{FRB}}}DataSet"
                                    or stack[-1].attrib.get("id") != "H15"
                                ):
                                    raise ValueError(
                                        "Ambiguous selected series or changed metadata"
                                    )
                                selected = node
                            stack.append(node)
                            continue
                        parent = stack[-2] if len(stack) >= 2 else None
                        if parent is not None and parent.tag == f"{{{MESSAGE}}}Header":
                            if node.tag == f"{{{MESSAGE}}}Prepared":
                                if prepared is not None or not isinstance(node.text, str):
                                    raise ValueError("Invalid prepared metadata")
                                prepared = node.text
                                if not re.fullmatch(
                                    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}",
                                    prepared,
                                ):
                                    raise ValueError("Changed prepared timestamp semantics")
                                datetime.fromisoformat(prepared)
                            elif node.tag == f"{{{MESSAGE}}}ID":
                                if release_id is not None or node.text != "H15":
                                    raise ValueError("Wrong release identity")
                                release_id = node.text
                        if selected is not None and node.tag == f"{{{COMMON}}}Annotation":
                            if parent is None or parent.tag != f"{{{FRB}}}Annotations":
                                raise ValueError("Misplaced selected annotation")
                            fields = {child.tag: child.text for child in node}
                            if len(node) != 2 or set(fields) != {
                                f"{{{COMMON}}}AnnotationType",
                                f"{{{COMMON}}}AnnotationText",
                            }:
                                raise ValueError("Invalid selected annotation")
                            kind, content = (
                                fields[f"{{{COMMON}}}AnnotationType"],
                                fields[f"{{{COMMON}}}AnnotationText"],
                            )
                            if (
                                not isinstance(kind, str)
                                or not 1 <= len(kind) <= 100
                                or not isinstance(content, str)
                                or not 1 <= len(content) <= 4000
                                or len(annotations) >= 20
                                or any(label == kind for label, _ in annotations)
                            ):
                                raise ValueError("Ambiguous or oversized annotation")
                            annotations.append((kind, content))
                        elif selected is not None and parent is selected:
                            if node.tag == f"{{{FRB}}}Obs":
                                if (
                                    len(node)
                                    or (node.text and node.text.strip())
                                    or set(node.attrib)
                                    != {
                                        "OBS_STATUS",
                                        "OBS_VALUE",
                                        "TIME_PERIOD",
                                    }
                                ):
                                    raise ValueError("Unexpected observation fields")
                                native = node.attrib["TIME_PERIOD"]
                                if node.attrib["OBS_STATUS"] != "A" or not re.fullmatch(
                                    r"[0-9]{4}-[0-9]{2}-[0-9]{2}", native
                                ):
                                    raise ValueError("Unverified observation status or label")
                                label = date.fromisoformat(native)
                                month = label.replace(day=1)
                                observation = MonthlyObservation(
                                    MacroSeries.FED_FUNDS,
                                    month,
                                    native,
                                    decimal_text(node.attrib["OBS_VALUE"]),
                                )
                                if month in seen or len(seen) >= 10000:
                                    raise ValueError("Duplicate or oversized monthly history")
                                seen.add(month)
                                if window.start <= month < window.end:
                                    observations.append(observation)
                            elif node.tag != f"{{{FRB}}}Annotations":
                                raise ValueError("Unsupported selected-series child")
                        if node is selected:
                            selected = None
                        stack.pop()
                        # Keep only annotation children until their text has been collected.
                        in_annotation = selected is not None and any(
                            ancestor.tag == f"{{{COMMON}}}Annotation" for ancestor in stack
                        )
                        if not in_annotation:
                            node.clear()
                            if parent is not None:
                                parent.remove(node)
                    if not chunk:
                        break
                parser.close()
                check_deadline()
                if (
                    stack
                    or header_count != 1
                    or release_id != "H15"
                    or prepared is None
                    or selected_count != 1
                    or not seen
                ):
                    raise ValueError("Incomplete release or missing monthly series")
                return (
                    tuple(sorted(observations, key=lambda row: row.month)),
                    prepared,
                    tuple(annotations),
                )
    except (
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
        RecursionError,
        ET.ParseError,
        zipfile.BadZipFile,
        zlib.error,
        NotImplementedError,
        RuntimeError,
        EOFError,
    ):
        raise MacroError(MacroErrorCode.INVALID_PAYLOAD) from None


class FedClient(MacroTransport):
    def fetch(self, window: MonthlyWindow, *, deadline: float) -> ProviderRead:
        payload, started, received = self._request(
            "GET",
            FED_URL,
            max_bytes=MAX_ZIP_BYTES,
            deadline=deadline,
        )
        observations, prepared, annotations = parse_fed(
            payload,
            window,
            check_deadline=lambda: self.check_deadline(deadline),
        )
        return ProviderRead(
            window,
            observations,
            started,
            received,
            prepared_text=prepared,
            source_annotations=annotations,
        )
