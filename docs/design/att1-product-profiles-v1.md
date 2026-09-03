# Paquete de perfiles de producto ATT1 V1

- **Estado:** Implementado como candidato local; no desplegado ni activo
- **Alcance:** empaquetado reproducible de los tres profiles definidos por ADR-0006
- **No implica:** aprobación de Conversation Release, activación comercial, despliegue en EasyPanel ni autorización para efectos externos

Este documento describe un candidato local no integrado. Por esa razón todavía
no modifica la topología vigente de `docs/architecture.md`; esa actualización
corresponde al checkpoint de integración serial y no al empaquetado aislado.

## Resultado implementado

El paquete tenant-scoped vive en `profiles/att1/` y contiene exactamente:

```text
profiles/att1/
├── agente-comercial/
├── automation-expert/
├── client-copilot/
└── att1-product-bundle-v1.json
```

Los nombres instalados son los tres roles canónicos de ADR-0006. El namespace de
fuente evita reemplazar perfiles globales o paquetes de otras aliadas.

`att1-product-bundle-v1.json` fija versión, lista exacta de profiles, tamaño y
SHA-256 de cada archivo. Declara explícitamente que el paquete no incluye
credenciales, capacidad de activación ni exposición de `default`.

## Perfil `agente-comercial`

Es un candidato Hermes v40 con API Server privado y cero toolsets efectivos.
Contiene una Conversation Release `draft_incomplete` y responde únicamente con
la propuesta fallback compatible con el contrato actual del bridge.

No contiene bindings runtime, credenciales, capacidad de proveedor ni autoridad
para activar conversaciones. No inventa facts pendientes de ATT1 ni afirma que
un handoff ocurrió.

## Perfil `automation-expert`

Es un candidato Hermes v40 instalable, pero su release funcional permanece
`not_released`. La API privada puede levantarse para comprobar identidad y salud,
pero no expone herramientas ni autoridad sobre schedulers, secuencias, políticas,
mensajería o profiles.

Hasta que exista el contrato estructurado aceptado y su API determinística,
devuelve únicamente `automation_expert_not_released`.

## Perfil `client-copilot`

El bundle conserva la capacidad acotada ya implementada de revisión de
correlaciones, la actualiza a configuración Hermes v40 dentro del namespace de
ATT1 y habilita únicamente su API Server privada. No incorpora terminal,
filesystem, web, memoria, skills, delegación, cron ni herramientas generales.

Su plugin mantiene los endpoints y aprobación humana existentes. El paquete
continúa con `activation_capability: false`; preparar el profile no configura sus
credenciales ni activa una superficie para el cliente.

## Instalación

`scripts/install_att1_product_profiles.py` instala un profile por vez en un home
vacío y explícito. El stack ejecutará tres instalaciones sobre homes separados.

Invariantes del instalador:

- allowlist cerrada de los tres nombres;
- verificación del hash fijo del bundle y de cada byte fuente;
- lectura de archivos y recorrido de directorios con `O_NOFOLLOW`;
- staging privado con directorios `0700` y archivos `0600`;
- publicación Linux mediante `renameat2(RENAME_NOREPLACE)`;
- rechazo create-only si el destino ya existe;
- retención deliberada del staging privado `0700` ante fallo: no existe cleanup
  automático por nombre que pueda alcanzar un objeto sustituido por otro proceso;
- verificación del inode del parent antes y después de publicar;
- recibo por profile con hashes y versión del bundle.

No existe modo update in-place. Una actualización futura deberá instalar un home
nuevo, validarlo y cambiar la referencia mediante un mecanismo de release/rollback
del stack, sin mezclar estado mutable con artefactos versionados.

Los directorios `.PROFILE.staging-*` retenidos requieren reconciliación manual
con todos los instaladores detenidos. Esta elección prioriza no borrar estado
concurrente sobre la limpieza automática.

## Fronteras pendientes

Este paquete no resuelve ni autoriza:

- aprobación final del contenido comercial;
- conexión del bridge con los profiles;
- modelo/proveedor y credenciales productivas;
- routing del producto o exposición al cliente;
- creación del stack EasyPanel;
- PostgreSQL, redes, volúmenes o dominios;
- activación de Meta o contacto real.

Esos puntos pertenecen a los checkpoints posteriores. La siguiente fase deberá
usar PostgreSQL y primitivas nativas de EasyPanel, según la decisión del usuario.
