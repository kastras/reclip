from __future__ import annotations

import os
import time
import uuid
import glob
import json
import asyncio
import logging
import re
import secrets
import subprocess
import threading
from flask import Flask, request, jsonify, send_file, render_template

try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
    TELEGRAM_LIB_AVAILABLE = True
except ImportError:
    TELEGRAM_LIB_AVAILABLE = False

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

ACL_PATH = os.environ.get("ACL_PATH", os.path.join(os.path.dirname(__file__), "data", "acl.json"))
os.makedirs(os.path.dirname(ACL_PATH), exist_ok=True)

acl_lock = threading.Lock()
pending_codes: dict[str, dict] = {}

jobs = {}
chat_sessions = {}
download_tokens = {}
telegram_thread = None
telegram_started = False
telegram_lock = threading.Lock()


def parse_ytdlp_json(stdout):
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        return json.loads(line)
    raise ValueError("yt-dlp returned no data")


# ---------------------------------------------------------------------------
# ACL helpers
# ---------------------------------------------------------------------------

def acl_load() -> dict:
    if not os.path.exists(ACL_PATH):
        return {}
    try:
        with open(ACL_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def acl_save(data: dict) -> None:
    tmp = ACL_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, ACL_PATH)


def acl_is_approved(chat_id: int) -> bool:
    data = acl_load()
    entry = data.get(str(chat_id))
    return bool(entry and entry.get("approved") and not entry.get("blocked"))


def acl_increment_downloads(chat_id: int) -> None:
    with acl_lock:
        data = acl_load()
        key = str(chat_id)
        if key in data:
            data[key]["downloads"] = data[key].get("downloads", 0) + 1
            acl_save(data)


