# Pasar un servicio a la imagen de GHCR, y volver atrás

- **Estado:** procedimiento escrito antes de su primera ejecución. La evidencia de cada corrida se agrega al final, con fecha, tag y digest.
- **Aplica a:** `infra_appointment-bridge`, `infra_daily-feedback`, `infra_supportmagician-slack-connector` y a los servicios de cualquier instancia nueva.
- **Reemplaza, para un servicio que ya usa imagen:** las secciones 2 y 4 de [appointment-bridge-release-runbook-v1.md](appointment-bridge-release-runbook-v1.md). La preservación manual de `:latest` y la comparación de blobs dejan de ser el único camino de vuelta, porque cada versión queda en el registro con su tag.
- **Quién hace qué:** el cambio en EasyPanel lo hace o lo autoriza **Dan**. Las verificaciones antes y después se pueden hacer por SSH, sin tocar el servicio.
- **Diseño:** [setter-producto-instalable-v1.md §4.1](../design/setter-producto-instalable-v1.md), [ADR-0021](../decisions/0021-setter-producto-instalable.md).

## 0. Qué cambia

Hoy EasyPanel descarga el repo, construye con el `Dockerfile` y publica `easypanel/infra/<servicio>:latest`, un tag que se sobreescribe. Con este procedimiento el servicio usa una imagen publicada por el workflow `release.yml`: `ghcr.io/danschwab2002/setter-bridge:v1.0.0`. Desplegar una versión es escribir su tag. Volver atrás es escribir el anterior.

## 1. Una sola vez: la credencial del registro

1. En GitHub, en la cuenta `danschwab2002`: *Settings → Developer settings → Personal access tokens → Tokens (classic)*. Crear un token con **solo** `read:packages`, sin vencimiento o con recordatorio. GHCR no acepta tokens *fine-grained* para bajar imágenes privadas.
2. Guardarlo en Vaultwarden. No va al repo, ni al vault, ni a un chat.
3. En EasyPanel, cada servicio que use la imagen lleva en su fuente *Docker Image* el usuario `danschwab2002` y ese token como contraseña.

Para verificar que el token alcanza sin tocar ningún servicio, desde el VPS:

```sh
ssh contabo 'docker login ghcr.io -u danschwab2002 --password-stdin && docker pull ghcr.io/danschwab2002/setter-bridge:v1.0.0 && docker logout ghcr.io'
```

La contraseña la escribe Dan en la terminal. Si el `pull` baja la imagen, EasyPanel también va a poder.

## 2. Antes de cambiar

1. La release existe y tiene los tres digests: `gh release view v1.0.0 --repo danschwab2002/ai-appointment-setter`.
2. El código de la versión es el que corre, o se sabe exactamente qué cambia:

   ```sh
   ssh contabo 'docker service inspect infra_appointment-bridge --format "{{range .Spec.TaskTemplate.ContainerSpec.Env}}{{println .}}{{end}}" | grep ^GIT_SHA='
   git diff --stat <GIT_SHA-desplegado> v1.0.0 -- src/ Dockerfile pyproject.toml uv.lock
   ```

   Un diff vacío en `src/` significa que el cambio de fuente no cambia el comportamiento.
3. Preservar la imagen actual como vuelta atrás de esta primera migración, porque todavía no hay un tag anterior en GHCR:

   ```sh
   ssh contabo 'docker tag easypanel/infra/appointment-bridge:latest easypanel/infra/appointment-bridge:preserved-$(date +%Y%m%d-%H%M)'
   ```

4. Tomar la línea de base de la sección 3 del runbook del bridge: hora de arranque, imagen, salud, `/ready` y el conteo de webhooks por código en Traefik.

## 3. El cambio

En EasyPanel, en el servicio, en *Source*:

1. Pasar de *GitHub* a *Docker Image*.
2. Imagen: `ghcr.io/danschwab2002/setter-bridge:v1.0.0`. El conector de Slack usa `setter-slack-connector` y la revisión diaria `setter-daily-feedback`.
3. Usuario y contraseña del registro de la sección 1.
4. No tocar las variables de entorno ni los montajes. **Ojo:** un redeploy de EasyPanel puede pisar variables que se cambiaron por fuera del panel. Antes de guardar, comparar la lista de nombres contra la línea de base.
5. Guardar y desplegar.

Orden recomendado: primero la revisión diaria, que no manda mensajes a leads. Después el conector de Slack y por último el bridge.

## 4. Verificar

```sh
# la imagen que corre es la del tag, por digest
ssh contabo 'docker service inspect infra_appointment-bridge --format "{{.Spec.TaskTemplate.ContainerSpec.Image}}"'
gh release view v1.0.0 --repo danschwab2002/ai-appointment-setter | grep setter-bridge
# el contenedor sano y la versión adentro
ssh contabo 'C=$(docker ps --filter name=infra_appointment-bridge --format "{{.Names}}" | head -1); docker exec "$C" sh -c "echo \$SETTER_VERSION; python -c \"import urllib.request;print(urllib.request.urlopen(\\\"http://127.0.0.1:8000/ready\\\",timeout=5).status)\""'
```

- El digest del servicio es el de la release.
- `SETTER_VERSION` dice `1.0.0`.
- `/ready` da 200.
- El conteo de webhooks por código en Traefik, en la hora siguiente, no muestra códigos nuevos contra la línea de base.
- Después de un día, `recuperador_estado.py` desde el OS no muestra emisiones nuevas en `reserved`.

## 5. Volver atrás

- **Con un tag anterior en GHCR:** escribir ese tag en *Source → Docker Image* y desplegar. Se verifica igual que en la sección 4.
- **En la primera migración, sin tag anterior:** volver la fuente a *GitHub*, o poner la imagen `easypanel/infra/appointment-bridge:preserved-<fecha>` de la sección 2.3.

**La prueba de vuelta atrás que cierra F1** se hace con la primera versión de parche (`v1.0.1`): desplegarla en la revisión diaria, volver a `v1.0.0` y verificar el digest. Es el servicio con menos riesgo de los tres y el único cambio es un tag.

## 6. Evidencia de las corridas

Sin corridas todavía. Cada una agrega: fecha UTC, servicio, tag de origen y destino, digest, resultado de la sección 4 y quién la autorizó.
