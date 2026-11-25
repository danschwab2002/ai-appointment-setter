"""El lector de la lista de un masivo (bridge.bulk_import), puro y sin red.

Porta los casos de `detectar_columnas`, `primer_nombre` y `leer_csv` del
`--selftest` del kit `masivo.py` (masivo del Black de Maribel, 2026-11-22) y
suma JSON y Excel. Los archivos de `tests/fixtures/bulk_import/` son
sinteticos (nombres, mails y telefonos inventados); su README dice de donde
sale cada forma. No hay borde externo: son entradas de archivo.

Los .xlsx que se arman aca adentro (con `zipfile`) prueban solo los bordes
del formato que un escritor real no produce a pedido: una DTD, un indice de
texto compartido roto, la notacion cientifica.
"""

from __future__ import annotations

import ast
import io
import json
import re
import zipfile
from pathlib import Path

import pytest

from bridge import bulk_import
from bridge.bulk_import import (
    BulkImportError,
    detect_columns,
    first_name,
    read_bulk_list,
)

FIXTURES = Path(__file__).parent / "fixtures" / "bulk_import"
GHL_CSV = FIXTURES / "lista_ghl_sintetica_cp1252_punto_y_coma.csv"
JSON_LIST = FIXTURES / "lista_sintetica.json"
OPENPYXL_XLSX = FIXTURES / "lista_sintetica_openpyxl.xlsx"
NUMBERS_XLSX = FIXTURES / "lista_sintetica_numbers.xlsx"


def _members(result) -> list[tuple[str, str | None, str | None]]:
    return [(m.phone, m.first_name, m.email) for m in result.members]


# --- los casos del kit ----------------------------------------------------------------------


def test_kit_csv_case() -> None:
    # masivo.py, selftest(): el CSV falso con ";", un duplicado en otra forma
    # y un numero sin pais.
    text = (
        "First Name;Last Name;Phone;Email\nAna;Uno;+57 300 111 0001;ana@x.co\n"
        "Beto;Dos;3001110002;b@x.co\nAna;Uno;573001110001;\nCami;Tres;601 555;c@x.co\n"
        "Dani;Cuatro;+57 300 111 0004;compra@x.co\nEli;Cinco;+52 55 1111 0005;\n"
    )
    result = read_bulk_list(text.encode(), country_code="57", filename="lista.csv")
    assert result.columns == {"phone": "Phone", "name": "First Name", "email": "Email"}
    assert result.total_rows == 6
    assert result.duplicates == 1
    assert len(result.members) == 4
    assert result.rejected == {"sin_pais": 1}
    assert [m.first_name for m in result.members] == ["Ana", "Beto", "Dani", "Eli"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("maría josé pérez", "María"),
        ("http://x", None),
        ("12345", None),
        ("", None),
        ("Ana", "Ana"),
        (None, None),
        ("A.", None),
        ("o'neil", "O'neil"),
    ],
)
def test_first_name(value: object, expected: str | None) -> None:
    assert first_name(value) == expected


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (["First Name", "Last Name", "Phone", "Email"], ("Phone", "First Name", "Email")),
        (["Nombre completo", "Celular (WhatsApp)", "E-mail"], ("Celular (WhatsApp)", "Nombre completo", "E-mail")),
        (["nombre", "Teléfono", "Correo electrónico"], ("Teléfono", "nombre", "Correo electrónico")),
        (["Contact Name", "Phone Number", "Tags"], ("Phone Number", "Contact Name", None)),
        (["Tags", "Fecha"], (None, None, None)),
    ],
)
def test_detect_columns(headers: list[str], expected: tuple[str | None, ...]) -> None:
    columns = detect_columns(headers)
    assert (columns["phone"], columns["name"], columns["email"]) == expected


# --- los archivos sinteticos ----------------------------------------------------------------