def acl_approve(chat_id: int, username: str, first_name: str) -> None:
    with acl_lock:
        data = acl_load()
        key = str(chat_id)
        existing = data.get(key, {})
        data[key] = {
            "chat_id": chat_id,
            "username": username,
            "first_name": first_name,
            "approved": True,
            "blocked": existing.get("blocked", False),
            "downloads": existing.get("downloads", 0),
            "approved_at": existing.get("approved_at") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        acl_save(data)


def format_duration(total_seconds):
    if total_seconds is None:
        return "unknown"
    total_seconds = int(total_seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02}:{seconds:02}"
    return f"{minutes}:{seconds:02}"


def build_quality_options(info):
    best_by_height = {}
    for f in info.get("formats", []):
        height = f.get("height")
        if height and f.get("vcodec", "none") != "none":
            tbr = f.get("tbr") or 0
            if height not in best_by_height or tbr > (best_by_height[height].get("tbr") or 0):
                best_by_height[height] = f

    formats = []
    for height, f in best_by_height.items():
        formats.append({
            "id": f["format_id"],
            "label": f"{height}p",
            "height": height,
        })
    formats.sort(key=lambda x: x["height"], reverse=True)
    return formats


def fetch_video_info(url, timeout=60):
    cmd = ["yt-dlp", "--no-playlist", "-j", url]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        error_line = result.stderr.strip().split("\n")[-1] if result.stderr else "Unknown error"
        raise ValueError(error_line)
    return parse_ytdlp_json(result.stdout)


def sanitize_filename(title, fallback_name):
    if title:
        safe_title = "".join(c for c in title if c not in r'\\/:*?"<>|').strip()[:80].strip()
        if safe_title:
            ext = os.path.splitext(fallback_name)[1]
            return f"{safe_title}{ext}"
    return fallback_name


def download_sync(prefix, url, format_choice, format_id=None, title="", timeout=300):
    out_template = os.path.join(DOWNLOAD_DIR, f"{prefix}.%(ext)s")
    cmd = ["yt-dlp", "--no-playlist", "-o", out_template]

    if format_choice == "audio":
        cmd += ["-x", "--audio-format", "mp3"]
    elif format_id:
        cmd += ["-f", f"{format_id}+bestaudio/best", "--merge-output-format", "mp4"]
    else:
        cmd += ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"]

    cmd.append(url)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        error_line = result.stderr.strip().split("\n")[-1] if result.stderr else "Unknown error"
        raise RuntimeError(error_line)

    files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{prefix}.*"))
    if not files:
        raise RuntimeError("Download completed but no file was found")

    if format_choice == "audio":
        target = [f for f in files if f.endswith(".mp3")]
        chosen = target[0] if target else files[0]
    else:
        target = [f for f in files if f.endswith(".mp4")]
        chosen = target[0] if target else files[0]

    for f in files:
        if f != chosen:
            try:
                os.remove(f)
            except OSError:
                pass

    default_name = os.path.basename(chosen)
    filename = sanitize_filename(title, default_name)
    return chosen, filename


def run_download(job_id, url, format_choice, format_id):
    job = jobs[job_id]
    try:
        chosen, filename = download_sync(
            prefix=job_id,
            url=url,
            format_choice=format_choice,
            format_id=format_id,
            title=job.get("title", ""),
            timeout=300,
        )
        job["status"] = "done"
        job["file"] = chosen
        job["filename"] = filename
    except subprocess.TimeoutExpired:
        job["status"] = "error"
        job["error"] = "Download timed out (5 min limit)"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/admin", methods=["GET", "POST"])
def admin():
    admin_password = os.environ.get("ADMIN_PASSWORD", "").strip()
    if not admin_password:
        return "Panel de administracion deshabilitado: ADMIN_PASSWORD no configurado.", 503

    error = None
    authed = False

    if request.method == "POST":
        action = request.form.get("action", "")

        if action == "login":
            if request.form.get("password", "") == admin_password:
                from flask import session
                session["admin"] = True
            else:
                error = "Contraseña incorrecta."

        elif action == "approve":
            from flask import session
            if session.get("admin"):
                code = (request.form.get("code") or "").strip().upper()
                entry = pending_codes.pop(code, None)
                if entry and time.time() - entry["created_at"] <= 1800:
                    acl_approve(entry["chat_id"], entry["username"], entry["first_name"])
                else:
                    error = "Código inválido o expirado."

        elif action == "block":
            from flask import session
            if session.get("admin"):
                chat_id = str(request.form.get("chat_id", "")).strip()
                with acl_lock:
                    data = acl_load()
                    if chat_id in data:
                        data[chat_id]["blocked"] = True
                        acl_save(data)

        elif action == "unblock":
            from flask import session
            if session.get("admin"):
                chat_id = str(request.form.get("chat_id", "")).strip()
                with acl_lock:
                    data = acl_load()
                    if chat_id in data:
                        data[chat_id]["blocked"] = False
                        acl_save(data)

        elif action == "logout":
            from flask import session
            session.pop("admin", None)

    from flask import session
    authed = session.get("admin", False)
    users = acl_load() if authed else {}
    return render_template("admin.html", authed=authed, users=users, error=error)


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    try:
        info = fetch_video_info(url, timeout=60)
        formats = build_quality_options(info)

        return jsonify({
            "title": info.get("title", ""),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration"),
            "uploader": info.get("uploader", ""),
            "formats": formats,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching video info"}), 400
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/playlist", methods=["POST"])
def get_playlist_info():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    cmd = ["yt-dlp", "--flat-playlist", "-J", url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return jsonify({"error": result.stderr.strip().split("\n")[-1]}), 400

        info = json.loads(result.stdout)
        entries = info.get("entries", [])
        urls = [entry.get("url") for entry in entries if entry.get("url")]
        return jsonify({"urls": urls})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching playlist info"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


def build_telegram_menu(formats):
    buttons = [[
        InlineKeyboardButton("Video (mejor)", callback_data="dl|best"),
        InlineKeyboardButton("Audio MP3", callback_data="dl|audio"),
    ]]

    shown_formats = formats[:8]
    for f in shown_formats:
        buttons.append([InlineKeyboardButton(f["label"], callback_data=f"dl|f|{f['id']}")])

    buttons.append([InlineKeyboardButton("Cancelar", callback_data="dl|cancel")])
    return InlineKeyboardMarkup(buttons)


def first_url_from_text(text):
    if not text:
        return None
    match = re.search(r"https?://\S+", text)
    return match.group(0) if match else None


async def telegram_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    chat_id = update.effective_chat.id
    user = update.effective_user

    if acl_is_approved(chat_id):
        await update.message.reply_text(
            "Ya estas autorizado. Enviame un enlace de video para descargarlo."
        )
        return

    code = secrets.token_hex(3).upper()
    pending_codes[code] = {
        "chat_id": chat_id,
        "username": user.username or "",
        "first_name": user.first_name or "",
        "created_at": time.time(),
    }
    base_url = os.environ.get("RECLIP_BASE_URL", "").rstrip("/")
    web_hint = f"\nWeb: {base_url}" if base_url else ""
    await update.message.reply_text(
        f"Para acceder necesitas autorización.\n\n"
        f"Tu código de acceso es:\n\n"
        f"<code>{code}</code>\n\n"
        f"Introduce este código en la sección de acceso de la web.{web_hint}",
        parse_mode="HTML",
    )


async def telegram_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Pega un enlace (YouTube, TikTok, Instagram, etc.) y elige una opcion del menu.")


async def telegram_on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    chat_id = update.effective_chat.id
    if not acl_is_approved(chat_id):
        await update.message.reply_text(
            "No tienes acceso. Usa /start para obtener tu codigo de acceso e introdicelo en la web."
        )
        return

    url = first_url_from_text(update.message.text or "")
    if not url:
        await update.message.reply_text("No detecte un enlace valido. Enviame una URL que empiece con http:// o https://")
        return

    loading_msg = await update.message.reply_text("Analizando enlace...")
    try:
        info = await asyncio.to_thread(fetch_video_info, url, 60)
    except subprocess.TimeoutExpired:
        await loading_msg.edit_text("Se agoto el tiempo al leer info del video.")
        return
    except Exception as e:
        await loading_msg.edit_text(f"No pude leer ese enlace: {e}")
        return

    formats = build_quality_options(info)
    chat_id = update.effective_chat.id
    chat_sessions[chat_id] = {
        "url": url,
        "title": (info.get("title") or "").strip(),
        "formats": formats,
    }

    title = info.get("title", "Sin titulo")
    uploader = info.get("uploader") or "desconocido"
    duration = format_duration(info.get("duration"))
    text = f"{title}\nCanal: {uploader}\nDuracion: {duration}\n\nElige formato:" 
    await loading_msg.edit_text(text, reply_markup=build_telegram_menu(formats))


async def telegram_send_download(chat_id, bot, url, title, format_choice, format_id=None):
    prefix = f"tg_{uuid.uuid4().hex[:10]}"
    path = None
    try:
        path, filename = await asyncio.to_thread(
            download_sync,
            prefix,
            url,
            format_choice,
            format_id,
            title,
            300,
        )
        size_mb = os.path.getsize(path) / (1024 * 1024)
        if size_mb > 49:
            base_url = os.environ.get("RECLIP_BASE_URL", "").rstrip("/")
            if not base_url:
                raise RuntimeError(
                    f"El archivo pesa {size_mb:.0f} MB y supera el limite de 49 MB de Telegram, "
                    "y RECLIP_BASE_URL no esta configurado para generar un enlace de descarga."
                )
            token = uuid.uuid4().hex
            download_tokens[token] = {"path": path, "filename": filename}
            link = f"{base_url}/dl/{token}"
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"El archivo pesa {size_mb:.0f} MB y supera el limite de Telegram.\n\n"
                    f"Descargalo desde este enlace (expira tras la primera descarga):\n{link}"
                ),
            )
            return
        with open(path, "rb") as f:
            await bot.send_document(chat_id=chat_id, document=f, filename=filename)
    finally:
        if path and os.path.exists(path) and not any(
            t["path"] == path for t in download_tokens.values()
        ):
            try:
                os.remove(path)
            except OSError:
                pass


async def telegram_on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()

    data = query.data or ""
    chat_id = query.message.chat_id if query.message else None
    if chat_id is None:
        return

    session = chat_sessions.get(chat_id)
    if not session:
        await query.edit_message_text("No tengo una URL activa. Enviame un enlace primero.")
        return

    if data == "dl|cancel":
        chat_sessions.pop(chat_id, None)
        await query.edit_message_text("Operacion cancelada.")
        return

    if not data.startswith("dl|"):
        return

    format_choice = "video"
    format_id = None
    if data == "dl|audio":
        format_choice = "audio"
    elif data == "dl|best":
        format_choice = "video"
    elif data.startswith("dl|f|"):
        format_choice = "video"
        format_id = data.split("|", 2)[2]
    else:
        await query.edit_message_text("Opcion no valida.")
        return

    await query.edit_message_text("Descargando... esto puede tardar un poco.")
    try:
        await telegram_send_download(
            chat_id=chat_id,
            bot=context.bot,
            url=session["url"],
            title=session.get("title", ""),
            format_choice=format_choice,
            format_id=format_id,
        )
        await context.bot.send_message(chat_id=chat_id, text="Listo. Si quieres otro, enviame otro enlace.")
        acl_increment_downloads(chat_id)
    except subprocess.TimeoutExpired:
        await context.bot.send_message(chat_id=chat_id, text="Tiempo agotado durante la descarga (5 min).")
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"Error al descargar o enviar: {e}")


def start_telegram_bot():
    global telegram_thread, telegram_started

    with telegram_lock:
        if telegram_started:
            return
        telegram_started = True

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        app.logger.info("Telegram bot disabled: TELEGRAM_BOT_TOKEN is not set")
        return

    if not TELEGRAM_LIB_AVAILABLE:
        app.logger.warning("Telegram bot disabled: install python-telegram-bot")
        return

    def run_bot():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        application = Application.builder().token(token).build()
        application.add_handler(CommandHandler("start", telegram_start))
        application.add_handler(CommandHandler("help", telegram_help))
        application.add_handler(CallbackQueryHandler(telegram_on_callback))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, telegram_on_message))
        try:
            loop.run_until_complete(application.initialize())
            loop.run_until_complete(application.start())
            loop.run_until_complete(application.updater.start_polling(drop_pending_updates=True))
            loop.run_forever()
        except Exception:
            app.logger.exception("Telegram bot crashed")
        finally:
            try:
                loop.run_until_complete(application.updater.stop())
                loop.run_until_complete(application.stop())
                loop.run_until_complete(application.shutdown())
            except Exception:
                pass
            loop.close()

    telegram_thread = threading.Thread(target=run_bot, name="telegram-bot", daemon=True)
    telegram_thread.start()


