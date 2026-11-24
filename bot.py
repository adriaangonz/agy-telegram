#!/usr/bin/env python3
import asyncio
import html
import json
import mimetypes
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

import whisper
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

CONFIG_DIR = Path.home() / ".config"
ID_FILE = CONFIG_DIR / "agy_telegram_uid"
TOKEN_FILE = CONFIG_DIR / "agy_telegram_token"
MEDIA_DIR = Path("/tmp/agy_telegram_media")
OUTBOX_DIR = Path("/tmp/agy_telegram_outbox")
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
OUTBOX_DIR.mkdir(parents=True, exist_ok=True)

VOICE_ENGINE_PY = Path.home() / "Documents/skills/voice-engine/.venv/bin/python"
VOICE_ENGINE_SCRIPT = Path.home() / "Documents/skills/voice-engine/voice_engine.py"
NOTIFY_HOST = "127.0.0.1"
NOTIFY_PORT = int(os.environ.get("AGY_NOTIFY_PORT", "8765"))
MEDIA_TTL_DAYS = 7

force_new_session = False
voice_reply_mode = False
current_cwd = Path.home()
agy_lock = asyncio.Lock()
current_proc: asyncio.subprocess.Process | None = None
cancel_requested = False
active_reminders: list[dict] = []

print("Cargando modelo de transcripcion Whisper...")
whisper_model = whisper.load_model("base")


def _base_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = f"{os.path.expanduser('~/.local/bin')}:{env.get('PATH', '')}"
    return env


def cleanup_old_media(max_age_days: int = MEDIA_TTL_DAYS) -> int:
    """Remove temporary files older than max_age_days from MEDIA_DIR and OUTBOX_DIR."""
    cutoff = time.time() - (max_age_days * 86400)
    removed = 0
    for folder in (MEDIA_DIR, OUTBOX_DIR):
        if not folder.exists():
            continue
        for item in folder.iterdir():
            try:
                if item.stat().st_mtime < cutoff:
                    if item.is_dir():
                        shutil.rmtree(item, ignore_errors=True)
                    else:
                        item.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                pass
    return removed


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
        if update.message:
            await update.message.reply_text(
                f"Dispositivo vinculado. Tu ID ({uid}) ha quedado registrado como único dueño."
            )
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


async def send_monitor_screenshot(message, target: str = "all"):
    env = _base_env()
    try:
        proc = await asyncio.to_thread(
            subprocess.run, ["hyprctl", "monitors", "-j"], capture_output=True, text=True, env=env, timeout=5
        )
        mons = json.loads(proc.stdout)
        mons.sort(key=lambda m: m.get("id", 0))
    except Exception:
        mons = []

    if mons and all(m.get("dpmsStatus") is False for m in mons):
        await message.reply_text("No se puede capturar la pantalla: los monitores están apagados (DPMS off).")
        return

    target_str = str(target).strip().lower()

    if target_str in ["all", "", "separado", "separados", "separada", "separadas", "ambos", "ambas", "todos", "todas"]:
        if not mons:
            img_path = "/tmp/tg_screen.png"
            ok, err = await _take_screenshot(img_path, "all", env)
            if ok:
                with open(img_path, "rb") as photo:
                    await message.reply_photo(photo=photo, caption="Pantalla completa")
            else:
                await message.reply_text(err)
            return

        for m in mons:
            m_num = m.get("id", 0) + 1
            m_name = m.get("name", f"DP-{m_num}")
            if m.get("dpmsStatus") is False:
                await message.reply_text(f"Monitor {m_num} ({m_name}) está apagado (DPMS off).")
                continue
            img_path = f"/tmp/tg_screen_{m_num}.png"
            ok, err = await _take_screenshot(img_path, str(m_num), env)
            if ok:
                with open(img_path, "rb") as photo:
                    await message.reply_photo(photo=photo, caption=f"Monitor {m_num} ({m_name})")
            else:
                await message.reply_text(f"Monitor {m_num} ({m_name}): {err}")
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
                await message.reply_photo(photo=photo, caption=caption)
        else:
            await message.reply_text(err)


