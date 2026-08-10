# E2E HTTP controlado del opt-out inbound — 2026-08-10

- **Estado:** verificado localmente; no desplegado
- **Canal:** HTTP TCP real sobre loopback, sin efectos externos
- **Datos:** únicamente fixtures ficticios y un JID sintético allowlisted
- **Supabase/Chatwoot/WABA productivos:** no utilizados

## Objetivo

Ejercitar el bridge como servidor ASGI real, incluyendo su lifespan y workers,
para comprobar que una baja inbound firmada alcanza la frontera controlada de
autoridad antes que los dobles de Hermes o reply. El test reutiliza
deliberadamente el mismo doble stateful al reiniciar el bridge: comprueba que el
bridge vuelve a consultar esa autoridad, no persistencia real entre procesos.

## Comando y resultado

```text
uv run pytest -q tests/test_opt_out_http_e2e.py -vv
RESULTADO: 1 passed
```

El test levanta Uvicorn sobre un puerto efímero de `127.0.0.1`, realiza requests
HTTP con `httpx`, termina su thread y cierra su socket conocido, y luego levanta
una segunda instancia con el mismo doble de autoridad de opt-out en memoria.

## Evidencia observada

1. `/health` respondió `200` por TCP real.
2. Una baja inequívoca con firma y timestamp válidos fue admitida con `202`.
3. El worker canónico registró una sola transición en el doble de autoridad.
4. Los dobles inyectados de Hermes y reply recibieron cero llamadas.
5. El replay del mismo delivery devolvió `200 duplicate` sin reprocesar.
6. El mismo mensaje bajo otro delivery fue reconciliado como stop ya aplicado,
   sin una segunda transición.
7. El worker de proyección invocó una vez el doble de macro y registró una
   finalización exitosa en el doble de autoridad; no prueba visibilidad real.
8. Después de detener y reiniciar Uvicorn, un inbound posterior volvió a consultar
   el estado retenido por el mismo doble, sin llamar a los dobles de Hermes/reply.
9. Las dos threads Uvicorn creadas por el test terminaron y sus sockets conocidos
   se cerraron; no se realizó una inspección global de procesos o listeners.

## Relación con evidencia SQL

Este E2E usa dobles stateful en memoria de Supabase, Chatwoot y Hermes para
observar el flujo HTTP y el lifecycle real sin tocar servicios externos. No
demuestra durabilidad ni efectos externos y no reemplaza la evidencia SQL:
`docs/operations/2026-08-09-inbound-opt-out-local.md` registra PostgreSQL 17 real,
ACL efectivas, orden inverso y la carrera opt-out/request-start. Ambas evidencias
juntas cubren fronteras distintas: HTTP/lifecycle y atomicidad/autoridad SQL.

## Límites y próximo gate

No demuestra migraciones desplegadas, PostgREST remoto, macro real de Chatwoot,
inbox WABA, template aprobado ni llegada física a WhatsApp. El próximo gate exige
un entorno remoto con la migración aplicada y configuración real; debe comenzar
con runtime/outbound apagados y sólo usar el contacto de prueba autorizado. Un
envío o cambio productivo requiere autorización separada.
