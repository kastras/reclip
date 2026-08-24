# ADR-0001: Soporte de imagenes mediante gallery-dl como segundo motor

- **Estado:** Aceptado
- **Fecha:** 2026-08-24

## Contexto

ReClip solo descargaba video/audio via yt-dlp. Al pegar un enlace de un post de solo imagenes
(p. ej. foto de Instagram, enlace directo `.jpg`), yt-dlp falla con `No video formats found!`
y el usuario recibe un error. El proyecto de yt-dlp declara oficialmente que no es un descargador
de imagenes; su mantenedor recomienda `--ignore-no-formats-error --write-thumb` como workaround,
que descarga la miniatura en la mayor resolucion disponible.

Alternativas evaluadas:

1. **Truco yt-dlp (`--write-thumb`)** — sin dependencias nuevas, pero usa la miniatura (puede
   perder calidad), no soporta carruseles completos y depende de extractores pensados para video.
2. **gallery-dl como motor dedicado a imagenes** — herramienta madura, soporte nativo de posts de
   fotos y carruseles de Instagram, Twitter/X, Pinterest, Tumblr, enlaces directos y cientos de
   sitios mas. Añade una cuarta dependencia Python.
3. **Hibrido** — yt-dlp primero y gallery-dl si falla; complejidad extra sin beneficio claro.

## Decision

Añadir **gallery-dl** como segundo motor de descarga, especializado en imagenes.
yt-dlp sigue siendo el motor exclusivo para video/audio; gallery-dl se invoca solo cuando el
formato elegido es `image`.

Detalles:

- Nueva funcion `gallery_dl_sync(prefix, url, title, timeout)` en `app.py`, simetrica a
  `download_sync`: ejecuta `gallery-dl --destination downloads/<prefix> [--cookies ...] <url>`
  via `subprocess.run`, recolecta los archivos producidos con `os.walk` y devuelve
  `(path, filename)`.
- Cookies: se reutiliza exactamente la misma resolucion que ya usa yt-dlp
  (`COOKIES_FROM_BROWSER` > `data/cookies_admin.txt` del admin > `COOKIES_FILE`). Necesario
  para Instagram privado; gallery-dl acepta cookies en formato Netscape sin cambios.
- `download_sync()` delega en `gallery_dl_sync()` cuando `format_choice == "image"`.
- Dependencia añadida a `requirements.txt`; `reclip.sh` y `docker-entrypoint.sh` la actualizan
  al arrancar junto a yt-dlp.

## Consecuencias

- Positivo: descarga fiable de imagenes de sitios donde yt-dlp no llega; carruseles completos;
  extensiones correctas; misma infraestructura de jobs/tokens/logs sin cambios.
- Negativo: una dependencia mas que actualizar; binario adicional en la imagen Docker (~pip).
- Neutro: los errores de gallery-dl se exponen con el mismo patron que yt-dlp (ultima linea de
  stderr dentro de un `RuntimeError`).