async def send_file_to_telegram(bot, chat_id: int, file_path: Path, caption: str | None = None):
    """Send a local file to Telegram using the most appropriate media method."""
    if not file_path.exists() or not file_path.is_file():
        return
    ext = file_path.suffix.lower()
    cap = (caption or file_path.name)[:1024]
    with open(file_path, "rb") as f:
        if ext in {".png", ".jpg", ".jpeg", ".webp"}:
            await bot.send_photo(chat_id=chat_id, photo=f, caption=cap)
        elif ext in {".mp4", ".mov", ".mkv", ".webm"}:
            await bot.send_video(chat_id=chat_id, video=f, caption=cap)
        elif ext in {".ogg", ".opus"}:
            await bot.send_voice(chat_id=chat_id, voice=f, caption=cap)
        elif ext in {".mp3", ".wav", ".flac", ".m4a"}:
            await bot.send_audio(chat_id=chat_id, audio=f, caption=cap)
        else:
            await bot.send_document(chat_id=chat_id, document=f, caption=cap)


async def _dispatch_outbox_and_tags(bot, chat_id: int, output: str) -> str:
    """Extract [[SEND_FILE:...]] tags and send any files placed in OUTBOX_DIR."""
    files_to_send: list[Path] = []

    def _collect_tag(match: re.Match) -> str:
        p = Path(match.group(1).strip()).expanduser()
        if p.exists() and p.is_file():
            files_to_send.append(p)
        return ""

    cleaned = re.sub(r"\[\[SEND_FILE:([^\]]+)\]\]", _collect_tag, output).strip()

    if OUTBOX_DIR.exists():
        for item in sorted(OUTBOX_DIR.iterdir()):
            if item.is_file() and item not in files_to_send:
                files_to_send.append(item)

    for fpath in files_to_send:
        try:
            await send_file_to_telegram(bot, chat_id, fpath)
        except Exception as e:
            await bot.send_message(chat_id=chat_id, text=f"No se pudo enviar {fpath.name}: {e}")
        finally:
            if fpath.parent == OUTBOX_DIR:
                fpath.unlink(missing_ok=True)

    return cleaned or "Archivo(s) enviado(s)."


def _gather_system_status() -> str:
    """Collect lightweight system metrics without external Python dependencies."""
    lines: list[str] = []

    try:
        load1, load5, load15 = os.getloadavg()
        cores = os.cpu_count() or 1
        lines.append(f"• <b>CPU load</b>: {load1:.2f} / {load5:.2f} / {load15:.2f} ({cores} hilos)")
    except OSError:
        pass

    try:
        meminfo = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            k, v = line.split(":", 1)
            meminfo[k] = int(v.strip().split()[0])
        total_gb = meminfo["MemTotal"] / 1048576
        avail_gb = meminfo.get("MemAvailable", 0) / 1048576
        used_gb = total_gb - avail_gb
        pct = (used_gb / total_gb * 100) if total_gb else 0
        lines.append(f"• <b>RAM</b>: {used_gb:.1f} / {total_gb:.1f} GiB ({pct:.0f}%)")
    except Exception:
        pass

    try:
        du = shutil.disk_usage("/")
        used_gb = du.used / (1024**3)
        total_gb = du.total / (1024**3)
        pct = du.used / du.total * 100
        lines.append(f"• <b>Disco /</b>: {used_gb:.1f} / {total_gb:.1f} GiB ({pct:.0f}%)")
    except OSError:
        pass

    temps: list[str] = []
    for z in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
        try:
            ztype = (z / "type").read_text(encoding="utf-8").strip()
            t_c = int((z / "temp").read_text(encoding="utf-8").strip()) / 1000.0
            if t_c > 0 and ("pkg" in ztype.lower() or "cpu" in ztype.lower() or "tctl" in ztype.lower() or not temps):
                temps.append(f"{t_c:.0f}°C")
                if len(temps) >= 2:
                    break
        except Exception:
            pass
    if temps:
        lines.append(f"• <b>Temp</b>: {', '.join(temps)}")

    try:
        up_sec = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
        h, rem = divmod(int(up_sec), 3600)
        m, _ = divmod(rem, 60)
        lines.append(f"• <b>Uptime</b>: {h}h {m}m")
    except Exception:
        pass

    try:
        env = _base_env()
        vol_proc = subprocess.run(
            ["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"],
            capture_output=True, text=True, env=env, timeout=3,
        )
        if vol_proc.returncode == 0:
            lines.append(f"• <b>Audio</b>: {html.escape(vol_proc.stdout.strip())}")
    except Exception:
        pass

    try:
        env = _base_env()
        mon_proc = subprocess.run(["hyprctl", "monitors", "-j"], capture_output=True, text=True, env=env, timeout=3)
        if mon_proc.returncode == 0:
            mons = json.loads(mon_proc.stdout)
            mon_states = ", ".join(
                f"{m.get('name', '?')}:{'ON' if m.get('dpmsStatus', True) else 'OFF'}" for m in mons
            )
            lines.append(f"• <b>Monitores</b>: {mon_states}")
    except Exception:
        pass

    lines.append(f"• <b>Directorio</b>: <code>{html.escape(str(current_cwd))}</code>")
    lines.append(f"• <b>Modo voz</b>: {'Activado' if voice_reply_mode else 'Desactivado'}")
    return "<b>Panel del Sistema</b>\n\n" + "\n".join(lines)


