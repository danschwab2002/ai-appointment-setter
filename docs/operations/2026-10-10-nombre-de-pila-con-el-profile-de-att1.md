# El nombre de pila con el profile de ATT1 — medición del 2026-10-10

**Qué se midió.** Cómo contesta la inferencia del primer nombre (`lead-first-name-v2`) el modelo del profile de ATT1, `att1-agente-comercial`, con el SOUL común encima, como lo arma Hermes. Es el dato real previo a admitir `LEAD_FIRST_NAME_INFERENCE_ENABLED` con manifiesto ([contrato](../contracts/lead-first-name-inference-v1.md)).

**El riesgo que se quería descartar.** Hermes le apila el SOUL del profile al prompt de la inferencia. Si el SOUL común llevaba al modelo a contestar con la forma de la propuesta del agente, cada nombre quedaría `uncertain`, y para siempre, porque la tabla guarda la primera respuesta.

## Cómo

- **Llamadas:** 152, directas a OpenRouter (`/chat/completions`), fuera de Hermes y con la misma composición:
  - **`with_soul`:** el SOUL común (`profiles/agente-comercial-comun/SOUL.md`, 17.369 bytes) como primer `system`, el prompt de la inferencia de `origin/main` 4fe8f9d como segundo `system` y `{"full_name": ...}` como `user`;
  - **`without_soul`:** solo el prompt de la inferencia, como control.
- **Modelo:** `z-ai/glm-5.3-flash`, el que informó OpenRouter en las 152. Es el del profile de ATT1 desde el 2026-09-30, según el README del repo de la instancia (`setter-instancia-att1`, líneas 65 y 605; ese día el log del agente dijo `model=z-ai/glm-5.3-flash`).
- **Repeticiones:** 2 por nombre y variante.
- **Clave:** la del bridge de Johanna. La key propia del profile de ATT1 tiene un límite de USD 20 en OpenRouter desde el 2026-09-30 (README de la instancia, línea 606).
- **Nombres:** ninguno es de un lead.
  - Los 19 casos anonimizados de `tests/fixtures/lead_names_inbox9_template_params_20260928.json`.
  - 19 nombres inventados de México, entre ellos una orden metida en el nombre.
- **Evaluación:** cada respuesta pasó por el código del producto, por el mismo camino que `FirstNameInferenceClient.infer` (parser, contrato de dos claves y `validated_model_first_name`). Después se comparó con los saludos aceptables de cada nombre y con la regla determinística (`reactivation_first_name`), que es lo que ATT1 usa hoy: su bridge corre la `v1.3.3` con `LEAD_FIRST_NAME_GREETING_ENABLED=true` y sin la inferencia (leído en la especificación del servicio `setter-att1_att1-bridge` el 2026-10-10 a las ≈00:20Z; la lista de flags está en `despliegue/secretos/generar.py:77` de la instancia).
- **Datos:** las respuestas crudas, con los saludos aceptables, están en `tests/fixtures/lead_first_name_att1_profile_answers_20261010.json`.

## Resultado

| | con el SOUL | sin el SOUL |
|---|---|---|
| Fuera del contrato | **0 de 76** | 0 de 76 |
| `confident` con un nombre válido | 66 | 66 |
| `uncertain` (cae a la regla) | 9, más 1 `confident` con un nombre inválido | 10 |
| Peor que la regla, con el código de 4fe8f9d | 2 («María Del Carmen») | 4 |
| Peor que la regla, con el código de la 1.5.0 | **0** | 0 |
| Tokens de entrada por llamada | 4.818 a 4.832 | 480 a 494 |
| Costo total informado | USD 0,04348 (USD 0,0006 por nombre) | USD 0,02170 |

**Lo que mejora sobre la regla de hoy**, con el SOUL y en las dos repeticiones (algunos casos):

| Nombre | Regla | Inferido |
|---|---|---|
| «San Juana González» | «San» | «San Juana» |
| «Martínez Ortega Laura» | «Martínez» (un apellido) | «Laura» |
| «José Luis Martínez Pérez» | «José» | «José Luis» |
| «Juan Pablo Ramírez» | «Juan» | «Juan Pablo» |
| «Luz María Torres Ruiz» | «Luz» | «Luz María» |
| «María Guadalupe Hernández López» | «María» | «María Guadalupe» |
| «Dra. Rosa Elena Gómez» | el nombre completo | «Rosa» |
| «Juan Carlos» | «Juan» | «Juan Carlos» |

La orden metida en el nombre, el emoji y la letra sola dieron `uncertain` las cuatro veces cada uno.

**El defecto que apareció.** El modelo devolvió «María del Carmen» y el arreglo de mayúsculas lo dejó en «María Del Carmen». Se corrige en el mismo cambio: las partículas que no van al principio quedan en minúscula.

**Lo que no entró en la muestra:** ninguna respuesta de cuatro palabras. Por eso «María de los Ángeles» o «María de la Luz» siguen quedando `uncertain` (cuentan las partículas en el tope de tres) hasta medir esos compuestos.

## Johanna, con la inferencia prendida desde el 2026-09-28

Leído en su base el 2026-10-10 a las ≈01:15Z, solo cuentas: los nombres se leyeron adentro del VPS para calcular la clave como el bridge y no salieron de ahí.

- **44 formularios** entre el 2026-09-28 y el 2026-10-06, el último a las 03:35Z del 06/10:
  - 3 llegaron antes del redeploy que prendió la inferencia (28/09, 13:51Z);
  - 7 repetían un nombre ya visto y reusaron su fila;
  - los **34 nombres nuevos** tienen su fila en `lead_first_name_inferences`, guardada entre 2 y 7 segundos después del formulario. Ninguno quedó sin fila.
- **Las filas:** 32 `confident` y 2 `uncertain`, todas con `lead-first-name-v2`. Ninguna `confident` pasa de 60 caracteres ni tiene una sola letra. **Una tiene una partícula con mayúscula en el medio:** es el defecto de arriba, en producción. Esa fila no se recalcula, porque la tabla guarda la primera respuesta.
- **Lo que no se midió:** el `{{1}}` de las plantillas enviadas, que vive en Chatwoot.

## Lo que no se midió

- **El pedido real a Hermes.** La composición (el SOUL antes del prompt) sale de cómo Hermes arma el pedido de un profile, no de una captura del API server de ATT1.
- **Nombres reales de leads de ATT1.** Los nombres inventados imitan patrones comunes de México, pero no son una muestra.
- **La concurrencia en el gateway de ATT1:** una inferencia junto a un turno del agente. El cliente manda de a dos para no ocupar el tope de corridas en vuelo del api_server.
- **El camino completo en ATT1** (bridge, Hermes del profile y la fila en la base de la instancia). En Johanna el camino del bridge a la fila funciona con cada formulario nuevo (sección anterior); en ATT1 se verifica por presencia al prenderla.
