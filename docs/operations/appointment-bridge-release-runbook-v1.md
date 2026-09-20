# Release del Appointment Bridge en EasyPanel

- **Estado:** procedimiento verificado en lo observable desde el host; los pasos que se ejecutan dentro del panel de EasyPanel estan descritos a partir de la evidencia que dejan sus acciones, no de una ejecucion propia.
- **Servicio:** `infra_appointment-bridge` (proyecto `infra`, Docker Swarm gestionado por EasyPanel).
- **Base de la descripcion:** auditoria del 2026-09-19 (`2026-09-19-claude-production-ingress-audit-v1.md`).
- **Quien hace que:** el despliegue lo dispara **Dan** desde el panel. Todas las verificaciones previas y posteriores las puede hacer el programador desde el host, por SSH y sin tocar el servicio.

## 0. Por que este documento existe

El runbook del conector de Slack fija el invariante correcto: desplegar una revision exacta y registrar el SHA completo. El flujo real de EasyPanel no lo cumple solo, y hasta esta auditoria no habia forma escrita de cerrar esa brecha para el bridge. Este documento describe el flujo tal como es y agrega los controles que lo hacen verificable y reversible.

Tres hechos que condicionan todo lo demas, medidos el 2026-09-19:

1. **El despliegue no es automatico.** Los merges a `main` del 18 y del 19 de septiembre no dispararon ninguna accion de EasyPanel: la ultima accion registrada es del 2026-09-18 a las 15:12 UTC. Mergear no despliega. Se aprieta en el panel.
2. **La imagen lleva un tag movil.** El build publica `easypanel/infra/appointment-bridge:latest` y lo sobreescribe. `docker service rollback` devuelve el spec anterior, que apunta al mismo tag: **no devuelve el codigo anterior**.
3. **No hay pin por digest ni registro automatico de la revision.** El servicio expone una variable `GIT_SHA`; el 2026-09-19 su valor coincidia con el codigo realmente desplegado, pero una variable de entorno no es prueba de nada por si sola. La prueba es la comparacion de contenido de la seccion 4.

## 1. Precondiciones

- El commit a desplegar esta en `origin/main`, con CI en verde y el claim de la tarea en `merged`.
- Se conoce **exactamente** que cambia en runtime respecto de lo desplegado. Se calcula asi, sin adivinar:

  ```sh
  git diff --stat <sha-desplegado>..origin/main -- src/
  ```

  Si el resultado es vacio, no hay release que hacer. Si toca mas de un area, se decide si conviene partir el release.
- Los flags de efectos estan en el estado deseado **antes** del release, y ese estado esta escrito. Un release no es el momento de estrenar un flag: un cambio por vez en lo que se mide.
- Hay una ventana de al menos treinta minutos sin trafico esperado de terceros, o se asume conscientemente el riesgo sobre el trafico que entra (hoy: eventos de Hotmart, del funnel y de la landing).

## 2. Preservar el destino del rollback

Antes de construir nada, fijar un alias para la imagen que hoy corre. Sin esto no hay vuelta atras, porque el build sobreescribe `latest` y el demonio no guarda copias.

```sh
ssh contabo 'docker tag easypanel/infra/appointment-bridge:latest \
  easypanel/infra/appointment-bridge:preserved-$(date +%Y%m%d-%H%M)'
ssh contabo 'docker images --format "{{.Repository}}:{{.Tag}} {{.ID}}" | grep preserved'
```

Anotar el identificador de imagen que devuelve. Un alias no modifica el servicio, no ocupa espacio y se borra con `docker rmi` del tag.

## 3. Tomar la linea de base

Todo se compara contra esto despues, asi que se guarda antes:

```sh
ssh contabo 'docker inspect $(docker ps --filter name=infra_appointment-bridge -q) \
  --format "{{.State.StartedAt}} {{.Image}} {{.State.Health.Status}}"'
ssh contabo 'docker service inspect infra_appointment-bridge \
  --format "{{range .Spec.TaskTemplate.ContainerSpec.Env}}{{println .}}{{end}}" | grep -E "^(GIT_SHA|DEPLOY_TIMESTAMP)="'
ssh contabo 'C=$(docker ps --filter name=infra_appointment-bridge --format "{{.Names}}" | head -1); \
  docker exec "$C" python -c "import urllib.request;print(urllib.request.urlopen(\"http://127.0.0.1:8000/ready\",timeout=5).read().decode())"'
```

Guardar tambien el conteo de trafico reciente por codigo, que es la unica alarma que hoy existe sobre el ingreso:

```sh
ssh contabo 'T=$(docker ps --filter name=easypanel-traefik --format "{{.Names}}" | head -1); \
  docker logs "$T" 2>&1 | grep -oE "\"POST /webhooks/[a-z-]+ HTTP/[0-9.]+\" [0-9]{3}" | sort | uniq -c | sort -rn'
```

