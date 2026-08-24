# ReClip — Contexto para Agentes

## Stack

- **Backend:** Python + Flask (single file: `app.py`, ~1350 lines)
- **Frontend:** Vanilla HTML/CSS/JS en `templates/` (sin frameworks)
- **Download engines:** yt-dlp + ffmpeg (video/audio), gallery-dl (imagenes, ADR-0001)
- **Bot Telegram:** python-telegram-bot v20+ (asyncio, opcional)
- **Base de datos:** Archivos JSON planos en `data/` (acl.json, web_codes.json, download_log.json)
- **Testing:** unittest con mocks de asyncio/subprocess (pytest tambien funciona)
- **Docker:** Python 3.12-slim, gunicorn, yt-dlp y gallery-dl se actualizan al arrancar

## Dependencias

Solo 4: `flask`, `yt-dlp`, `gallery-dl`, `python-telegram-bot`

## Estructura de archivos

```
app.py                    # Backend completo (Flask + Telegram bot)
docs/
  CONTEXT.md              # Dominio, glosario y flujos
  adr/                    # Decisiones de arquitectura (gallery-dl, ZIP carruseles, deteccion)
templates/
  index.html              # UI web principal (~730 lines, vanilla)
  admin.html              # Panel de administracion (407 lines)
  access.html             # Pagina de codigo de acceso web
static/
  favicon.svg
tests/
  test_app.py             # Tests de app (163 lines)
  test_integration.py     # Tests de integracion (>1100 lines)
data/
  cookies_admin.txt       # Cookies gestionadas desde el admin
downloads/                # Descargas (gitignored)
```

## Convenciones de codigo

- Sin tipo estricto (salvo algunos type hints basicos)
- Sin comentarios en codigo (IMPORTANTE: no anadir comentarios)
- Nombres de variables/funciones en ingles, strings de UI en espanol
- Usar `os.environ.get()` para configuracion, no archivos de config
- JSON storage: `json_load_dict`, `json_load_list`, `json_save` con escritura atomica (tmp + replace)
- Locks de threading para acceso a datos compartidos (`acl_lock`, `web_codes_lock`, `download_log_lock`, `telegram_lock`, `login_attempts_lock`)

## Arquitectura

### Middleware (`@app.before_request`)
- `/static/`, `/access`, `/admin`, `/dl/`, `/api/acl/` -> permitidos sin auth
- `/api/*` sin sesion -> 401 JSON
- `/` sin sesion -> redirect `/access`
- Admin session: `session["admin"] = True`
- Web access session: `session["web_access"] = {"code", "label"}`

### Variables globales clave
- `jobs = {}` — descargas web activas (job_id -> {status, file, filename, ...})
- `chat_sessions = {}` — sesiones de Telegram (chat_id -> {url, title, formats})
- `download_tokens = {}` — tokens de descarga unico uso (token -> {path, filename})
- `pending_codes = {}` — codigos de acceso pendientes de aprobar (code -> {chat_id, username, ...})
- `pending_cookies_setup = {}` — usuarios enviando cookies por Telegram

### Rutas API

| Ruta | Metodo | Uso |
|------|--------|-----|
| `/api/info` | POST | Obtener metadatos de un video |
| `/api/playlist` | POST | Expandir playlist YouTube en URLs individuales |
| `/api/download` | POST | Iniciar descarga asincrona (devuelve job_id) |
| `/api/status/<job_id>` | GET | Pollear estado de descarga |
| `/api/file/<job_id>` | GET | Descargar archivo completado (stream + delete) |
| `/api/acl/approve` | POST | Aprobar usuario Telegram |
| `/api/acl/users` | GET | Listar usuarios ACL |
| `/api/acl/block` | POST | Bloquear usuario |
| `/api/acl/unblock` | POST | Desbloquear usuario |

### Telegram bot

Handlers: `start`, `help`, `cookies`, `cancel`, `on_message`, `on_callback`

Flujo:
1. Usuario envia /start -> genera codigo de 6 chars -> admin lo aprueba en `/admin`
2. Usuario envia URL -> bot responde con menu de formatos (callback `dl|best`, `dl|audio`, `dl|f|<format_id>`); si el enlace es de imagenes muestra boton unico "Imagen" (`dl|image`)
3. Bot descarga con `download_sync` (o `gallery_dl_fetch` si imagen) y envia archivo; carrusel 2-9 -> menu `img|loose`/`img|zip`/`img|cancel`
4. Si archivo > 49 MB: genera token de descarga, envia enlace de un solo uso

### Soporte de imagenes (ADR-0001/0002/0003/0004)

