# automation-expert-results-v1: el resumen de resultados para el Automation Expert

Encargo E-573 (etapa-1, macro 3), 2026-11-15. Para que el Automation Expert conteste «cómo venimos» con los números de `resultados/v1` (E-507, `src/bridge/results_read.py`), sin que alguien corra el tablero del OS a mano.

## La ruta

`GET /internal/automation-expert/v1/resultados/resumen`, con el mismo token de lectura que el resto de las rutas del Automation Expert (`AUTOMATION_EXPERT_READ_TOKEN`) y el mismo cupo de pedidos en vuelo.

La registra `results_read.register`: `app.py` no cambia.

Parámetros (todos opcionales):

- `dias`: de 1 a 90. Los últimos N días de la zona de la instancia hasta `hasta` o hasta hoy, con el día de hoy incluido y parcial. Sin nada, 7.
- `desde` y `hasta`: `AAAA-MM-DD`, días enteros de la zona de la instancia. Un `hasta` futuro se recorta a hoy: los días que no pasaron no cuentan para el largo, el tope ni la comparación.
- `desde` y `dias` juntos no van: 422 `window_conflict`.

Además de la ventana pedida, lee la anterior: el mismo número de días, pegada antes. Si hoy va parcial, la anterior se corta en el mismo punto (las mismas horas desde su comienzo), para no comparar un período a medias con uno entero. Son dos llamadas a `read_results_v1` con la misma vara (`venta_recuperada_v1`, 7 días de atribución, piso de 15 minutos) y las mismas pruebas excluidas (`AUTOMATION_EXPERT_RESULTS_TEST_PREFIXES`). El costo de Meta se pide solo para la ventana actual (con el caché de una hora de `/resultados`) y con un tope de 5 s: el plugin espera 15 s en total.

## Los flags

- `AUTOMATION_EXPERT_RESULTS_ENABLED` (el de `/resultados`): sin este, la ruta no existe. La configuración de `/resultados` declara las pruebas que no cuentan, y el resumen no tiene otra.
- `AUTOMATION_EXPERT_RESULTS_SUMMARY_ENABLED`: el de esta ruta, apagado por defecto.

Con cualquiera de los dos apagado, la ruta no se registra: 404 y no se llama a la base. Prender este flag no cambia la respuesta de `/resultados`.

## La respuesta

```json
{
  "contract": "automation-expert-results-v1",
  "contract_version": "1.0.0",
  "fuente": "resultados/v1",
  "regla": "venta_recuperada_v1",
  "instancia": "<tenant_ref>",
  "zona": "America/Mexico_City",
  "periodo": {"desde": "AAAA-MM-DD", "hasta": "AAAA-MM-DD", "dias": 7, "parcial": true,
              "since": "<UTC>", "until": "<UTC>"},
  "numeros": {"ventas_recuperadas": 0, "ventas_directas": 0, "ventas_atribuibles": 0,
              "neto_productor_usd": 0.0, "neto_directas_usd": 0.0, "neto_atribuibles_usd": 0.0,
              "compras_aprobadas": 0, "envios_aceptados": 0, "costo_meta_usd": null},
  "por_flujo": [{"flujo": "carrito", "ejecutor": "legado", "envios_aceptados": 0, "ventas": 0, "neto_usd": 0.0}],
  "flujos_omitidos": 0,
  "ventas_sin_flujo": 0,
  "anterior": {"periodo": {"...": "..."}, "numeros": {"ventas_recuperadas": 0, "...": "..."}},
  "lineas": ["Del 06/11 al 12/11 (hoy va parcial): ..."],
  "gaps": ["conversaciones", "costo_modelo"]
}
```

- `contract_version` va con major 1 a propósito: el plugin de lectura del Automation Expert rechaza otro major (`CONTRACT_MAJOR` en `profiles/automation-expert/plugins/automation-expert-read/tools.py`).
- `numeros` sale del `resumen` de `/resultados` de la misma ventana; `envios_aceptados` es la suma de los envíos aceptados de los flujos. `compras_aprobadas` cuenta todas las compras aprobadas de la ventana, recuperadas o no, sin las pruebas.
- `por_flujo`: hasta 10, por ventas y después por envíos. Los que quedan afuera se cuentan en `flujos_omitidos`, y una venta cuyo flujo no vino en la lista, en `ventas_sin_flujo` (y en una línea): nada se recorta en silencio.
- `anterior` es `null` si su lectura falló; entonces `gaps` suma `periodo_anterior_no_leido` y `lineas` lo dice. Una falla de la ventana actual es 503, como en `/resultados`.
- `lineas`: el resumen ya escrito en castellano, con los montos como se dicen (`USD 1.234,56`) y sin códigos. El agente las dice tal cual en vez de hacer cuentas.
- `gaps`: los de `faltan` de `/resultados` (`conversaciones`, `costo_modelo`, `costo_meta`, `monto_de_alguna_venta`) más `periodo_anterior_no_leido`. El agente los nombra y no los rellena.

## Datos personales

No sale ninguna venta suelta: ni el pedazo de su id, ni la fecha, el país, la moneda, el precio o el medio de pago. Solo totales y totales por flujo. Lo que devuelve el bridge vive solo en la sesión del agente, como el resto de automation-expert-read-v1.

## Errores

- 404: alguno de los dos flags apagado.
- 401 `automation_expert_authentication_required`: sin el Bearer o con otro.
- 422: `dias_invalid`, `window_conflict`, `desde_invalid`, `hasta_invalid`, `window_empty`, `window_too_long` (más de 90 días).
- 503 `automation_expert_read_unavailable` (la base) o `automation_expert_busy` (el cupo). El log anota solo `error_type`.

## La herramienta del agente

El plugin de lectura no se toca en este contrato: la herramienta `results_summary` se suma al profile `automation-expert` aparte, con el OK de Dan (vive en `profiles/`). Llama a esta ruta con `dias` o `desde`/`hasta` y devuelve el cuerpo tal cual. Tiene que sumar los 422 de arriba a los códigos que reconoce (`_BRIDGE_ERRORS`): si no, un parámetro mal puesto le llega al agente como `automation_expert_read_unavailable`.
