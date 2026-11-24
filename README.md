# agy-telegram

Puente de Telegram hacia Antigravity CLI (`agy`) ejecutado como servicio systemd de usuario (`agy-telegram.service`).

## Comandos
- `/start` o `/help`: Muestra la ayuda y comandos disponibles.
- `/screen [1|2|focused]`: Captura las pantallas por separado en Hyprland.
- `/panel`: Panel interactivo con estado del sistema (CPU, RAM, disco, temperatura, audio, monitores) y botones de control rápido.
- `/clip [texto]`: Lee el portapapeles del PC (`wl-paste`) o copia texto al PC (`wl-copy`).
- `/cd [ruta]`: Consulta o cambia el directorio de trabajo de `agy`.
- `/voice [on|off]`: Activa o desactiva respuestas habladas con el motor local de voz clonada.
- `/remind <tiempo> <mensaje>`: Programa un recordatorio (ej. `/remind 30m revisar backup` o `/remind list`).
- `/watch <pid> [descripción]`: Vigila un proceso en ejecución y avisa por Telegram al finalizar.
- `/cancel`: Cancela la tarea actual de `agy` (también disponible mediante botón inline).
- `/new`: Reinicia la sesión de conversación con `agy`.

## Envío bidireccional de archivos
- **Entrantes**: Fotos, vídeos, notas de voz (Whisper), animaciones y documentos se guardan en `/tmp/agy_telegram_media/` (limpieza automática >7 días).
- **Salientes**: Cualquier archivo guardado por `agy` en `/tmp/agy_telegram_outbox/` o referenciado con `[[SEND_FILE:/ruta/archivo]]` se envía automáticamente a Telegram.

## Notificaciones proactivas (Webhook local)
El bot expone un servidor local en `http://127.0.0.1:8765/notify` (configurable con `AGY_NOTIFY_PORT`) para recibir alertas desde scripts o cronjobs:
```bash
curl -s -X POST http://127.0.0.1:8765/notify -d '{"text":"Compilación terminada"}'
curl -s -X POST http://127.0.0.1:8765/notify -d '{"text":"Informe listo","file":"/tmp/reporte.pdf"}'
```

## Estructura
- `bot.py`: Bot principal.
- `~/.local/bin/agy-telegram`: Enlace simbólico a `/home/adrian/agy-telegram/bot.py`.
- `~/.config/agy_telegram_token`: Token del bot de Telegram (`chmod 600`).
- `~/.config/agy_telegram_uid`: ID de usuario de Telegram autorizado.

## Gestión del servicio
```bash
systemctl --user status agy-telegram.service
systemctl --user restart agy-telegram.service
journalctl --user -u agy-telegram.service -f
```
