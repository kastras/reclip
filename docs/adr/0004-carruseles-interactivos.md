# ADR-0004: Entrega interactiva de carruseles de 2 a 9 imagenes

- **Estado:** Aceptado (enmienda ADR-0002)
- **Fecha:** 2026-08-24

## Contexto

ADR-0002 fijaba que todo carrusel multi-imagen se entregaba siempre como ZIP. En el uso real,
comprimir 3 fotos es mas estorbo que ayuda; pero comprimir 30 es imprescindible. Ademas, en
Telegram conviene poder recibir las imagenes sueltas directamente en el chat.

## Decision

La entrega depende del numero de archivos N que produce `gallery_dl_fetch`:

| N | Comportamiento |
|---|----------------|
| 1 | Archivo original directo (contrato singular `job["file"]`) |
| 2-9 | **Pregunta al usuario**: sueltas o ZIP (web y Telegram) |
| >= 10 (`IMAGE_ZIP_MIN_FILES`) | ZIP automatico |

- **Telegram**: tras elegir "Imagen", si quedan pendientes el mensaje se transforma en un menu
  con botones `img|loose` (envia cada imagen con `send_document`), `img|zip` (empaqueta bajo
  demanda y envia un documento) e `img|cancel` (borra todo). Las pendientes viven en
  `chat_sessions[chat_id]["images_pending"] = {prefix, workdir, files, url, title, created_at}`.
- **Web**: el job queda `done` con `files=[(path, name)...]`, `files_count` y `workdir`. La
  tarjeta muestra dos botones: "Images (N)" (descarga secuencial por `/api/file/<id>/<index>`)
  y "ZIP" (`/api/zip/<id>`).
- **Motor refactorizado**: `gallery_dl_fetch(prefix, url, timeout)` SIEMPRE devuelve la lista
  suelta `(path, nombre_original)` dentro de `downloads/<prefix>/`; el empaquetado es decision
  del llamador via `zip_image_files(files, zip_path)`. Los nombres duplicados se resuelven con
  sufijos `_2`, `_3`...
- **Limpieza anti-huerfanos**: `cleanup_pass()` (hilo existente, cada 5 min) borra pendientes
  del bot y jobs multi sin resolver tras `PENDING_TTL_SECONDS` (30 min). Enviar una URL nueva
  con pendientes abiertas tambien las purga antes de sobrescribir la sesion.
- Cada imagen/ZIP conserva el chequeo de 49 MB via `telegram_send_file()` (documento o enlace
  de token de un solo uso).

## Consecuencias

- Positivo: UX natural para carruseles pequenos; cero clicks extra cuando el usuario quiere
  sueltas; los archivos nunca quedan huerfanos gracias al barrido periodico.
- Negativo: ventana temporal en la que el disco guarda imagenes sin resolver (acotada a 30
  min); dos rutas nuevas y tres callbacks nuevos que mantener.
- Neutro: ADR-0002 sigue siendo cierto para N >= 10; el caso N == 1 no cambia.
