---
name: instruccion-temporal
description: "Una indicación por unos días para todas las charlas"
---

# Instrucción temporal para todas las conversaciones

**Cargala antes de tu primera respuesta si alguien quiere que el agente diga o deje de decir algo por unos días, en todas las conversaciones.** Por ejemplo: «hoy no ofrezcas cupón», «avisá que el vivo se movió al jueves», «esta semana no prometas el bono», «¿qué le dijimos al agente?», «sacá lo del cupón».

## Primero: ¿está prendida en esta instancia?

**Tu primera herramienta es `list_temporary_instructions`, antes de preguntar nada.**

- Si vuelve `temporary_instruction_not_enabled`: no está prendida acá. Decilo así («eso todavía no está prendido acá; lo prende el equipo») y frená. No digas que va a quedar andando.
- Con una lista (vacía o no) seguí. `live_instruction_id` dice si ya hay una andando.

## Lo que esto hace, y lo que no

Dicho en una frase: **la redactamos acá, la aprueba alguien de la lista en una página, y el agente la tiene en cuenta en todas las conversaciones hasta la fecha que pongas.** Después la deja de usar solo.

- **No manda nada a nadie.** El agente la usa solo cuando contesta.
- **Una sola andando por instancia.** Si se aprueba otra, la anterior deja de usarse: decilo antes de proponer.
- **Como mucho 7 días**, y siempre con fecha de vencimiento.
- **Sin links, teléfonos, mails ni montos.** El precio y los links salen del conocimiento del agente. Si quiere cambiar un precio o una oferta, eso es una campaña (`armar-una-campana`) o un cambio del conocimiento, no esto.
- **No la publicás ni la retirás vos.** Lo hace una persona de la lista en la página.

## Cómo se conversa

Mensajes cortos, en lenguaje de negocio. Lo que tiene que quedar claro:

1. **Qué tiene que hacer o dejar de hacer el agente**, en una o dos frases, como lo diría el equipo. Escribilo vos y mostráselo. Hasta 600 caracteres.
2. **Hasta cuándo.** Día y hora, con la zona de la instancia. Si no lo dice, proponé el fin del día de hoy.

Las fechas van con barra («el 29/11»): con guiones y el año («29-11-2026») parece un teléfono y se frena.

## Los pasos

1. **La vista previa.** `preview_temporary_instruction` con `texto` y `hasta`. Si vuelve con `blocks`, corregí vos y probá de nuevo:
   - `temporary_instruction_link`: sacá el link; si hace falta, decí «el link de siempre».
   - `temporary_instruction_identifier`: sacá el teléfono o el mail (o la fecha con guiones).
   - `temporary_instruction_amount`: sacá el monto; el precio lo dice el conocimiento.
   - `temporary_instruction_window_over` o `temporary_instruction_window_too_long`: otra fecha, en el futuro y a lo sumo 7 días.
2. **El resumen.** El texto y hasta cuándo, en dos líneas, y si ya hay una andando, que esa deja de usarse. **Esperá el sí.**
3. **Proponer.** `propose_temporary_instruction`, con `purpose` en una línea.
4. **El link para aprobar.** Vuelve `approval_url`: mandáselo a la persona, así, sin explicar qué es un id. Vale 24 horas. «Ya quedó redactada. Alguien de la lista la aprueba en esta página y queda andando; el agente no la usa antes.» No digas que quedó andando.

## Después

- **Si preguntan qué tiene el agente**, `list_temporary_instructions` o `get_temporary_instruction`: el estado y hasta cuándo vale.
- **Si la quieren sacar antes**: no la retirás vos. Mandá el `approval_url` de la que está andando y decí que ahí está el botón «Retirar». En menos de un minuto el agente la deja de usar.
- **Si la quieren cambiar**: se propone una nueva (pasos 1 a 4). Al aprobarla, reemplaza a la anterior.
