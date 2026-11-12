# flow-audience-trigger-v1: el disparo por audiencia (fecha, cada, manual)

Versión 0.1.0 (propuesta, E-484, 12/11/2026). Extiende `setter-flujo/v1` (`flow-definition-v1.md`) y el ciclo (`flow-change-cycle-v1.md`). Diseño: hito H6 del mapa «el AE maneja todos los flujos» (vault, `coordinacion/etapa-1/mapa-ae-todos-los-flujos.md` §2), decisiones D6, D7, D8 y D10.

Los valores marcados **DEFAULT_REVISAR_DAN** son los del mapa y los tiene que revisar Dan antes de prender nada.

## Para qué

Hasta la v1.36.0 un flujo nace de una persona: un evento (`carrito`, `pago_fallido`, `compra`…) o un silencio. Los tres disparadores nuevos nacen de un momento, y el «a quién» es una audiencia: un recordatorio de Black a quienes dejaron el formulario, un aviso a las clientas cada lunes, un envío que una persona lanza cuando quiere.

## La forma

```yaml
dispara:
  fecha:  {el: "2026-12-01T10:00", zona: America/Mexico_City}      # una vez
  # o
  cada:   {dias: [lun, jue], hora: "10:00", zona: America/Mexico_City, desde: "2026-12-01", hasta: "2026-12-31"}
  # o
  manual: {}                                                        # lo lanza una persona
para:
  es: prospecta            # o clienta (obligatorio)
  etiquetas: [black-2026]  # de Chatwoot; tiene que tener todas
  excluye: [vip]           # ninguna de estas
  producto: el-protocolo   # con clienta: compró ese producto
  formulario_en: 30d       # con prospecta: dejó el formulario con permiso en los últimos N días (DEFAULT_REVISAR_DAN: 30d)
ritmo:
  por_hora: 200            # DEFAULT_REVISAR_DAN (D10)
  por_ocurrencia: 1000     # DEFAULT_REVISAR_DAN (D10)
entra:
  permiso: formulario      # con prospecta; compra con clienta
```

- `cada` sale una vez por día como mucho (un día de la semana y una hora): es el intervalo mínimo de 24 h del mapa.
- `para` es obligatorio con las tres formas (`audience_requires_para`, regla 2); `para` y `ritmo` con un evento o un silencio son `para_requires_audience_trigger` (regla 2).
- **Quién recibe (D6):** una prospecta solo con `permiso: formulario`; una clienta solo con `permiso: compra`, que además exige el `ok_del_cliente` del ítem y una sola plantilla de utilidad (reglas del ciclo y 17, sin cambios). `conversacion` no alcanza: `audience_permission_mismatch` (regla 3).
- Al normalizar se escriben `ritmo`, `formulario_en` y la repetición `una_por: persona, otra_despues_de: 7d` (DEFAULT_REVISAR_DAN).
- Hasta que el motor nativo admita un recorrido de audiencia, un flujo así vuelve con `motor_nativo:disparador` («todavía no»), como cualquier disparador que el motor no ejecuta.

## Cómo corre (propuesta, se mide en el E2E)

1. **Ocurrencia.** `fecha`: la única, entre su hora y 6 h después. `cada`: una por día local pedido, dentro de `desde`/`hasta`, en la misma gracia. Pasada la gracia no sale tarde. `manual`: la arma una persona desde la página.
2. **Lista congelada.** Al vencer, se materializa la audiencia en `flow_audience_occurrences` + `flow_audience_members`, idempotente por (flujo, versión, ocurrencia): un reinicio no la vuelve a armar. La base vuelve a mirar la baja (`contact_authorizations` denegada) y, con `formulario`, la intención con permiso de WhatsApp en la ventana; quien no pasa queda `omitida` con su motivo.
3. **Confirmación (D7).** El AE nunca dispara. Una ocurrencia `manual` queda `materializada` hasta que una persona autorizada la confirma en la página escribiendo el conteo exacto de la lista y marcando el segundo paso; un conteo distinto no confirma. `fecha` y `cada` quedan confirmadas por la aprobación del flujo al publicarse.
4. **Freno por salud (D8).** Antes de soltar, el barredor lee la última lectura de salud de Meta de la instancia. Rojo o amarillo frena la ocurrencia (`frenada`, con el motivo); una lectura que no se puede leer o con más de 26 h también frena (fail-closed). DEFAULT_REVISAR_DAN: amarillo frena.
5. **Ritmo.** Se sueltan a lo sumo `por_hora` personas por hora; cada una abre un recorrido nativo y desde ahí rige la puerta común (horario, topes, baja, conversación viva, salida `compra`).

## Flags

- `FLOW_AUDIENCE_TRIGGERS_ENABLED` (default `false`): sin él el barredor no lee, no materializa ni suelta nada.

## Historial

- 0.1.0 (12/11/2026, E-484): forma, reglas 2 y 3, la base y el barredor detrás del flag. Sin E2E.