# ---------------------------------------------------------------------------
# ACL web API
# ---------------------------------------------------------------------------

@app.route("/api/acl/approve", methods=["POST"])
def acl_approve_code():
    data = request.json or {}
    code = (data.get("code") or "").strip().upper()
    if not code:
        return jsonify({"error": "No code provided"}), 400

    entry = pending_codes.pop(code, None)
    if not entry:
        return jsonify({"error": "Invalid or expired code"}), 404

    if time.time() - entry["created_at"] > 1800:
        return jsonify({"error": "Code has expired"}), 410

    acl_approve(entry["chat_id"], entry["username"], entry["first_name"])
    return jsonify({"ok": True, "chat_id": entry["chat_id"], "username": entry["username"]})


@app.route("/api/acl/users", methods=["GET"])
def acl_list_users():
    return jsonify(acl_load())


@app.route("/api/acl/block", methods=["POST"])
def acl_block_user():
    data = request.json or {}
    chat_id = str(data.get("chat_id", "")).strip()
    if not chat_id:
        return jsonify({"error": "No chat_id provided"}), 400
    with acl_lock:
        acl_data = acl_load()
        if chat_id not in acl_data:
            return jsonify({"error": "User not found"}), 404
        acl_data[chat_id]["blocked"] = True
        acl_save(acl_data)
    return jsonify({"ok": True})