def test_ghl_csv_in_cp1252_with_semicolons() -> None:
    result = read_bulk_list(GHL_CSV.read_bytes(), country_code="57", filename=GHL_CSV.name)
    assert result.format == "csv"
    assert result.columns == {"phone": "Phone", "name": "First Name", "email": "Email"}
    assert result.total_rows == 15  # la linea vacia no cuenta
    assert result.duplicates == 1
    assert result.rejected == {
        "sin_pais": 1,
        "co_no_movil": 1,
        "mx_largo": 1,
        "ar_largo": 1,
        "vacio": 1,
        "no_movil": 1,
        "largo": 1,
        "sin_digitos": 1,
    }
    assert _members(result) == [
        ("573000000001", "Ana", "ana@example.com"),
        ("573000000002", "Beto", "beto@example.com"),
        ("5215500000005", "Eli", None),
        ("541100000006", "Fede", "fede@example.com"),
        ("34612000010", "José", "jose@example.com"),
        ("13055550112", None, None),
    ]


def test_json_list_of_objects() -> None:
    result = read_bulk_list(JSON_LIST.read_bytes(), country_code="57", filename=JSON_LIST.name)
    assert result.format == "json"
    assert result.columns == {"phone": "telefono", "name": "nombre", "email": "correo"}
    assert result.total_rows == 7
    assert result.duplicates == 1  # el mismo movil de Mexico, como texto y como numero
    assert result.rejected == {"vacio": 1, "co_no_movil": 1}  # una lista en vez de texto; un fijo
    assert _members(result) == [
        ("5215500000021", "Nora", "nora@example.com"),
        ("573000000022", "Pía", "pia@example.com"),
        ("543510000023", "Quique", None),  # "no-es-un-mail" no es un mail
        ("573000000024", None, None),
    ]


def test_xlsx_written_by_openpyxl() -> None:
    result = read_bulk_list(OPENPYXL_XLSX.read_bytes(), country_code="57", filename=OPENPYXL_XLSX.name)
    assert result.format == "xlsx"
    assert result.columns == {"phone": "Celular (WhatsApp)", "name": "Nombre completo", "email": "E-mail"}
    assert result.total_rows == 5  # la fila vacia no cuenta; la segunda hoja no se lee
    assert result.duplicates == 1
    assert result.rejected == {"co_no_movil": 1}
    assert _members(result) == [
        ("573000000031", "Valentina", "vale@example.com"),
        ("5215500000032", "Walter", None),
        ("541100000034", "Zoe", None),
    ]


def test_xlsx_exported_by_numbers_with_a_title_row() -> None:
    # Numbers pone el nombre de la tabla en la fila 1 y los encabezados en la 2.
    result = read_bulk_list(NUMBERS_XLSX.read_bytes(), country_code="57", filename=NUMBERS_XLSX.name)
    assert result.columns == {"phone": "Telefono", "name": "Nombre", "email": "Correo"}
    assert result.total_rows == 3
    assert result.duplicates == 1
    assert _members(result) == [
        ("573000000041", "Ana", "ana.numbers@example.com"),  # el mail es texto enriquecido
        ("5215500000042", "Beto", None),
    ]


@pytest.mark.parametrize(
    ("path", "kind"),
    [(GHL_CSV, "csv"), (JSON_LIST, "json"), (OPENPYXL_XLSX, "xlsx"), (NUMBERS_XLSX, "xlsx")],
)
def test_format_from_content_without_a_name(path: Path, kind: str) -> None:
    assert read_bulk_list(path.read_bytes(), country_code="57").format == kind


# --- lo que ve el AE ------------------------------------------------------------------------


@pytest.mark.parametrize("path", [GHL_CSV, JSON_LIST, OPENPYXL_XLSX, NUMBERS_XLSX])
def test_summary_and_repr_never_show_a_phone_or_an_email(path: Path) -> None:
    result = read_bulk_list(path.read_bytes(), country_code="57", filename=path.name)
    summary = result.summary()
    shown = json.dumps(summary, ensure_ascii=False) + repr(result) + repr(result.members)
    assert re.search(r"[0-9]{6,}", shown) is None
    assert "@" not in shown
    for member in result.members:
        assert member.phone not in shown
        assert member.first_name is None or member.first_name not in repr(result.members)
    assert summary["valid"] == len(result.members)
    assert summary["total_rows"] == result.total_rows