def _panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Actualizar", callback_data="panel_refresh"),
            InlineKeyboardButton("📸 Captura", callback_data="panel_screen"),
        ],
        [
            InlineKeyboardButton("🔉 -5%", callback_data="vol_down"),
            InlineKeyboardButton("🔇 Mute", callback_data="vol_mute"),
            InlineKeyboardButton("🔊 +5%", callback_data="vol_up"),
        ],
        [
            InlineKeyboardButton("🖥️ Monitores ON", callback_data="dpms_on"),
            InlineKeyboardButton("🌑 Monitores OFF", callback_data="dpms_off"),
        ],
        [
            InlineKeyboardButton("🪟 Ventanas", callback_data="panel_windows"),
            InlineKeyboardButton("📋 Portapapeles", callback_data="panel_clip"),
        ],
    ])


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update):
        return
    await update.message.reply_text(
        "Agy conectado a tu Arch Linux.\n\n"
        "Comandos:\n"
        "/screen [1|2|focused] - Captura de pantallas\n"
        "/panel - Panel rápido de sistema, audio y monitores\n"
        "/clip [texto] - Leer o escribir en el portapapeles del PC\n"
        "/cd [ruta] - Ver o cambiar directorio de trabajo\n"
        "/voice [on|off|once] - Respuestas habladas con tu voz clonada\n"
        "/remind <tiempo> <texto> - Recordatorio (ej: 30m revisar backup)\n"
        "/watch <pid> [nombre] - Avisar cuando termine un proceso\n"
        "/cancel - Cancelar tarea actual de agy\n"
        "/new - Reiniciar sesión de conversación\n\n"
        "Puedes enviar texto, notas de voz, imágenes, vídeos o archivos."
    )


async def cmd_screen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update):
        return
    mon_arg = context.args[0] if context.args else "all"
    await send_monitor_screenshot(update.message, mon_arg)


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global force_new_session
    if not await check_auth(update):
        return
    force_new_session = True
    await update.message.reply_text("Nueva sesión de conversación iniciada con agy.")


async def cmd_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update):
        return
    status_text = await asyncio.to_thread(_gather_system_status)
    await update.message.reply_text(status_text, parse_mode=ParseMode.HTML, reply_markup=_panel_keyboard())


async def cmd_clip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update):
        return
    env = _base_env()
    text_to_copy = " ".join(context.args).strip()
    if text_to_copy:
        proc = await asyncio.to_thread(
            subprocess.run, ["wl-copy"], input=text_to_copy, text=True, env=env, capture_output=True, timeout=5
        )
        if proc.returncode == 0:
            await update.message.reply_text("Texto copiado al portapapeles del PC.")
        else:
            await update.message.reply_text(f"Error al copiar: {proc.stderr.strip()}")
    else:
        proc = await asyncio.to_thread(
            subprocess.run, ["wl-paste", "--no-newline"], text=True, env=env, capture_output=True, timeout=5
        )
        content = proc.stdout.strip()
        if proc.returncode == 0 and content:
            await send_formatted_reply(update.message, f"Portapapeles actual:\n```\n{content[:3000]}\n```")
        else:
            await update.message.reply_text("El portapapeles está vacío o no contiene texto.")


