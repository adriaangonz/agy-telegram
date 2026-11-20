# agy-telegram

Puente de Telegram hacia Antigravity CLI (`agy`) ejecutado como servicio systemd de usuario (`agy-telegram.service`).

## Estructura
- `bot.py`: Bot principal (comandos `/start`, `/screen`, `/new`, texto, voz con Whisper, imágenes, vídeos y documentos).
- `~/.local/bin/agy-telegram`: Enlace simbólico a `/home/adrian/agy-telegram/bot.py`.
- `~/.config/agy_telegram_token`: Token del bot de Telegram (`chmod 600`).
- `~/.config/agy_telegram_uid`: ID de usuario de Telegram autorizado.
- `/tmp/agy_telegram_media/`: Carpeta temporal donde se descargan fotos, vídeos y documentos recibidos por Telegram.

## Gestión del servicio
```bash
systemctl --user status agy-telegram.service
systemctl --user restart agy-telegram.service
journalctl --user -u agy-telegram.service -f
```