def test_summary_counts() -> None:
    summary = read_bulk_list(GHL_CSV.read_bytes(), country_code="57", filename=GHL_CSV.name).summary()
    assert summary == {
        "format": "csv",
        "columns": {"phone": "Phone", "name": "First Name", "email": "Email"},
        "total_rows": 15,
        "valid": 6,
        "duplicates": 1,
        "rejected": {
            "ar_largo": 1,
            "co_no_movil": 1,
            "largo": 1,
            "mx_largo": 1,
            "no_movil": 1,
            "sin_digitos": 1,
            "sin_pais": 1,
            "vacio": 1,
        },
        "with_first_name": 5,
        "with_email": 4,
    }


def test_a_file_without_headers_masks_them_in_the_error() -> None:
    data = b"Ana,+57 300 000 0001,ana@example.com\nBeto,+57 300 000 0002,\n"
    with pytest.raises(BulkImportError) as caught:
        read_bulk_list(data, country_code="57", filename="sin-encabezado.csv")
    assert caught.value.code == "sin_columna_telefono"
    assert caught.value.headers == ("Ana", "<num>", "<mail>")
    assert "0001" not in str(caught.value) + repr(caught.value.headers)


# --- CSV ------------------------------------------------------------------------------------


@pytest.mark.parametrize("delimiter", [",", ";", "\t", "|"])
def test_csv_delimiters(delimiter: str) -> None:
    lines = [["Nombre", "Telefono"], ["Ana, la primera", "+57 300 000 0001"], ["Beto", "+52 55 0000 0002"]]
    buffer = io.StringIO()
    import csv

    csv.writer(buffer, delimiter=delimiter).writerows(lines)
    result = read_bulk_list(buffer.getvalue().encode(), country_code="57", filename="l.csv")
    assert _members(result) == [("573000000001", "Ana", None), ("5215500000002", "Beto", None)]


def test_csv_utf8_with_bom_and_a_title_line() -> None:
    data = "﻿Inscritas al vivo\nNombre,Teléfono\nÁngela,3000000001\n".encode()
    result = read_bulk_list(data, country_code="57", filename="l.csv")
    assert result.columns["phone"] == "Teléfono"
    assert _members(result) == [("573000000001", "Ángela", None)]


def test_forced_columns() -> None:
    data = b"Nombre,Telefono,Otro telefono\nAna,+57 300 000 0001,+57 300 000 0009\n"
    result = read_bulk_list(data, country_code="57", filename="l.csv", phone_column="Otro telefono")
    assert result.columns["phone"] == "Otro telefono"
    assert _members(result) == [("573000000009", "Ana", None)]
    with pytest.raises(BulkImportError) as caught:
        read_bulk_list(data, country_code="57", filename="l.csv", email_column="Mail")
    assert caught.value.code == "columna_inexistente"


def test_a_duplicate_fills_the_missing_name_and_email() -> None:
    data = b"Nombre,Telefono,Email\n,+57 300 000 0001,\nAna,3000000001,ana@example.com\n"
    result = read_bulk_list(data, country_code="57", filename="l.csv")
    assert _members(result) == [("573000000001", "Ana", "ana@example.com")]
    assert result.duplicates == 1


def test_the_instance_country_applies_to_national_numbers() -> None:
    data = b"Nombre,Telefono\nAna,55 0000 0001\n"
    assert _members(read_bulk_list(data, country_code="52", filename="l.csv")) == [
        ("5215500000001", "Ana", None)
    ]
    assert read_bulk_list(data, country_code="57", filename="l.csv").rejected == {"sin_pais": 1}


# --- errores --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "filename", "code"),
    [
        (b"", "l.csv", "vacio"),
        (b"Nombre,Telefono\n", "l.csv", "sin_filas"),
        (b"Nombre,Fecha\nAna,hoy\n", "l.csv", "sin_columna_telefono"),
        (b"a,b", "lista.pdf", "formato_desconocido"),
        (b"[{]", "l.json", "json_invalido"),
        (b'{"telefono": "+57 300 000 0001"}', "l.json", "json_no_es_lista"),
        (b'["+57 300 000 0001"]', "l.json", "json_no_es_lista"),
        (b"PK\x03\x04no es un zip", "l.xlsx", "xlsx_invalido"),
        (b"Nombre,Telefono\n\xff\xfe\x00\x00", "l.csv", "codificacion"),
    ],
)
def test_errors(data: bytes, filename: str, code: str) -> None:
    with pytest.raises(BulkImportError) as caught:
        read_bulk_list(data, country_code="57", filename=filename)
    assert caught.value.code == code


