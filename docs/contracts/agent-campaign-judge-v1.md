# Contrato: el juez de una campaña, pedido por el Automation Expert (agent-campaign-judge-v1)

- **Estado:** propuesto (4.9 de la lista definitiva, F-163; serie Z1 del 2026-11-26). Describe lo que construye este cambio detrás de un flag apagado; no dice que esté desplegado ni prendido en ninguna instancia.
- **Contrato:** `agent-campaign-judge-v1`, versión `1.0.0`.
- **Depende de:** `agent-campaigns-v1` (la campaña, su validación y el paso 3, el control de calidad) y `segment-check-v1` (el juez). Reusa sin cambiarla la función del comando del operador, `bridge.campaign_check.run_check`, con `--criterio frenos`.
- **Código:** `src/bridge/campaign_judge_api.py` (las dos rutas y la corrida), la migración `20261126500000_agent_campaign_judge_runs_v1.sql` (las corridas) y las herramientas `judge_campaign` y `get_campaign_judge` del plugin `automation-expert-change` 0.14.0, con una línea en la guía `armar-una-campana`.
- **Flag:** `AUTOMATION_EXPERT_JUDGE_ENABLED`, apagado por defecto. Apagado, las rutas no existen (404), `/ready` no cambia y ningún camino arma el juez de OpenRouter ni llama a las funciones nuevas.

## 1. Qué cambia

Hasta acá, el control de calidad de una campaña validada lo corre el operador a mano (`python -m bridge.campaign_check <id> --instancia /instancia --soul <SOUL> --criterio frenos`, el kit `e673/juez.sh`). Con el flag, el AE lo pide desde el hilo y el bridge lo corre **en segundo plano**, con la misma función y los mismos frenos que el kit; el AE lee el resultado después y se lo cuenta al dueño.

El juez llama al modelo por OpenRouter (3 corridas, de 6 a 17 minutos): por eso cada corrida queda en la base y se acota.

## 2. Las rutas (con la clave de cambio del AE)

`POST /internal/automation-expert/v1/campaigns/{id}/judge`, cuerpo `{"requested_by": {"slack_user": "U…"}}` (lo pone el plugin desde la sesión de Slack, nunca el modelo).

1. Fuera de **09:00 a 21:00 de la zona de la instancia** (`instancia.zona_horaria`) contesta `409 agent_campaign_judge_outside_hours` sin llamar a la base ni al modelo.
2. Abre la corrida en la base (`start_agent_campaign_judge_run_v1`), que contesta con un motivo si: la campaña no está `validada` (`agent_campaign_not_validated`), venció (`agent_campaign_expired`), le quedan 20 minutos o menos (`agent_campaign_judge_expires_soon`), hay otra corriendo en la instancia (`agent_campaign_judge_running`) o la clave de la campaña ya tiene 3 corridas desde el comienzo del día de la instancia (`agent_campaign_judge_daily_limit`). Todas son `409`; la campaña que no existe, `404 agent_campaign_not_found`.
3. Con la corrida abierta contesta `202` enseguida (`status: corriendo`, `resultado: CORRIENDO`, `runs_today`, `daily_limit`, `window`) y corre el juez en un hilo. Al terminar anota el resultado con `finish_agent_campaign_judge_run_v1`. La fila de la campaña la anota `run_check` como siempre (`record_agent_campaign_check_v1`).

`GET /internal/automation-expert/v1/campaigns/{id}/judge` devuelve `last_run` (`run_id`, `status`, `resultado`, `error_code`, `result`, `started_at`, `finished_at`) o `null`, `runs_today`, `daily_limit`, `window` y `can_start_now`.

`resultado` es una palabra: `CORRIENDO`, `LIMPIO`, `CON-PREGUNTAS` o `ERROR`, como termina el kit. `result` trae `veredicto`, `abiertos`, `corridas_validas`, `corridas_pedidas`, `criterio`, `avisos_total`, `preguntas` (hasta 10 de las que frenan: `numero`, `tipo`, `razon`, `campo` hasta 120 caracteres, `pregunta` hasta 300) y `avisos` (hasta 5, `pregunta` hasta 200). La forma sale del `check_report` real de Maribel `forever-young-2026` v4 (leído en solo lectura el 2026-11-26).

## 3. Cómo termina una corrida

| Lo que pasó | `status` | `error_code` |
|---|---|---|
| El juez dio limpio y la base lo anotó | `limpio` | — |
| Quedaron preguntas que frenan | `con_preguntas` | — |
| Ninguna corrida del modelo fue válida, o faltó alguna y nada frena (`sin_veredicto`; también si OpenRouter no contesta) | `error` | `agent_campaign_judge_no_verdict` (con `result`) |
| Falta la clave del modelo | `error` | `agent_campaign_judge_model_unavailable` |
| El chequeo no pudo leer la campaña o armar el segmento | `error` | `agent_campaign_judge_check_failed` |
| La base no anotó el resultado | `error` | `agent_campaign_judge_not_recorded:<motivo>` |
| El bridge se reinició en el medio (corriendo hace más de 30 minutos) | `error` | `agent_campaign_judge_abandoned` |

Una abandonada cuenta para el tope del día (ya gastó) y no frena la siguiente: la base la cierra como error al abrir otra. Un resultado que llega tarde no la pisa (`agent_campaign_judge_run_not_running`).

## 4. La base (20261126500000)

Una tabla, `agent_campaign_judge_runs` (una fila por corrida, con RLS y sin permisos para la API, índice único parcial que deja **una sola corriendo por instancia**), y tres funciones `security definer`, solo de `service_role`: `start_agent_campaign_judge_run_v1(p_instance, p_campaign_id, p_requested_by_ref, p_day_start, p_daily_limit)`, `finish_agent_campaign_judge_run_v1(p_instance, p_run_id, p_status, p_result, p_error_code)` y `get_agent_campaign_judge_run_v1(p_instance, p_campaign_id, p_day_start)`. El comienzo del día lo calcula el bridge en la zona de la instancia; la base lo acepta solo dentro de las últimas 26 horas. El tope cuenta por **clave** de campaña (todas sus versiones). Errores: `22023` entrada inválida, `P0002` lo que no existe.

## 5. Lo que el arranque exige con el flag

`AGENT_CAMPAIGNS_ENABLED`, el manifiesto de la instancia (la carpeta de `INSTANCE_MANIFEST_PATH` es la `--instancia` del comando), `SUPABASE_BASE_URL` y `SUPABASE_SERVICE_ROLE_KEY`, `OPENROUTER_API_KEY` en el entorno del bridge y `AUTOMATION_EXPERT_JUDGE_SOUL_PATH`: el SOUL común del agente comercial de la versión que corre, montado como archivo (la imagen del bridge no lo trae). Sin alguno, el bridge no arranca. El modelo es `SEGMENT_CHECK_MODEL` o el de `segment_check`. `/ready` suma `automation_expert_judge: enabled` solo con el flag.

## 6. Lo que no hace

- No manda nada a nadie: ni a leads ni al hilo. El AE se entera leyendo `get_campaign_judge` cuando el dueño vuelve a preguntar.
- No corre sobre una publicada o pausada (volver a certificar sigue siendo del comando del operador).
- No prueba la campaña conversando: un turno de prueba del agente comercial con el conocimiento de la campaña queda para un PR aparte.
