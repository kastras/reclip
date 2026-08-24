# ReClip — Contexto de dominio

Documento de referencia para el modelo de dominio y los flujos del sistema.
Las decisiones de arquitectura se registran en `docs/adr/`.

## Que es ReClip

Descargador self-hosted de media (video/audio/imagenes) con dos interfaces: web y bot de
Telegram. Un solo proceso Flask (`app.py`) sirve la UI, la API y el bot.

## Motores de descarga

| Motor | Uso | Invocacion |
|-------|-----|------------|
| yt-dlp | Video MP4 / audio MP3 | `subprocess.run` en `download_sync()` |
| gallery-dl | Imagenes (posts de fotos, carruseles, enlaces directos) | `subprocess.run` en `gallery_dl_fetch()` (ADR-0001, ADR-0004) |

La eleccion del motor no es por sitio sino por **formato pedido** (`video`/`audio` -> yt-dlp,
`image` -> gallery-dl). La clasificacion del enlace se hace en `/api/info` (ADR-0003).

## Glosario

- **Job** (`jobs[job_id]`) — descarga web asincrona. Estados: `downloading` -> `done` | `error`.
  Produce UN archivo (`file`, `filename`) o, para carruseles de 2-9 imagenes, VARIOS
  (`files=[(path, name)...]`, `files_count`, `workdir`) a la espera de eleccion (ADR-0004).
- **Chat session** (`chat_sessions[chat_id]`) — estado del menu de formatos del bot:
  `{url, title, formats, is_image, images_pending?}`.
- **Images pending** — carrusel de 2-9 imagenes descargado en Telegram esperando que el
  usuario elija sueltas/ZIP/cancelar; TTL de 30 min (ADR-0004).
- **Download token** (`download_tokens[token]`) — enlace de un solo uso `{path, filename}`
  para archivos >49 MB en Telegram; servido por `/dl/<token>` y consumido en
  `/dl/<token>/download`.
- **ACL** (`data/acl.json`) — usuarios de Telegram aprobados, con flag `blocked`.
- **Web codes** (`data/web_codes.json`) — codigos hex de 8 chars para acceder a la UI web;
  los de Telegram son de 6 chars y pasan por `pending_codes` hasta que el admin los aprueba.
- **Cookies** — Netscape `cookies.txt`; resolucion: `COOKIES_FROM_BROWSER` >
  `data/cookies_admin.txt` > `COOKIES_FILE`. Compartidas por ambos motores.

## Formatos de descarga

- `video` — mejor video+audio fusionado a MP4 (o formato elegido por chips de calidad).
- `audio` — extraccion a MP3 via ffmpeg.
- `image` — gallery-dl; 1 archivo = original; 2-9 = eleccion sueltas/ZIP; >= 10 = ZIP
  (`IMAGE_ZIP_MIN_FILES`).

## Flujos principales

### Web

1. POST `/api/info` -> metadatos + `formats[]` + `is_image`.
2. POST `/api/download` -> `job_id`; hilo en background ejecuta `run_download()`.
3. Frontend polla GET `/api/status/<job_id>` cada 1s.
4. Al terminar, GET `/api/file/<job_id>` streamea el archivo y lo elimina.

### Telegram

1. Usuario envia URL -> `fetch_video_info`; si no hay media -> `is_image` (ADR-0003).
2. Bot muestra menu: calidades + "Audio MP3", o boton unico "Imagen" si `is_image`.
3. Callback `dl|best` / `dl|audio` / `dl|f|<id>` / `dl|image` -> `telegram_send_download()`.
4. Con `dl|image`: 1 -> directo; 2-9 -> menu `img|loose` / `img|zip` / `img|cancel`;
   >= 10 -> ZIP (ADR-0004).
5. Envio por `telegram_send_file()`: <=49 MB documento; >49 MB token de un solo uso y enlace
   publico (requiere `RECLIP_BASE_URL`).

### Deteccion de imagenes (ADR-0003)

- Error de yt-dlp tipo "no video formats" -> imagen.
- Info sin formatos con `vcodec`/`acodec` reales ni extension multimedia -> imagen.
- Playlist donde TODAS las entradas carecen de media -> imagen; una entrada con video -> flujo
  normal de video.

## Invariantes

- Los archivos servidos se borran tras la entrega (`stream_and_delete`); los pendientes de
  eleccion (web y bot) se barren a los 30 min por `cleanup_pass()`.
- Escritura atomica de JSON (tmp + replace) bajo locks de threading.
- Sin comentarios en el codigo Python; strings de UI en espanol.
- Descargas limitadas a 5 min por subprocess; log de descargas capped a 200 entradas.