def test_size_and_row_limits() -> None:
    data = b"Telefono\n" + b"+57 300 000 0001\n" * 3
    with pytest.raises(BulkImportError) as caught:
        read_bulk_list(data, country_code="57", filename="l.csv", max_bytes=10)
    assert caught.value.code == "demasiado_grande"
    with pytest.raises(BulkImportError) as caught:
        read_bulk_list(data, country_code="57", filename="l.csv", max_rows=2)
    assert caught.value.code == "demasiadas_filas"


# --- bordes del .xlsx -----------------------------------------------------------------------

_WORKBOOK = (
    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    '<sheets><sheet name="Hoja" sheetId="1" r:id="rId1"/></sheets></workbook>'
)
_RELS = (
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships/worksheet" Target="/xl/worksheets/hoja.xml"/></Relationships>'
)


def _xlsx(sheet_data: str, shared: str | None = None, prolog: str = "") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/workbook.xml", _WORKBOOK)
        archive.writestr("xl/_rels/workbook.xml.rels", _RELS)
        archive.writestr(
            "xl/worksheets/hoja.xml",
            prolog + '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetData>{sheet_data}</sheetData></worksheet>",
        )
        if shared is not None:
            archive.writestr(
                "xl/sharedStrings.xml",
                f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">{shared}</sst>',
            )
    return buffer.getvalue()


def test_xlsx_scientific_notation_absolute_target_and_skipped_cells() -> None:
    data = _xlsx(
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1" t="s"><v>1</v></c></row>'
        '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="C2"><v>5.73000000001E+11</v></c></row>'
        '<row r="3"><c r="A3" t="inlineStr"><is><t>Beto</t></is></c><c r="C3"><v>3000000002.0</v></c></row>'
        '<row r="4"><c r="A4" t="b"><v>1</v></c><c r="C4" t="e"><v>#N/A</v></c></row>',
        shared="<si><t>Nombre</t></si><si><t>Celular</t></si><si><r><t>A</t></r><r><t>na</t></r></si>",
    )
    result = read_bulk_list(data, country_code="57", filename="l.xlsx")
    assert result.columns == {"phone": "Celular", "name": "Nombre", "email": None}
    assert _members(result) == [("573000000001", "Ana", None), ("573000000002", "Beto", None)]
    # Un booleano y un error no traen dato: la fila queda vacia y no cuenta.
    assert result.total_rows == 2 and result.rejected == {}


@pytest.mark.parametrize(
    ("sheet", "shared", "prolog"),
    [
        ('<row r="1"><c r="A1" t="s"><v>7</v></c></row>', "<si><t>Telefono</t></si>", ""),
        (
            '<row r="1"><c r="A1" t="inlineStr"><is><t>&x;</t></is></c></row>',
            None,
            '<!DOCTYPE worksheet [<!ENTITY x "Telefono">]>',
        ),
        ("<row><c", None, ""),
    ],
)
def test_xlsx_invalid_parts(sheet: str, shared: str | None, prolog: str) -> None:
    with pytest.raises(BulkImportError) as caught:
        read_bulk_list(_xlsx(sheet, shared, prolog), country_code="57", filename="l.xlsx")
    assert caught.value.code == "xlsx_invalido"


def test_xlsx_without_workbook() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("hola.txt", "no es un libro")
    with pytest.raises(BulkImportError) as caught:
        read_bulk_list(buffer.getvalue(), country_code="57", filename="l.xlsx")
    assert caught.value.code == "xlsx_invalido"


# --- puro -----------------------------------------------------------------------------------


def test_the_module_imports_nothing_that_talks_to_the_network() -> None:
    tree = ast.parse(Path(bulk_import.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {
        "__future__",
        "bridge",
        "collections",
        "csv",
        "dataclasses",
        "decimal",
        "io",
        "json",
        "posixpath",
        "re",
        "unicodedata",
        "xml",
        "zipfile",
    }
