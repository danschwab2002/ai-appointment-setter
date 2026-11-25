"""Read the list of a bulk send: CSV, JSON or Excel, pure and without network.

A bulk send starts from a file someone uploads (a GHL export, a spreadsheet,
a JSON). This module turns its bytes into the people the send can reach: it
finds the phone, name and email columns by their header, normalizes every
phone with ``phones.normalize_bulk_phone`` for the instance's country,
deduplicates by the normalized phone and counts what was left out and why.

It ports ``detectar_columnas``, ``primer_nombre`` and ``leer_csv`` of the
``masivo.py`` kit (the 2026-11-22 Black Pack send in Maribel) and adds JSON and
Excel. An ``.xlsx`` is a zip of XML parts, so it is read with ``zipfile`` and
``xml.etree`` from the standard library: the bridge carries no ``openpyxl``.

Nothing here talks to the network, to the database or to the model.
``BulkListImport.summary()`` is the view for the Automation Expert and for
logs: counts and column names, never a phone, a name or an email. The members
carry the phones, and their ``repr`` hides them.
"""

from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from posixpath import join as posix_join
from posixpath import normpath
from xml.etree import ElementTree

from bridge.phones import BULK_PHONE_OK, normalize_bulk_phone

FORMATS = ("csv", "json", "xlsx")
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_ROWS = 100_000
# What one part of an .xlsx may weigh once unzipped: a 5 MB upload of shared
# strings and a sheet is far below; a zip bomb is far above.
_XLSX_MAX_PART_BYTES = 64 * 1024 * 1024
_XLSX_MAIN_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_XLSX_STRICT_REL = "http://purl.oclc.org/ooxml/officeDocument/relationships"
_CELL_REF = re.compile(r"([A-Za-z]{1,3})([0-9]*)")
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_NAME_OK = re.compile(r"[^\W\d_](?:[^\W\d_]|['’.-]){1,29}", re.UNICODE)
_DELIMITERS = (",", ";", "\t", "|")
_HEADER_SEARCH_ROWS = 10

# The headers each column is recognized by, after ``_header_key`` (lower case,
# no accents, words split by spaces). First an exact match, in this order; then
# a header that has the word or starts with it (``Phone (WhatsApp)``,
# ``Nombre completo``). From ``detectar_columnas`` of the kit.
PHONE_HEADERS = (
    "phone",
    "telefono",
    "celular",
    "whatsapp",
    "movil",
    "mobile",
    "phone number",
    "numero",
)
NAME_HEADERS = ("first name", "firstname", "nombre", "name", "full name", "contact name")
EMAIL_HEADERS = ("email", "correo", "mail", "e mail")


class BulkImportError(ValueError):
    """The file cannot be read as a list. ``code`` says why, without data.

    Codes: ``vacio``, ``demasiado_grande``, ``formato_desconocido``,
    ``codificacion``, ``json_invalido``, ``json_no_es_lista``, ``xlsx_invalido``,
    ``sin_filas``, ``demasiadas_filas``, ``sin_columna_telefono``,
    ``columna_inexistente``. ``headers`` lists the file's headers, with long
    digit runs and emails masked, so a person can pick the right column.
    """

    def __init__(self, code: str, *, headers: Sequence[str] = ()) -> None:
        super().__init__(code)
        self.code = code
        self.headers = tuple(_mask_header(h) for h in headers)


@dataclass(frozen=True)
class BulkListMember:
    """One person of the list. ``repr`` does not show the phone or the email."""

    phone: str = field(repr=False)
    first_name: str | None = field(default=None, repr=False)
    email: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class BulkListRejection:
    """One row that was left out: its number among the data rows (1 is the first
    row after the header), the phone as it was written and the reason (one of
    ``BULK_PHONE_REASONS`` or ``"duplicado"``). ``repr`` hides the phone."""

    row: int
    raw_phone: str = field(repr=False)
    reason: str


DUPLICATE_REASON = "duplicado"


@dataclass(frozen=True)
class BulkListImport:
    format: str
    columns: Mapping[str, str | None]
    total_rows: int
    members: tuple[BulkListMember, ...] = field(repr=False)
    rejected: Mapping[str, int]
    duplicates: int
    # Only with ``collect_rejections``: the rows left out, for the person who
    # uploaded the file to download them (the page of the bulk send). Never
    # for the Automation Expert or a log.
    rejections: tuple[BulkListRejection, ...] = field(default=(), repr=False)

    def summary(self) -> dict[str, object]:
        """The counts, safe to show to the Automation Expert or to log."""
        return {
            "format": self.format,
            "columns": {k: _mask_header(v) if v else None for k, v in self.columns.items()},
            "total_rows": self.total_rows,
            "valid": len(self.members),
            "duplicates": self.duplicates,
            "rejected": dict(sorted(self.rejected.items())),
            "with_first_name": sum(1 for m in self.members if m.first_name),
            "with_email": sum(1 for m in self.members if m.email),
        }


