#!/usr/bin/env python3
import asyncio
import html
import json
import mimetypes
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

import whisper
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

CONFIG_DIR = Path.home() / ".config"
ID_FILE = CONFIG_DIR / "agy_telegram_uid"
TOKEN_FILE = CONFIG_DIR / "agy_telegram_token"
MEDIA_DIR = Path("/tmp/agy_telegram_media")
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

force_new_session = False

print("Cargando modelo de transcripcion Whisper...")
whisper_model = whisper.load_model("base")


def load_token() -> str:
    env_token = os.environ.get("AGY_TELEGRAM_TOKEN", "").strip()
    if env_token:
        return env_token
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    raise RuntimeError(f"No se encontró token de Telegram en {TOKEN_FILE} ni en AGY_TELEGRAM_TOKEN.")


def get_owner_id():
    if ID_FILE.exists():
        return int(ID_FILE.read_text(encoding="utf-8").strip())
    return None


def set_owner_id(uid: int):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    ID_FILE.write_text(str(uid), encoding="utf-8")


async def check_auth(update: Update) -> bool:
    uid = update.effective_user.id
    owner = get_owner_id()
    if owner is None:
        set_owner_id(uid)
        await update.message.reply_text(f"Dispositivo vinculado. Tu ID ({uid}) ha quedado registrado como unico dueño.")
        return True
    return uid == owner


async def _take_screenshot(img_path: str, target_arg: str, env: dict) -> tuple[bool, str]:
    if os.path.exists(img_path):
        try:
            os.remove(img_path)
        except OSError:
            pass
    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            [os.path.expanduser("~/.local/bin/syscontrol"), "screenshot", img_path, target_arg],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0 and os.path.exists(img_path):
            return True, ""
        err = (proc.stderr or proc.stdout or "Error al capturar pantalla").strip()
        return False, err
    except subprocess.TimeoutExpired:
        return False, "Tiempo de espera agotado al capturar la pantalla (¿monitores apagados?)."
    except Exception as e:
        return False, f"Error al capturar la pantalla: {e}"


async def send_monitor_screenshot(update: Update, target: str = "all"):
    env = os.environ.copy()
    env["PATH"] = f"{os.path.expanduser('~/.local/bin')}:{env.get('PATH', '')}"

    try:
        proc = await asyncio.to_thread(
            subprocess.run, ["hyprctl", "monitors", "-j"], capture_output=True, text=True, env=env, timeout=5
        )
        mons = json.loads(proc.stdout)
        mons.sort(key=lambda m: m.get("id", 0))
    except Exception:
        mons = []

    if mons and all(m.get("dpmsStatus") is False for m in mons):
        await update.message.reply_text("No se puede capturar la pantalla: los monitores están apagados (Modo Fantasma / DPMS off).")
        return

    target_str = str(target).strip().lower()

    if target_str in ["all", "", "separado", "separados", "separada", "separadas", "ambos", "ambas", "todos", "todas"]:
        if not mons:
            img_path = "/tmp/tg_screen.png"
            ok, err = await _take_screenshot(img_path, "all", env)
            if ok:
                with open(img_path, "rb") as photo:
                    await update.message.reply_photo(photo=photo, caption="Pantalla completa")
            else:
                await update.message.reply_text(err)
            return

        for m in mons:
            m_num = m.get("id", 0) + 1
            m_name = m.get("name", f"DP-{m_num}")
            if m.get("dpmsStatus") is False:
                await update.message.reply_text(f"Monitor {m_num} ({m_name}) está apagado (DPMS off).")
                continue
            img_path = f"/tmp/tg_screen_{m_num}.png"
            ok, err = await _take_screenshot(img_path, str(m_num), env)
            if ok:
                with open(img_path, "rb") as photo:
                    await update.message.reply_photo(photo=photo, caption=f"Monitor {m_num} ({m_name})")
            else:
                await update.message.reply_text(f"Monitor {m_num} ({m_name}): {err}")
    else:
        img_path = f"/tmp/tg_screen_{target_str}.png"
        ok, err = await _take_screenshot(img_path, target_str, env)
        if ok:
            caption = f"Monitor {target_str}"
            if target_str.isdigit():
                idx = int(target_str)
                matching = next((m for m in mons if m.get("id") == idx - 1), None)
                if matching:
                    caption = f"Monitor {target_str} ({matching.get('name')})"
            elif target_str == "focused":
                caption = "Monitor activo"
            else:
                caption = f"Pantalla ({target_str})"
            with open(img_path, "rb") as photo:
                await update.message.reply_photo(photo=photo, caption=caption)
        else:
            await update.message.reply_text(err)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update):
        return
    await update.message.reply_text(
        "Agy conectado a tu Arch Linux.\n\n"
        "Comandos:\n"
        "/screen - Captura de pantallas por separado\n"
        "/screen 1 - Solo Monitor 1\n"
        "/screen 2 - Solo Monitor 2\n"
        "/new - Reiniciar memoria\n"
        "Puedes enviar texto, notas de voz, imágenes, vídeos o archivos."
    )