## 4. Desplegar y verificar la correspondencia con Git

El disparo es manual, en el panel de EasyPanel, servicio `infra / appointment-bridge`, boton de despliegue. EasyPanel descarga un archive del repositorio en GitHub, construye con el `Dockerfile` del repositorio y publica `easypanel/infra/appointment-bridge:latest`; despues actualiza el servicio. La politica de actualizacion del servicio es de una replica, `start-first` y `pause` ante fallo.

**Advertencia sobre la etiqueta de commit del panel:** el encabezado `Commit:` que queda en el log de la accion no es evidencia suficiente de que se construyo. El 2026-09-18, dos despliegues del bridge posteriores al merge del PR #155 quedaron registrados con el mensaje del PR #154. No se determino si el archive sigue al HEAD de la rama o a la ultima notificacion recibida. Por eso el paso siguiente no es opcional.

**Verificacion de correspondencia (la que vale).** Se comparan los objetos del codigo que efectivamente corre contra el arbol del commit esperado:

```sh
ssh contabo 'C=$(docker ps --filter name=infra_appointment-bridge --format "{{.Names}}" | head -1); docker exec "$C" python -c "
import hashlib, os
for dirpath, dirnames, filenames in os.walk(\"/app/src\"):
    dirnames[:] = [d for d in dirnames if d != \"__pycache__\"]
    for fn in sorted(filenames):
        if fn.endswith(\".pyc\"): continue
        p = os.path.join(dirpath, fn)
        data = open(p, \"rb\").read()
        print(hashlib.sha1(b\"blob %d\\0\" % len(data) + data).hexdigest(), os.path.relpath(p, \"/app\"))
"' > /tmp/deployed-blobs.txt
git ls-tree -r <sha-esperado> src/ | awk '{print $3, $4}' | sort > /tmp/expected-blobs.txt
sort /tmp/deployed-blobs.txt | diff - /tmp/expected-blobs.txt && echo "corresponde"
```

Cualquier diferencia detiene el release: lo desplegado no es lo revisado.

Si el panel permite editar variables, actualizar `GIT_SHA` al commit desplegado en el mismo movimiento. Una variable que dice un commit viejo es peor que no tenerla, porque la proxima auditoria la va a creer.

## 5. Verificacion funcional

```text
GET /health -> 200 status=ok
GET /ready  -> 200, y ademas:
  automation_state y los flags, iguales a la linea de base salvo lo que el release cambia a proposito
  todos los contadores de human_handoff en 0
  ningun contador nuevo distinto de cero respecto de la linea de base
docker inspect -> Health = healthy, StartedAt posterior al despliegue
```

Despues, quince minutos de observacion del ingreso con el mismo conteo por codigo del paso 3. Lo que se busca no es "esta arriba", sino que la proporcion de 2xx por endpoint no empeore. Un `/health` en verde no prueba que los emisores esten entrando.

## 6. Rollback

El rollback correcto usa el alias preservado en el paso 2, porque el tag movil ya fue sobreescrito:

```sh
ssh contabo 'docker service update --image easypanel/infra/appointment-bridge:preserved-<sello> infra_appointment-bridge'
```

Es un cambio de spec con la misma politica de actualizacion y el mismo volumen. Despues se repiten los pasos 4 y 5 contra el SHA anterior.

Dos advertencias:

- `docker service rollback` **no** sirve por si solo mientras las imagenes lleven tag movil; deja el spec anterior apuntando al codigo nuevo.
- El siguiente despliegue desde el panel vuelve a poner `latest`. Un rollback por linea de comandos es un puente hasta que se corrija el codigo o se rearme el servicio desde el panel, no un estado final.

## 7. Lo que este runbook no resuelve

- **No hay pin por digest.** Mientras el servicio apunte a `:latest`, la trazabilidad commit a contenedor depende de la verificacion del paso 4, que es manual. Fijar el servicio a `easypanel/infra/appointment-bridge@sha256:<digest>` daria trazabilidad real, pero cambia como EasyPanel gestiona el servicio y esa decision es de Dan.
- **No hay alerta de ingreso.** Nadie se entera de que el bridge devuelve 502 o 503 salvo que alguien mire el log del proxy. La auditoria del 2026-09-19 encontro quince horas de caida del 16 al 17 de septiembre que nadie habia registrado.
- **No cubre migraciones de Supabase.** Si el release incluye DDL, el orden y las guardas estan en `lancemos-supabase-schema-contract-runbook.md` y en `supabase-first-infoproducer-release-runbook.md`, y la migracion se autoriza aparte.
- **No cubre los otros dos servicios del repositorio.** El conector de Slack tiene su propio runbook con invariantes mas estrictos; `infra_daily-feedback` no tiene ninguno.
