"""Streamlit converter: 96-well tube-rack scanner CSV -> SciSure-style scan XML.


from __future__ import annotations

import csv
from datetime import datetime
from html import escape
import io
from pathlib import Path
import re
import xml.etree.ElementTree as ET

REQUIRED_COLUMNS = ("Tube Rack ID", "Tube Position", "2D Barcode", "Status Code")
ROW_LETTERS = "ABCDEFGH"
EXPECTED_WELLS = {f"{letter}{col}" for letter in ROW_LETTERS for col in range(1, 13)}
POSITION_RE = re.compile(r"^([A-Ha-h])0?([1-9]|1[0-2])$")
FILENAME_TIME_RE = re.compile(
    r"(?P<month>\d{1,2})-(?P<day>\d{1,2})-(?P<year>\d{4})"
    r"[._ -](?P<hour>\d{1,2}),(?P<minute>\d{1,2}),(?P<second>\d{1,2})"
)


class ConversionError(ValueError):
    """CSV validation or XML conversion failed."""


def normalize_position(value: str) -> tuple[str, int, int]:
    match = POSITION_RE.fullmatch(value.strip())
    if not match:
        raise ConversionError(f"Invalid well position {value!r}. Expected A01–H12.")
    letter, col_string = match.groups()
    row = ROW_LETTERS.index(letter.upper()) + 1
    col = int(col_string)
    return f"{letter.upper()}{col}", row, col


def check_xml_text(value: str, label: str) -> None:
    """Reject characters disallowed in XML 1.0 and CDATA terminators."""
    for char in value:
        point = ord(char)
        allowed = (
            point in (9, 10, 13)
            or 0x20 <= point <= 0xD7FF
            or 0xE000 <= point <= 0xFFFD
            or 0x10000 <= point <= 0x10FFFF
        )
        if not allowed:
            raise ConversionError(f"{label} contains a character XML cannot represent.")


def load_csv(csv_bytes: bytes, require_full_rack: bool = True) -> tuple[str, list[dict], list[str]]:
    """Return rack ID, sorted well records, and non-fatal warnings."""
    try:
        content = csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ConversionError("CSV must be saved as UTF-8.") from exc

    reader = csv.DictReader(io.StringIO(content, newline=""))
    if not reader.fieldnames:
        raise ConversionError("CSV has no header row.")
    reader.fieldnames = [str(name).strip() for name in reader.fieldnames]
    missing_columns = [name for name in REQUIRED_COLUMNS if name not in reader.fieldnames]
    if missing_columns:
        raise ConversionError("Missing CSV columns: " + ", ".join(missing_columns))
    if len(set(reader.fieldnames)) != len(reader.fieldnames):
        raise ConversionError("CSV has duplicate column names.")

    records: list[dict] = []
    seen_positions: set[str] = set()
    seen_barcodes: set[str] = set()
    rack_ids: set[str] = set()
    nonzero_statuses: list[str] = []

    for line_number, row in enumerate(reader, start=2):
        if None in row or any(value is None for value in row.values()):
            raise ConversionError(f"CSV row {line_number} has the wrong number of columns.")
        if not any(value.strip() for value in row.values()):
            continue

        rack = row["Tube Rack ID"].strip()
        tube = row["2D Barcode"].strip()
        status = row["Status Code"].strip()
        well, r, c = normalize_position(row["Tube Position"])

        if not rack or not tube:
            raise ConversionError(f"CSV row {line_number} ({well}) needs both rack and tube barcodes.")
        check_xml_text(rack, f"Tube Rack ID on CSV row {line_number}")
        check_xml_text(tube, f"2D Barcode on CSV row {line_number}")
        if "]]>" in tube:
            raise ConversionError(f"Tube barcode at {well} contains a CDATA terminator (]]>).")
        if well in seen_positions:
            raise ConversionError(f"Duplicate well position: {well}.")
        if tube in seen_barcodes:
            raise ConversionError(f"Duplicate 2D tube barcode: {tube}.")
        if not status:
            raise ConversionError(f"CSV row {line_number} ({well}) has no Status Code.")
        if status != "0":
            nonzero_statuses.append(f"{well}: {status}")

        rack_ids.add(rack)
        seen_positions.add(well)
        seen_barcodes.add(tube)
        records.append({"well": well, "row": r, "column": c, "barcode": tube})

    if not records:
        raise ConversionError("CSV contains no tube records.")
    if len(rack_ids) != 1:
        raise ConversionError(
            "CSV contains multiple Tube Rack ID values: " + ", ".join(sorted(rack_ids))
            + ". Export one rack per XML file."
        )

    missing_wells = sorted(EXPECTED_WELLS - seen_positions,
                           key=lambda well: (well[0], int(well[1:])))
    if missing_wells and require_full_rack:
        raise ConversionError(
            f"Expected 96 wells; found {len(records)}. Missing: "
            + ", ".join(missing_wells)
        )

    warnings = []
    if missing_wells:
        warnings.append(f"Partial rack: {len(records)}/96 wells. Missing: {', '.join(missing_wells)}")
    if nonzero_statuses:
        warnings.append(
            "CSV has nonzero Status Code values (not included in XML); "
            "verify these tubes in the scanner output: " + ", ".join(nonzero_statuses)
        )

    records.sort(key=lambda record: (record["row"], record["column"]))
    return next(iter(rack_ids)), records, warnings


def scan_time_from_filename(filename: str) -> str | None:
    """Parse filename like '10-13-2026.15,37,34(2).csv' -> '20261013 153734'."""
    match = FILENAME_TIME_RE.search(Path(filename).name)
    if not match:
        return None
    values = {key: int(value) for key, value in match.groupdict().items()}
    try:
        moment = datetime(values["year"], values["month"], values["day"],
                          values["hour"], values["minute"], values["second"])
    except ValueError:
        return None
    return moment.strftime("%Y%m%d %H%M%S")


def validate_scan_time(value: str) -> str:
    if not re.fullmatch(r"\d{8} \d{6}", value):
        raise ConversionError("Scan time must be YYYYMMDD HHMMSS, e.g. 20261013 153734.")
    try:
        datetime.strptime(value, "%Y%m%d %H%M%S")
    except ValueError as exc:
        raise ConversionError("Scan time is not a valid date/time.") from exc
    return value


def build_xml(rack_id: str, records: list[dict], scan_time: str,
              scan_id: int = 1, rack_number: int = 1) -> bytes:
   
    validate_scan_time(scan_time)
    if scan_id < 1 or rack_number < 1:
        raise ConversionError("Scan ID and rack number must be positive integers.")
    if not rack_id:
        raise ConversionError("Tube Rack ID is empty.")
    check_xml_text(rack_id, "Tube Rack ID")

    # Both attributes intentionally come from the same CSV field.
    rack_attribute = escape(rack_id, quote=True)
    lines = [
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>',
        f'<scan scanID="{scan_id}" scanTime="{scan_time}">',
        f'  <rack barcode="{rack_attribute}" number="{rack_number}" '
        f'orientationBarcode="{rack_attribute}">',
    ]
    for record in records:
        barcode = record["barcode"]
        lines.append(
            f'    <container column="{record["column"]}" row="{record["row"]}">'
            f'<![CDATA[{barcode}]]></container>'
        )
    lines += ["  </rack>", "</scan>"]
    xml_bytes = ("\n".join(lines) + "\n").encode("utf-8")

    # Re-parse and validate the generated XML to catch any malformed output.
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ConversionError(f"Generated XML failed parsing: {exc}") from exc
    rack_element = root.find("rack")
    if rack_element is None or any(
        rack_element.attrib.get(field) != rack_id
        for field in ("barcode", "orientationBarcode")
    ):
        raise ConversionError("XML rack barcode attributes differ from the CSV rack ID.")
    if len(root.findall("./rack/container")) != len(records):
        raise ConversionError("XML container count changed during conversion.")
    return xml_bytes


def main() -> None:
    import streamlit as st

    st.set_page_config(page_title="SciSure CSV → XML", page_icon="🧪")
    st.title("SciSure 96-well CSV → XML")
    st.caption("Generates the supplied SciSure example's XML structure; uses the CSV Tube Rack ID "
               "for BOTH barcode and orientationBarcode.")

    uploaded = st.file_uploader("Upload scanner CSV", type=["csv"])
    if uploaded is None:
        st.info("Required CSV columns: Tube Rack ID, Tube Position, 2D Barcode, Status Code.")
        return

    require_full_rack = st.checkbox("Require all 96 wells", value=True)
    try:
        rack_id, records, warnings = load_csv(uploaded.getvalue(), require_full_rack)
    except ConversionError as exc:
        st.error(str(exc))
        return

    st.success(f"CSV validated: {len(records)} unique tubes in rack {rack_id}.")
    st.write(f"**rack barcode:** `{rack_id}`")
    st.write(f"**orientationBarcode:** `{rack_id}`")
    for warning in warnings:
        st.warning(warning)

    parsed_scan_time = scan_time_from_filename(uploaded.name)
    scan_time = st.text_input(
        "Scan time (YYYYMMDD HHMMSS)", value=parsed_scan_time or "",
        help="Detected from filenames like 10-13-2026.15,37,34.csv; otherwise enter actual scan time."
    )
    scan_id = st.number_input("scanID", min_value=1, value=1, step=1)
    rack_number = st.number_input("Rack number", min_value=1, value=1, step=1)

    try:
        xml_bytes = build_xml(rack_id, records, scan_time,
                              int(scan_id), int(rack_number))
    except ConversionError as exc:
        st.error(str(exc))
        return

    preview = "\n".join(xml_bytes.decode("utf-8").splitlines()[:6]) + "\n..."
    st.code(preview, language="xml")
    st.download_button(
        "Download SciSure-style XML",
        data=xml_bytes,
        file_name=f"SciSure_{rack_id}_{scan_time.replace(' ', '_')}.xml",
        mime="application/xml",
    )
    st.caption("XML syntax validated. Actual SciSure import acceptance must be tested in your "
               "configured scanner integration; participant IDs are not included in this scan XML.")


if __name__ == "__main__":
    main()