def read_bulk_list(
    data: bytes,
    *,
    country_code: str | None,
    filename: str | None = None,
    phone_column: str | None = None,
    name_column: str | None = None,
    email_column: str | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_rows: int = DEFAULT_MAX_ROWS,
    collect_rejections: bool = False,
) -> BulkListImport:
    """Read a CSV, JSON or XLSX list and return the people it can reach.

    The format comes from the file name (``.csv``, ``.txt``, ``.json``,
    ``.xlsx``) or, without one, from the content (a zip is an .xlsx, a ``[`` is
    JSON, the rest CSV). A column can be forced by its exact header; otherwise
    it is detected. A row whose phone is left out counts under its reason; a
    phone that repeats counts as a duplicate and keeps the first row (its name,
    if the first one had none, comes from the repeat). With
    ``collect_rejections`` every row left out also comes in ``rejections``,
    with the phone as written.
    """
    if not data:
        raise BulkImportError("vacio")
    if len(data) > max_bytes:
        raise BulkImportError("demasiado_grande")
    kind = _detect_format(data, filename)
    if kind == "json":
        headers, rows = _read_json(data)
    else:
        grid = _read_xlsx(data) if kind == "xlsx" else _read_csv(data)
        headers, rows = _split_header(grid, phone_column)
    if not rows:
        raise BulkImportError("sin_filas", headers=headers)
    if len(rows) > max_rows:
        raise BulkImportError("demasiadas_filas")
    columns = detect_columns(
        headers, phone_column=phone_column, name_column=name_column, email_column=email_column
    )
    if columns["phone"] is None:
        raise BulkImportError("sin_columna_telefono", headers=headers)
    rejected: dict[str, int] = {}
    rejections: list[BulkListRejection] = []
    seen: dict[str, dict[str, str | None]] = {}
    duplicates = 0
    for number, row in enumerate(rows, start=1):
        verdict = normalize_bulk_phone(row.get(columns["phone"]), country_code)
        if verdict.reason != BULK_PHONE_OK or verdict.phone is None:
            rejected[verdict.reason] = rejected.get(verdict.reason, 0) + 1
            if collect_rejections:
                rejections.append(BulkListRejection(number, row.get(columns["phone"]) or "", verdict.reason))
            continue
        name = first_name(row.get(columns["name"])) if columns["name"] else None
        if verdict.phone in seen:
            duplicates += 1
            if collect_rejections:
                rejections.append(BulkListRejection(number, row.get(columns["phone"]) or "", DUPLICATE_REASON))
            kept = seen[verdict.phone]
            if not kept["first_name"]:
                kept["first_name"] = name
            if not kept["email"] and columns["email"]:
                kept["email"] = _email(row.get(columns["email"]))
            continue
        seen[verdict.phone] = {
            "first_name": name,
            "email": _email(row.get(columns["email"])) if columns["email"] else None,
        }
    members = tuple(
        BulkListMember(phone=phone, first_name=v["first_name"], email=v["email"])
        for phone, v in seen.items()
    )
    return BulkListImport(
        format=kind,
        columns=columns,
        total_rows=len(rows),
        members=members,
        rejected=rejected,
        duplicates=duplicates,
        rejections=tuple(rejections),
    )


def detect_columns(
    headers: Sequence[str],
    *,
    phone_column: str | None = None,
    name_column: str | None = None,
    email_column: str | None = None,
) -> dict[str, str | None]:
    """``{"phone": header, "name": header, "email": header}``, ``None`` if absent."""
    keys: dict[str, str] = {}
    for header in headers:
        keys.setdefault(_header_key(header), header)

    def pick(forced: str | None, preferred: Sequence[str]) -> str | None:
        if forced:
            if forced not in headers:
                raise BulkImportError("columna_inexistente", headers=headers)
            return forced
        for wanted in preferred:
            if wanted in keys:
                return keys[wanted]
        for wanted in preferred:
            for key, header in keys.items():
                if wanted in key.split() or key.startswith(wanted):
                    return header
        return None

    return {
        "phone": pick(phone_column, PHONE_HEADERS),
        "name": pick(name_column, NAME_HEADERS),
        "email": pick(email_column, EMAIL_HEADERS),
    }


