# Listas sinteticas para el lector de masivos (`bridge.bulk_import`)

Todos los archivos de esta carpeta son **sinteticos**: los nombres, mails y telefonos son
inventados (telefonos de digitos repetidos, `300 000 00xx`, `55 0000 00xx`, mails en
`example.com`). Ninguno sale de una lista real ni de una persona.

No son un borde externo (no hay API de por medio): son entradas de archivo. Las formas salen de:

- `lista_ghl_sintetica_cp1252_punto_y_coma.csv`: los encabezados de la exportacion de contactos
  de GHL que uso el kit `masivo.py` el 2026-11-22 (`First Name`, `Last Name`, `Phone`, `Email`,
  `Tags`) y las formas de telefono de su `--selftest`, guardado en cp1252 con `;` y CRLF, como lo
  guarda un Excel en castellano.
- `lista_sintetica.json`: una lista de objetos con claves en castellano, telefonos como texto y
  como numero, un mail invalido y valores anidados.
- `lista_sintetica_openpyxl.xlsx`: escrito con openpyxl 3.1.5 (celdas `inlineStr`, telefonos como
  numero, una fila vacia, una segunda hoja que no se lee) el 2026-11-25.
- `lista_sintetica_numbers.xlsx`: exportado con Numbers (macOS) a "Microsoft Excel" el 2026-11-25
  desde un CSV de tres filas: `sharedStrings.xml`, texto enriquecido en el mail y la fila de
  titulo que Numbers pone arriba de los encabezados.
