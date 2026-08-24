# ADR-0002: Carruseles multi-imagen entregados como ZIP unico

- **Estado:** Aceptado
- **Fecha:** 2026-08-24

## Contexto

Los posts con varias imagenes (carruseles de Instagram, galerias) producen N archivos por
descarga. El modelo de datos de ReClip asume **un archivo por trabajo**:

- `jobs[job_id] = {status, file, filename}` — un solo path.
- `/api/file/<job_id>` streamea y borra ese archivo.
- Telegram envia un documento o genera un token de un solo uso para >49 MB.

Ampliar el modelo a multiples archivos obligaria a tocar jobs, `/api/status`, `/api/file`,
el flujo de tokens de descarga, `telegram_send_download` y el frontend (varios botones Save).

## Decision

Cuando `gallery_dl_sync` encuentra **mas de un archivo** en la descarga, los empaqueta en un
unico ZIP (`downloads/<prefix>.zip`, entradas con el basename original) y devuelve ese ZIP como
archivo del job. Con un solo archivo se entrega el archivo tal cual, sin empaquetar.

El nombre del ZIP se genera con `sanitize_filename(title, "<prefix>.zip")`: si hay titulo,
`<titulo>.zip`; si no, el nombre interno del job.

## Consecuencias

- Positivo: cero cambios en jobs, tokens, streaming ni Telegram; el usuario recibe todo el
  carrusel en una sola descarga; funciona identico en web y bot.
- Negativo: descompresion manual por parte del usuario; no hay vista previa individual de cada
  slide.
- Neutro: un carrusel grande puede superar 49 MB en Telegram y usara el flujo existente de
  enlace de un solo uso (`/dl/<token>`).