def first_name(value: object) -> str | None:
    """The first word if it looks like a name (2 to 30 letters), capitalized.

    ``None`` for a number, a link or an initial: the template then uses its
    neutral greeting instead of a wrong name.
    """
    words = str(value or "").split()
    if not words:
        return None
    word = words[0].strip(".,;:")
    if len(word) < 2 or _NAME_OK.fullmatch(word) is None:
        return None
    return word[:1].upper() + word[1:].lower()


# --- formats -----------------------------------------------------------------------------------


def _detect_format(data: bytes, filename: str | None) -> str:
    if filename:
        extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if extension in ("csv", "txt", "tsv"):
            return "csv"
        if extension == "json":
            return "json"
        if extension == "xlsx":
            return "xlsx"
        if extension:
            raise BulkImportError("formato_desconocido")
    if data.startswith(b"PK\x03\x04"):
        return "xlsx"
    if data.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"["):
        return "json"
    return "csv"


def _decode(data: bytes) -> str:
    # UTF-8 (with or without BOM) first; then the Windows code page an Excel
    # in Spanish saves a CSV with. Latin-1 is not tried: it decodes anything.
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise BulkImportError("codificacion")


def _read_csv(data: bytes) -> list[list[str]]:
    text = _decode(data)
    if "\x00" in text:
        raise BulkImportError("codificacion")
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    delimiter = _csv_delimiter(first_line, text[:8192])
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    return [[cell.strip() for cell in record] for record in reader if any(c.strip() for c in record)]


def _csv_delimiter(first_line: str, sample: str) -> str:
    # The header line decides: a header rarely has a separator inside, while a
    # phone with spaces or a name with a comma can fool csv.Sniffer.
    counts = {d: first_line.count(d) for d in _DELIMITERS}
    best = max(_DELIMITERS, key=lambda d: counts[d])
    if counts[best] > 0:
        return best
    try:
        return csv.Sniffer().sniff(sample, delimiters="".join(_DELIMITERS)).delimiter
    except csv.Error:
        return ","


def _read_json(data: bytes) -> tuple[list[str], list[dict[str, str]]]:
    try:
        parsed = json.loads(_decode(data))
    except json.JSONDecodeError:
        raise BulkImportError("json_invalido") from None
    if not isinstance(parsed, list):
        raise BulkImportError("json_no_es_lista")
    headers: list[str] = []
    rows: list[dict[str, str]] = []
    for item in parsed:
        if not isinstance(item, dict):
            raise BulkImportError("json_no_es_lista")
        row: dict[str, str] = {}
        for key, value in item.items():
            key = str(key).strip()
            if key not in headers:
                headers.append(key)
            row[key] = _scalar_text(value)
        rows.append(row)
    return headers, rows


def _scalar_text(value: object) -> str:
    if value is None or isinstance(value, (dict, list, bool)):
        return ""
    if isinstance(value, float):
        return _number_text(repr(value))
    return str(value).strip()


def _number_text(text: str) -> str:
    """A number written by a spreadsheet as ``5.73001234567E+11`` or
    ``573001234567.0`` back to its digits; any other text as it is."""
    text = text.strip()
    try:
        number = Decimal(text)
    except InvalidOperation:
        return text
    if not number.is_finite() or number != number.to_integral_value():
        return text
    return str(int(number))


