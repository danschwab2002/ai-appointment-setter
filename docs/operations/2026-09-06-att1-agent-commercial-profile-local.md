# Evidencia local — candidato de agente comercial ATT1

- **Fecha:** 2026-09-06
- **Estado:** verificación local completada; integración y despliegue persistente pendientes
- **Alcance:** paquete candidato inerte `agente-comercial`

## Evidencia

- `uv run pytest -q`: suite completa aprobada sobre el candidato corregido.
- `uv run pytest -q tests/test_att1_product_profiles.py`: 10 pruebas focales aprobadas, incluidos inventario exacto, bundle y payload adulterados, rutas inseguras, padre symlink y falla previa a publicación.
- Instalación create-only en directorio temporal: exitosa; hashes e inventario iguales al recibo.
- Permisos del paquete instalado: home `0700` y archivos `0600`.
- Segunda instalación sobre el mismo destino: rechazada con exit `2` sin modificar el destino.
- `HERMES_HOME=<temporal> /opt/hermes/bin/hermes config check`: exitoso sobre el candidato corregido.
- Un probe del API Server del primer candidato produjo intentos auxiliares de
  resolución de proveedores pese a tener cero toolsets habilitados. Esa
  configuración fue rechazada como evidencia de aislamiento y el API Server
  quedó deshabilitado en el candidato corregido.

## Límites de la evidencia

- No se usaron credenciales persistentes.
- No se conectaron Chatwoot, bridge ni Meta.
- El probe inicial no permite afirmar ausencia de conexión con proveedores.
- No se emitieron mensajes ni efectos externos.
- La Brand Voice y la Conversation Release permanecen sin aprobación de la autoridad comercial.
- Esta evidencia no prueba despliegue productivo ni activación comercial.