- Deteccion: `error_is_no_media()` (errores tipo "No video formats") y `info_is_image()` (sin formatos con vcodec/acodec reales). `/api/info` devuelve `is_image: true`.
- Motor: `gallery_dl_fetch()` ejecuta gallery-dl y devuelve lista suelta `(path, nombre_original)`; `zip_image_files()` empaqueta bajo demanda
- Entrega: 1 -> original; 2-9 -> pregunta sueltas/ZIP (botones web "Images"/"ZIP", rutas `/api/file/<id>/<index>` y `/api/zip/<id>`; en bot callbacks `img|loose`/`img|zip`/`img|cancel` con `images_pending` en chat_sessions); >= 10 (`IMAGE_ZIP_MIN_FILES`) -> ZIP automatico
- Limpieza: `cleanup_pass()` barre pendientes/jobs multi sin resolver tras 30 min (`PENDING_TTL_SECONDS`)
- Cookies compartidas con yt-dlp (misma resolucion: browser > admin cookies.txt > COOKIES_FILE)

### Descargas grandes (>49 MB Telegram)

En `telegram_send_download` (linea 856):
1. Descarga el archivo normalmente
2. Si `size_mb > 49` y `RECLIP_BASE_URL` esta configurado: genera `download_tokens[token]`, envia enlace `{base_url}/dl/{token}`
3. Si `RECLIP_BASE_URL` no esta configurado: lanza RuntimeError
4. La pagina `/dl/<token>` muestra HTML bonito con boton de descarga
5. `/dl/<token>/download` stremea el archivo y lo elimina al terminar
6. El token es de un solo uso (se elimina tras descargar)

### Web download flow

1. Frontend envia POST a `/api/download` -> recibe `job_id`
2. Thread en background ejecuta `run_download` -> actualiza `jobs[job_id]`
3. Frontend polla `/api/status/<job_id>` cada 1s
4. Cuando `status == "done"`, frontend hace GET a `/api/file/<job_id>` (stream + delete)

### Admin panel (`/admin`)

- Login con `ADMIN_PASSWORD`
- Generar/revocar codigos de acceso web
- Aprobar codigos de Telegram pendientes
- Gestionar cookies (guardar/eliminar)
- Bloquear/desbloquear usuarios
- Ver logs de descargas (ultimas 200)

## Configuracion (.env)

Variables esenciales:
- `TELEGRAM_BOT_TOKEN` — Token del bot de Telegram
- `RECLIP_BASE_URL` — URL publica (necesaria para descargas >49MB)
- `ADMIN_PASSWORD` — Password del panel /admin
- `SECRET_KEY` — Clave de sesion Flask
- `ACL_PATH` — Ruta del archivo ACL (defecto: `data/acl.json`)
- `COOKIES_FILE` — Ruta cookies.txt (defecto: `./cookies.txt`)
- `COOKIES_FROM_BROWSER` — Usar cookies del navegador (chrome, firefox, etc.)
- `HOST` / `PORT` — Bind address (defecto: 127.0.0.1:8899)

## Docker

- `Dockerfile`: python:3.12-slim + ffmpeg + gunicorn
- `docker-compose.yaml`: single service con volumenes `downloads` y `data`
- `docker-compose-portainer.yaml`: variante con red Traefik externa
- `docker-entrypoint.sh`: actualiza yt-dlp al arrancar (skip con `RECLIP_NO_UPDATE=1`)
- Labels de Traefik configurables via .env

## Testing

```bash
python -m pytest tests/ -v
```

- `test_app.py`: tests de app (mocks basicos)
- `test_integration.py`: tests de integracion (mocks de subprocess, asyncio)
- `ImmediateThread` helper: ejecuta threads inline en tests
- Usar `@patch` para mockear `download_sync`, `subprocess.run`, `fetch_video_info`

## Git

Commits recientes relevantes:
- `ee26f82` — Anadio soporte Telegram + admin panel
- `246c7c2` — Cookies desde Instagram
- `22df644` — Arreglado descarga por enlace (token no se consumia hasta download real)
- `be13420` — Anadido boton de descarga en pagina `/dl/<token>`

## Notas importantes

- NO añadir comentarios al codigo
- Strings de UI en espanol
- Los archivos descargados se eliminan tras servir la descarga (stream_and_delete)
- No hay base de datos real — todo son JSON en `data/`
- yt-dlp se actualiza automaticamente al iniciar (en reclip.sh y docker-entrypoint.sh)
- Las cookies se guardan en `data/cookies_admin.txt` desde el admin panel
- Los codigos web son tokens hex de 8 chars, los de Telegram son 6 chars
- Rate limiting: 5 intentos de login cada 15 minutos por IP
- Download log limitado a 200 entradas (las mas recientes)