def _read_xlsx(data: bytes) -> list[list[str]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise BulkImportError("xlsx_invalido") from None
    with archive:
        names = set(archive.namelist())
        sheet_path = _xlsx_first_sheet(archive, names)
        shared = _xlsx_shared_strings(archive, names)
        sheet = _xlsx_xml(archive, sheet_path)
    grid: list[list[str]] = []
    for element in sheet.iter():
        if _local(element.tag) != "row":
            continue
        cells: dict[int, str] = {}
        next_column = 0
        for cell in element:
            if _local(cell.tag) != "c":
                continue
            column = _xlsx_column(cell.get("r"), next_column)
            cells[column] = _xlsx_cell(cell, shared)
            next_column = column + 1
        if not any(v.strip() for v in cells.values()):
            continue
        width = max(cells) + 1
        grid.append([cells.get(i, "").strip() for i in range(width)])
    return grid


def _xlsx_xml(archive: zipfile.ZipFile, name: str) -> ElementTree.Element:
    try:
        info = archive.getinfo(name)
    except KeyError:
        raise BulkImportError("xlsx_invalido") from None
    if info.file_size > _XLSX_MAX_PART_BYTES:
        raise BulkImportError("xlsx_invalido")
    try:
        raw = archive.read(name)
    except (zipfile.BadZipFile, OSError, RuntimeError):
        raise BulkImportError("xlsx_invalido") from None
    if len(raw) > _XLSX_MAX_PART_BYTES or b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        # An .xlsx has no DTD: one here is an entity expansion attempt.
        raise BulkImportError("xlsx_invalido")
    try:
        return ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        raise BulkImportError("xlsx_invalido") from None


def _xlsx_first_sheet(archive: zipfile.ZipFile, names: set[str]) -> str:
    workbook = _xlsx_xml(archive, "xl/workbook.xml")
    first = next((e for e in workbook.iter() if _local(e.tag) == "sheet"), None)
    if first is None:
        raise BulkImportError("xlsx_invalido")
    rel_id = first.get(f"{{{_XLSX_MAIN_REL}}}id") or first.get(f"{{{_XLSX_STRICT_REL}}}id")
    if rel_id and "xl/_rels/workbook.xml.rels" in names:
        rels = _xlsx_xml(archive, "xl/_rels/workbook.xml.rels")
        for rel in rels.iter():
            if _local(rel.tag) == "Relationship" and rel.get("Id") == rel_id:
                target = rel.get("Target") or ""
                path = target.lstrip("/") if target.startswith("/") else posix_join("xl", target)
                path = normpath(path)
                if path in names:
                    return path
    raise BulkImportError("xlsx_invalido")


def _xlsx_shared_strings(archive: zipfile.ZipFile, names: set[str]) -> list[str]:
    if "xl/sharedStrings.xml" not in names:
        return []
    root = _xlsx_xml(archive, "xl/sharedStrings.xml")
    return [_xlsx_text(item) for item in root if _local(item.tag) == "si"]


def _xlsx_text(element: ElementTree.Element) -> str:
    # The text of a rich string is spread over runs (<r><t>…</t></r>); the
    # phonetic guide (<rPh>) is not part of it.
    parts: list[str] = []
    for child in element:
        tag = _local(child.tag)
        if tag == "t":
            parts.append(child.text or "")
        elif tag == "r":
            parts.extend(t.text or "" for t in child if _local(t.tag) == "t")
    return "".join(parts)


def _xlsx_cell(cell: ElementTree.Element, shared: list[str]) -> str:
    kind = cell.get("t", "n")
    if kind == "inlineStr":
        inline = next((c for c in cell if _local(c.tag) == "is"), None)
        return _xlsx_text(inline) if inline is not None else ""
    value = next((c.text for c in cell if _local(c.tag) == "v"), None)
    if value is None:
        return ""
    if kind == "s":
        try:
            return shared[int(value)]
        except (ValueError, IndexError):
            raise BulkImportError("xlsx_invalido") from None
    if kind == "n":
        return _number_text(value)
    if kind in ("str", "d"):
        return value
    return ""  # booleans and errors carry no phone, name or email


def _xlsx_column(ref: str | None, fallback: int) -> int:
    match = _CELL_REF.fullmatch(ref or "")
    if match is None:
        return fallback
    index = 0
    for letter in match.group(1).upper():
        index = index * 26 + (ord(letter) - ord("A") + 1)
    return index - 1


def _local(tag: object) -> str:
    return str(tag).rsplit("}", 1)[-1]


# --- helpers -----------------------------------------------------------------------------------


def _split_header(
    grid: Sequence[Sequence[str]], phone_column: str | None
) -> tuple[list[str], list[dict[str, str]]]:
    """The header is the first of the first rows that has a phone column.

    Numbers (and some exports) put a title row above the headers; without a
    row that names a phone, the first row is the header and the caller reports
    ``sin_columna_telefono`` with it.
    """
    if not grid:
        return [], []
    header_index = 0
    for index, record in enumerate(grid[:_HEADER_SEARCH_ROWS]):
        headers = list(record)
        if phone_column is not None:
            if phone_column in headers:
                header_index = index
                break
        elif detect_columns(headers)["phone"] is not None:
            header_index = index
            break
    headers = list(grid[header_index])
    return headers, [_row(headers, record) for record in grid[header_index + 1 :]]



def _row(headers: Sequence[str], record: Iterable[str]) -> dict[str, str]:
    row: dict[str, str] = {}
    for header, value in zip(headers, record, strict=False):
        if header and header not in row:
            row[header] = value
    return row


def _header_key(header: str) -> str:
    text = unicodedata.normalize("NFKD", header or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _email(value: object) -> str | None:
    text = str(value or "").strip().lower()
    return text if _EMAIL.fullmatch(text) else None


def _mask_header(header: str) -> str:
    # A file without a header row has a person's data in the first row.
    masked = re.sub(r"[^\s@]+@[^\s@]+", "<mail>", header)
    return re.sub(r"\+?[0-9][0-9 ().-]{5,}[0-9]", "<num>", masked)