async def cmd_cd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global current_cwd
    if not await check_auth(update):
        return
    if not context.args:
        await update.message.reply_text(f"Directorio actual de trabajo:\n{current_cwd}")
        return
    target = Path(" ".join(context.args).strip()).expanduser().resolve()
    if target.exists() and target.is_dir():
        current_cwd = target
        await update.message.reply_text(f"Directorio de trabajo cambiado a:\n{current_cwd}")
    else:
        await update.message.reply_text(f"No existe el directorio: {target}")


async def cmd_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global voice_reply_mode
    if not await check_auth(update):
        return
    if not VOICE_ENGINE_SCRIPT.exists():
        await update.message.reply_text("Motor de voz no encontrado en ~/Documents/skills/voice-engine.")
        return
    arg = context.args[0].lower() if context.args else ""
    if arg in {"on", "1", "si", "sí", "activar"}:
        voice_reply_mode = True
    elif arg in {"off", "0", "no", "desactivar"}:
        voice_reply_mode = False
    else:
        voice_reply_mode = not voice_reply_mode
    estado = "ACTIVADO" if voice_reply_mode else "DESACTIVADO"
    await update.message.reply_text(f"Modo respuesta por voz: {estado}.")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update):
        return
    cancelled = await _cancel_running_agy()
    if cancelled:
        await update.message.reply_text("Cancelando tarea actual de agy...")
    else:
        await update.message.reply_text("No hay ninguna tarea ejecutándose ahora mismo.")


def _parse_duration(spec: str) -> int | None:
    m = re.fullmatch(r"(\d+)\s*([smhd]?)", spec.strip().lower())
    if not m:
        return None
    val = int(m.group(1))
    unit = m.group(2) or "m"
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return val * mult