@app.route("/api/acl/unblock", methods=["POST"])
def acl_unblock_user():
    data = request.json or {}
    chat_id = str(data.get("chat_id", "")).strip()
    if not chat_id:
        return jsonify({"error": "No chat_id provided"}), 400
    with acl_lock:
        acl_data = acl_load()
        if chat_id not in acl_data:
            return jsonify({"error": "User not found"}), 404
        acl_data[chat_id]["blocked"] = False
        acl_save(acl_data)
    return jsonify({"ok": True})


@app.route("/dl/<token>")
def download_by_token(token):
    entry = download_tokens.pop(token, None)
    if not entry:
        return "Enlace no valido o ya utilizado.", 404
    path = entry["path"]
    filename = entry["filename"]
    if not os.path.exists(path):
        return "El archivo ya no esta disponible.", 410

    def stream_and_delete():
        try:
            with open(path, "rb") as f:
                yield from f
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    from flask import Response, stream_with_context
    import mimetypes
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return Response(stream_with_context(stream_and_delete()), headers=headers, mimetype=mime)


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json
    url = data.get("url", "").strip()
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    title = data.get("title", "")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    job_id = uuid.uuid4().hex[:10]
    jobs[job_id] = {"status": "downloading", "url": url, "title": title}

    thread = threading.Thread(target=run_download, args=(job_id, url, format_choice, format_id))
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def check_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "error": job.get("error"),
        "filename": job.get("filename"),
    })


@app.route("/api/file/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404
    return send_file(job["file"], as_attachment=True, download_name=job["filename"])


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    start_telegram_bot()
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=port)
