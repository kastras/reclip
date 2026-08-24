# ADR-0003: Deteccion generica de enlaces de imagenes

- **Estado:** Aceptado
- **Fecha:** 2026-08-24

## Contexto

Para ofrecer descarga de imagenes sin que el usuario tenga que adivinar el tipo de enlace,
ReClip necesita clasificar una URL como "imagen" durante `/api/info` y en el bot de Telegram.
No existe un campo fiable `_type: image` en yt-dlp; el comportamiento varia por extractor:

- Posts de fotos de Instagram: `-j` puede fallar con `There is no video in this post`
  o devolver entradas de playlist sin formatos.
- Enlaces directos `.jpg/.png/.webp`: `-j` funciona pero el unico formato tiene
  `vcodec: none`, `acodec: none` y extension de imagen.

## Decision

Deteccion genérica con dos caminos, encapsulada en helpers puros:

1. **`error_is_no_media(message)`** — si `fetch_video_info` lanza `ValueError` cuyo texto
   contiene patrones conocidos (`no video formats`, `there is no video in this post`,
   `no media formats`), se trata como imagen, no como error. Cualquier otro error
   (login requerido, geo-bloqueo, URL no soportada) sigue siendo un error visible.
2. **`info_is_image(info)`** — sobre la info parseada: es imagen si ningun formato declara
   `vcodec`/`acodec` reales (distintos de `"none"`/`None`) ni una extension multimedia
   conocida (`MEDIA_FORMAT_EXTS`: mp4, webm, mkv, mp3, m4a, ...). En playlists (carruseles)
   se exige que **todas** las entradas carezcan de media; una sola entrada con video hace
   que el enlace siga el flujo normal de video.

`/api/info` responde entonces `"is_image": true` junto a titulo/thumbnail cuando esten
disponibles. El frontend y el bot usan esa senal para ofrecer el modo imagen.

## Consecuencias

- Positivo: cero configuracion para el usuario; cubre cualquier sitio soportado por
  gallery-dl aunque yt-dlp no reconozca el post; helpers puros y testeables con mocks.
- Negativo: heuristica basada en mensajes de error de terceros — si yt-dlp cambia la redaccion
  de "There is no video in this post" habra que ampliar los patrones.
- Neutro: enlaces mixtos (video + imagenes en carrusel) siguen el flujo de video con yt-dlp;
  las imagenes del carrusel no se descargan en ese caso.