async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update):
        return
    if not context.args or context.args[0].lower() == "list":
        if not active_reminders:
            await update.message.reply_text("No tienes recordatorios activos.")
            return
        now = datetime.now()
        lines = []
        for r in active_reminders:
            rem_min = max(0, int((r["at"] - now).total_seconds() // 60))
            lines.append(f"• {r['text']} (en ~{rem_min} min, a las {r['at'].strftime('%H:%M')})")
        await update.message.reply_text("Recordatorios activos:\n" + "\n".join(lines))
        return

    seconds = _parse_duration(context.args[0])
    msg_text = " ".join(context.args[1:]).strip()
    if not seconds or not msg_text:
        await update.message.reply_text("Uso: /remind <tiempo> <mensaje>\nEjemplo: /remind 30m revisar backup")
        return

    trigger_at = datetime.now() + timedelta(seconds=seconds)
    entry = {"at": trigger_at, "text": msg_text}
    active_reminders.append(entry)
    chat_id = update.effective_chat.id
    bot = context.bot

    async def _job():
        try:
            await asyncio.sleep(seconds)
            await bot.send_message(chat_id=chat_id, text=f"⏰ Recordatorio:\n{msg_text}")
        finally:
            if entry in active_reminders:
                active_reminders.remove(entry)

    asyncio.create_task(_job())
    await update.message.reply_text(f"Recordatorio programado para las {trigger_at.strftime('%H:%M:%S')}.")


async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Uso: /watch <PID> [descripción]\nEjemplo: /watch 12345 compilación")
        return

    pid = int(context.args[0])
    label = " ".join(context.args[1:]).strip() or f"PID {pid}"
    proc_dir = Path(f"/proc/{pid}")
    if not proc_dir.exists():
        await update.message.reply_text(f"El proceso {pid} no existe o ya terminó.")
        return

    chat_id = update.effective_chat.id
    bot = context.bot
    start_ts = time.time()

    async def _watcher():
        while proc_dir.exists():
            await asyncio.sleep(3)
        elapsed = int(time.time() - start_ts)
        await bot.send_message(chat_id=chat_id, text=f"✅ Proceso finalizado: {label} (PID {pid}, {elapsed}s).")

    asyncio.create_task(_watcher())
    await update.message.reply_text(f"Vigilando proceso {pid} ({label}). Te avisaré en cuanto termine.")


async def _cancel_running_agy() -> bool:
    global cancel_requested
    proc = current_proc
    if proc is None or proc.returncode is not None:
        return False
    cancel_requested = True
    try:
        proc.terminate()
    except ProcessLookupError:
        return False
    return True


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await check_auth(update):
        await query.answer("No autorizado.", show_alert=True)
        return

    data = query.data or ""
    env = _base_env()

    if data == "cancel_agy":
        ok = await _cancel_running_agy()
        await query.answer("Cancelando..." if ok else "Ya finalizó.")
        return

    if data == "panel_refresh":
        status_text = await asyncio.to_thread(_gather_system_status)
        try:
            await query.edit_message_text(status_text, parse_mode=ParseMode.HTML, reply_markup=_panel_keyboard())
        except Exception:
            pass
        await query.answer("Actualizado")
        return

    if data == "panel_screen":
        await query.answer("Capturando...")
        await send_monitor_screenshot(query.message, "all")
        return

    if data in {"vol_up", "vol_down", "vol_mute"}:
        if data == "vol_up":
            cmd = ["wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", "5%+"]
        elif data == "vol_down":
            cmd = ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "5%-"]
        else:
            cmd = ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"]
        await asyncio.to_thread(subprocess.run, cmd, env=env, capture_output=True, timeout=3)
        status_text = await asyncio.to_thread(_gather_system_status)
        try:
            await query.edit_message_text(status_text, parse_mode=ParseMode.HTML, reply_markup=_panel_keyboard())
        except Exception:
            pass
        await query.answer("Audio actualizado")
        return

    if data in {"dpms_on", "dpms_off"}:
        mode = "on" if data == "dpms_on" else "off"
        await asyncio.to_thread(
            subprocess.run, ["hyprctl", "dispatch", "dpms", mode], env=env, capture_output=True, timeout=3
        )
        await asyncio.sleep(0.4)
        status_text = await asyncio.to_thread(_gather_system_status)
        try:
            await query.edit_message_text(status_text, parse_mode=ParseMode.HTML, reply_markup=_panel_keyboard())
        except Exception:
            pass
        await query.answer(f"Monitores {mode.upper()}")
        return

    if data == "panel_windows":
        await query.answer()
        proc = await asyncio.to_thread(
            subprocess.run, ["hyprctl", "clients", "-j"], env=env, capture_output=True, text=True, timeout=4
        )
        try:
            clients = json.loads(proc.stdout)
            lines = [
                f"• [{c.get('workspace', {}).get('name', '?')}] {c.get('class', '?')} — {c.get('title', '')[:45]}"
                for c in clients if c.get("mapped")
            ]
            msg = "Ventanas abiertas:\n" + ("\n".join(lines[:20]) if lines else "Ninguna")
        except Exception:
            msg = "No se pudieron listar las ventanas."
        await query.message.reply_text(msg)
        return

    if data == "panel_clip":
        await query.answer()
        proc = await asyncio.to_thread(
            subprocess.run, ["wl-paste", "--no-newline"], text=True, env=env, capture_output=True, timeout=4
        )
        content = proc.stdout.strip()
        if proc.returncode == 0 and content:
            await send_formatted_reply(query.message, f"Portapapeles actual:\n```\n{content[:3000]}\n```")
        else:
            await query.message.reply_text("El portapapeles está vacío.")
        return


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


async def send_formatted_reply(message, output: str):
    for chunk in _split_markdown_chunks(output):
        formatted = markdown_to_telegram_html(chunk)
        if not formatted:
            continue
        try:
            await message.reply_text(
                formatted,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception:
            for i in range(0, len(chunk), 4000):
                await message.reply_text(chunk[i:i + 4000])


async def _synthesize_and_send_voice(message, text: str):
    """Synthesize a concise voice note using the local voice engine when voice_reply_mode is enabled."""
    if not VOICE_ENGINE_PY.exists() or not VOICE_ENGINE_SCRIPT.exists():
        return
    clean = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    clean = re.sub(r"[`*_#>\[\]]", "", clean)
    clean = re.sub(r"\s+", " ", clean).strip()[:500]
    if not clean:
        return

    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    wav_out = Path(f"/tmp/tg_tts_{ts}.wav")
    ogg_out = Path(f"/tmp/tg_tts_{ts}.ogg")
    try:
        proc = await asyncio.create_subprocess_exec(
            str(VOICE_ENGINE_PY),
            str(VOICE_ENGINE_SCRIPT),
            "--text", clean,
            "--out", str(wav_out),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=120)
        if wav_out.exists():
            conv = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", str(wav_out), "-c:a", "libopus", "-b:a", "48k", str(ogg_out),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(conv.wait(), timeout=30)
            if ogg_out.exists():
                with open(ogg_out, "rb") as vf:
                    await message.reply_voice(voice=vf)
    except Exception:
        pass
    finally:
        wav_out.unlink(missing_ok=True)
        ogg_out.unlink(missing_ok=True)


def _build_telegram_prompt(prompt: str, media_info: tuple[str, str] | None = None) -> str:
    header = (
        "[Canal: Telegram | El usuario te escribe desde Telegram, no está delante de su ordenador. "
        f"Si necesitas enviarle un archivo, imagen, vídeo o audio, guárdalo en {OUTBOX_DIR} "
        "o escribe [[SEND_FILE:/ruta/absoluta]] en tu respuesta.]"
    )
    if media_info:
        media_path, media_type = media_info
        header += f"\n[Archivo adjunto recibido por Telegram: {media_path} (tipo: {media_type})]"
    return f"{header}\n\n{prompt}"


async def process_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str, media_info: tuple[str, str] | None = None):
    global force_new_session, current_proc, cancel_requested

    if agy_lock.locked():
        await update.message.reply_text(
            "Ya hay una tarea de agy en curso. Espera a que termine o usa /cancel para detenerla."
        )
        return

    async with agy_lock:
        cancel_requested = False
        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data="cancel_agy")]])
        status_msg = await update.message.reply_text("pensando...", reply_markup=cancel_kb)

        full_prompt = _build_telegram_prompt(prompt, media_info=media_info)

        cmd = [
            "agy",
            "--dangerously-skip-permissions",
            "--mode", "auto",
            "--add-dir", str(current_cwd),
            "--add-dir", str(MEDIA_DIR),
            "--add-dir", str(OUTBOX_DIR),
        ]
        if not force_new_session:
            cmd.append("-c")
        cmd.extend(["-p", full_prompt])

        env = _base_env()
        started_at = time.monotonic()
        done_event = asyncio.Event()

        async def _progress_ticker():
            while not done_event.is_set():
                try:
                    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
                except Exception:
                    pass
                await asyncio.sleep(5)
                if done_event.is_set():
                    break
                elapsed = int(time.monotonic() - started_at)
                try:
                    await status_msg.edit_text(f"pensando... ({elapsed}s)", reply_markup=cancel_kb)
                except Exception:
                    pass

        ticker_task = asyncio.create_task(_progress_ticker())

        try:
            current_proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(current_cwd),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, stderr_b = await current_proc.communicate()
            if cancel_requested:
                output = "Tarea cancelada por el usuario."
            else:
                force_new_session = False
                stdout_s = stdout_b.decode("utf-8", errors="replace").strip()
                stderr_s = stderr_b.decode("utf-8", errors="replace").strip()
                output = stdout_s or stderr_s or "Tarea completada."
        except Exception as e:
            output = f"Error al ejecutar agy: {e}"
        finally:
            current_proc = None
            done_event.set()
            ticker_task.cancel()
            try:
                await ticker_task
            except asyncio.CancelledError:
                pass

        try:
            await status_msg.delete()
        except Exception:
            pass

        output = await _dispatch_outbox_and_tags(context.bot, update.effective_chat.id, output)
        await send_formatted_reply(update.message, output)

        if voice_reply_mode and not cancel_requested:
            await _synthesize_and_send_voice(update.message, output)

        lower_prompt = prompt.lower()
        if any(k in lower_prompt for k in ["captura", "pantalla", "foto"]) and media_info is None and not cancel_requested:
            if any(k in lower_prompt for k in ["monitor 1", "pantalla 1", "/screen 1"]) or (" 1" in lower_prompt and " 2" not in lower_prompt):
                await send_monitor_screenshot(update.message, "1")
            elif any(k in lower_prompt for k in ["monitor 2", "pantalla 2", "/screen 2"]) or (" 2" in lower_prompt and " 1" not in lower_prompt):
                await send_monitor_screenshot(update.message, "2")
            elif any(k in lower_prompt for k in ["activo", "activa", "focused"]):
                await send_monitor_screenshot(update.message, "focused")
            else:
                await send_monitor_screenshot(update.message, "all")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update):
        return
    await process_prompt(update, context, update.message.text)


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

    await process_prompt(update, context, user_prompt, media_info=(str(local_path), media_type))


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update):
        return

    voice = update.message.voice or update.message.audio
    voice_file = await voice.get_file()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    ogg_path = f"/tmp/voice_input_{ts}.ogg"
    wav_path = f"/tmp/voice_input_{ts}.wav"

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

    try:
        await status_trans.edit_text(f"Transcripción: {text}")
    except Exception:
        pass

    for p in (ogg_path, wav_path):
        if os.path.exists(p):
            os.remove(p)

    if text:
        await process_prompt(update, context, text)
    else:
        await update.message.reply_text("No se pudo entender el audio.")