async def cmd_screen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update):
        return
    mon_arg = context.args[0] if context.args else "all"
    await send_monitor_screenshot(update, mon_arg)


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global force_new_session
    if not await check_auth(update):
        return
    force_new_session = True
    await update.message.reply_text("Nueva sesion de conversacion iniciada con agy.")


def markdown_to_telegram_html(text: str) -> str:
    """Convert GitHub-style Markdown into Telegram-compatible HTML."""
    code_blocks: list[str] = []
    inline_codes: list[str] = []

    def _save_code_block(match: re.Match) -> str:
        lang = (match.group(1) or "").strip()
        body = html.escape(match.group(2).strip("\n"), quote=False)
        if lang:
            rendered = f'<pre><code class="language-{html.escape(lang, quote=True)}">{body}</code></pre>'
        else:
            rendered = f"<pre>{body}</pre>"
        idx = len(code_blocks)
        code_blocks.append(rendered)
        return f"\x00CODEBLOCK{idx}\x00"

    def _save_inline_code(match: re.Match) -> str:
        body = html.escape(match.group(1), quote=False)
        idx = len(inline_codes)
        inline_codes.append(f"<code>{body}</code>")
        return f"\x00INLINECODE{idx}\x00"

    text = re.sub(r"```([a-zA-Z0-9_+-]*)\n(.*?)```", _save_code_block, text, flags=re.DOTALL)
    text = re.sub(r"`([^`\n]+)`", _save_inline_code, text)

    text = html.escape(text, quote=False)

    text = re.sub(r"^\s*[-*_]{3,}\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", r"<b>\1</b>", text, flags=re.MULTILINE)
    text = re.sub(r"^&gt;\s?(.*)$", r"<blockquote>\1</blockquote>", text, flags=re.MULTILINE)
    text = text.replace("</blockquote>\n<blockquote>", "\n")
    text = re.sub(r"^(\s*)[-*+]\s+(.+)$", r"\1• \2", text, flags=re.MULTILINE)

    text = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        lambda m: f'<a href="{html.escape(html.unescape(m.group(2)), quote=True)}">{m.group(1)}</a>',
        text,
    )
    text = re.sub(r"\[([^\]]+)\]\(file://[^)\s]+\)", r"<b>\1</b>", text)

    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    text = re.sub(r"(?<!\*)\*(?!\*)([^*\n]+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"(?<!\w)_([^_\n]+?)_(?!\w)", r"<i>\1</i>", text)

    for idx, rendered in enumerate(inline_codes):
        text = text.replace(f"\x00INLINECODE{idx}\x00", rendered)
    for idx, rendered in enumerate(code_blocks):
        text = text.replace(f"\x00CODEBLOCK{idx}\x00", rendered)

    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _split_markdown_chunks(text: str, limit: int = 3500) -> list[str]:
    """Split text on paragraph/line boundaries so Telegram's 4096-char limit isn't exceeded."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit and current:
            chunks.append(current.rstrip())
            current = ""
        if len(line) > limit:
            for i in range(0, len(line), limit):
                chunks.append(line[i:i + limit])
        else:
            current += line
    if current.strip():
        chunks.append(current.rstrip())
    return chunks or [text[:limit]]


async def send_formatted_reply(update: Update, output: str):
    for chunk in _split_markdown_chunks(output):
        formatted = markdown_to_telegram_html(chunk)
        if not formatted:
            continue
        try:
            await update.message.reply_text(
                formatted,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception:
            for i in range(0, len(chunk), 4000):
                await update.message.reply_text(chunk[i:i + 4000])


def _build_telegram_prompt(prompt: str, media_info: tuple[str, str] | None = None) -> str:
    header = "[Canal: Telegram | El usuario te escribe desde Telegram, no está delante de su ordenador.]"
    if media_info:
        media_path, media_type = media_info
        header += f"\n[Archivo adjunto recibido por Telegram: {media_path} (tipo: {media_type})]"
    return f"{header}\n\n{prompt}"


async def process_prompt(update: Update, prompt: str, media_info: tuple[str, str] | None = None):
    global force_new_session
    status_msg = await update.message.reply_text("pensando...")

    full_prompt = _build_telegram_prompt(prompt, media_info=media_info)

    cmd = [
        "agy",
        "--dangerously-skip-permissions",
        "--mode", "auto",
        "--add-dir", os.path.expanduser("~"),
        "--add-dir", str(MEDIA_DIR),
    ]
    if not force_new_session:
        cmd.append("-c")
    cmd.extend(["-p", full_prompt])

    env = os.environ.copy()
    env["PATH"] = f"{os.path.expanduser('~/.local/bin')}:{env.get('PATH', '')}"

    try:
        proc = await asyncio.to_thread(
            subprocess.run, cmd, cwd=os.path.expanduser("~"), env=env, capture_output=True, text=True
        )
        force_new_session = False
        output = (proc.stdout or proc.stderr or "Tarea completada.").strip()
    except Exception as e:
        output = f"Error al ejecutar agy: {e}"

    await status_msg.delete()
    await send_formatted_reply(update, output)

    lower_prompt = prompt.lower()
    if any(k in lower_prompt for k in ["captura", "pantalla", "foto"]) and media_info is None:
        if any(k in lower_prompt for k in ["monitor 1", "pantalla 1", "/screen 1"]) or (" 1" in lower_prompt and " 2" not in lower_prompt):
            await send_monitor_screenshot(update, "1")
        elif any(k in lower_prompt for k in ["monitor 2", "pantalla 2", "/screen 2"]) or (" 2" in lower_prompt and " 1" not in lower_prompt):
            await send_monitor_screenshot(update, "2")
        elif any(k in lower_prompt for k in ["activo", "activa", "focused"]):
            await send_monitor_screenshot(update, "focused")
        else:
            await send_monitor_screenshot(update, "all")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update):
        return
    await process_prompt(update, update.message.text)


def _guess_extension(mime_type: str | None, fallback: str) -> str:
    if mime_type:
        ext = mimetypes.guess_extension(mime_type)
        if ext:
            return ext
    return fallback


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update):
        return

    msg = update.message
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    media_type = "archivo"
    tg_file = None
    filename = f"{timestamp}_file"

    if msg.photo:
        media_type = "imagen"
        tg_file = await msg.photo[-1].get_file()
        filename = f"{timestamp}_photo.jpg"
    elif msg.video:
        media_type = "video"
        tg_file = await msg.video.get_file()
        ext = _guess_extension(msg.video.mime_type, ".mp4")
        filename = f"{timestamp}_video{ext}"
    elif msg.animation:
        media_type = "animacion"
        tg_file = await msg.animation.get_file()
        ext = _guess_extension(msg.animation.mime_type, ".mp4")
        filename = f"{timestamp}_anim{ext}"
    elif msg.video_note:
        media_type = "videomensaje"
        tg_file = await msg.video_note.get_file()
        filename = f"{timestamp}_videonote.mp4"
    elif msg.document:
        media_type = "documento"
        tg_file = await msg.document.get_file()
        orig_name = Path(msg.document.file_name or "documento").name
        safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", orig_name)
        filename = f"{timestamp}_{safe_name}"

    if tg_file is None:
        return

    local_path = MEDIA_DIR / filename
    await tg_file.download_to_drive(str(local_path))

    caption = (msg.caption or "").strip()
    if caption:
        user_prompt = caption
    else:
        user_prompt = (
            f"El usuario ha enviado este archivo ({media_type}) sin texto adicional. "
            "Usa read_file para verlo/analizarlo si es compatible y responde describiéndolo brevemente "
            "o preguntando qué necesita hacer con él."
        )

    await process_prompt(update, user_prompt, media_info=(str(local_path), media_type))


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update):
        return

    voice = update.message.voice or update.message.audio
    voice_file = await voice.get_file()

    ogg_path = "/tmp/voice_input.ogg"
    wav_path = "/tmp/voice_input.wav"

    await voice_file.download_to_drive(ogg_path)

    await asyncio.to_thread(
        subprocess.run,
        ["ffmpeg", "-y", "-i", ogg_path, "-ar", "16000", "-ac", "1", wav_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    status_trans = await update.message.reply_text("Transcribiendo audio...")

    result = await asyncio.to_thread(whisper_model.transcribe, wav_path, language="es")
    text = result["text"].strip()

    await status_trans.edit_text(f"Transcripción: {text}")

    for p in [ogg_path, wav_path]:
        if os.path.exists(p):
            os.remove(p)

    if text:
        await process_prompt(update, text)
    else:
        await update.message.reply_text("No se pudo entender el audio.")


if __name__ == "__main__":
    token = load_token()
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("screen", cmd_screen))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(
        MessageHandler(
            filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.VIDEO_NOTE | filters.Document.ALL,
            handle_media,
        )
    )
    print("Bot agy-telegram activo...", flush=True)
    app.run_polling()