async def _handle_notify_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, bot):
    """Minimal local HTTP server on 127.0.0.1:8765 for proactive notifications."""
    try:
        raw = await asyncio.wait_for(reader.read(65536), timeout=5)
        if not raw:
            return
        head, _, body = raw.partition(b"\r\n\r\n")
        body_text = body.decode("utf-8", errors="replace").strip()

        # If Content-Length wasn't fully read in the first chunk, read the rest
        cl_match = re.search(rb"Content-Length:\s*(\d+)", head, re.IGNORECASE)
        if cl_match:
            expected = int(cl_match.group(1))
            while len(body) < expected:
                more = await asyncio.wait_for(reader.read(expected - len(body)), timeout=5)
                if not more:
                    break
                body += more
            body_text = body.decode("utf-8", errors="replace").strip()

        owner = get_owner_id()
        if owner is None:
            writer.write(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 16\r\n\r\nNo owner linked.")
            await writer.drain()
            return

        text = body_text
        file_path = None
        if body_text.startswith("{"):
            try:
                payload = json.loads(body_text)
                text = str(payload.get("text", "")).strip()
                if payload.get("file"):
                    file_path = Path(str(payload["file"])).expanduser()
            except json.JSONDecodeError:
                pass

        if text:
            await bot.send_message(chat_id=owner, text=f"🔔 {text}")
        if file_path and file_path.exists():
            await send_file_to_telegram(bot, owner, file_path)

        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
        await writer.drain()
    except Exception:
        try:
            writer.write(b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 5\r\n\r\nError")
            await writer.drain()
        except Exception:
            pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def post_init(app: Application):
    cleanup_old_media()
    server = await asyncio.start_server(
        lambda r, w: _handle_notify_client(r, w, app.bot),
        NOTIFY_HOST,
        NOTIFY_PORT,
    )
    app.bot_data["notify_server"] = server
    print(f"Webhook de notificaciones activo en http://{NOTIFY_HOST}:{NOTIFY_PORT}/notify", flush=True)


async def post_shutdown(app: Application):
    server = app.bot_data.get("notify_server")
    if server is not None:
        server.close()
        await server.wait_closed()


if __name__ == "__main__":
    token = load_token()
    app = (
        ApplicationBuilder()
        .token(token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("screen", cmd_screen))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("panel", cmd_panel))
    app.add_handler(CommandHandler("clip", cmd_clip))
    app.add_handler(CommandHandler("cd", cmd_cd))
    app.add_handler(CommandHandler("voice", cmd_voice))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("remind", cmd_remind))
    app.add_handler(CommandHandler("watch", cmd_watch))
    app.add_handler(CallbackQueryHandler(handle_callback))
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
