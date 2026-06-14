# MAX ↔ Telegram Bridge: текущая стадия

## Проект

Путь: `C:\Claude\max2tg`

Цель: локальный мост между личным аккаунтом MAX и Telegram-группой с Topics.

UX: каждая тема Telegram = отдельный MAX-диалог, группа или канал.

## Текущий статус

Мост запущен и работает локально.

Последний известный процесс:

- parent PID: `25844`
- child PID: `25904`

Лог:

- `C:\Claude\max2tg\bridge.log`

Состояние:

- `C:\Claude\max2tg\state.json`

Конфиг:

- `C:\Claude\max2tg\config.json`
- содержит live-токены, не печатать и не коммитить.

## Основная архитектура

### MAX side

Используется неофициальный WebSocket API MAX через `vkmax` и кастомный `BrowserMaxClient`.

Файлы:

- `max_client.py`
- `bridge.py`
- `mediamax.py`
- `attaches.py`

MAX login:

- токен из `web.max.ru`;
- `opcode=19`;
- browser-like headers;
- актуальная web app version.

Incoming MAX messages:

- WebSocket event `opcode=128`;
- обработчик: `MaxToTelegramBridge._on_packet`.

Message sending:

- текст: `vkmax.functions.messages.send_message`;
- reply: `vkmax.functions.messages.reply_message`;
- загрузка файлов в MAX:
  - `opcode=87` получить upload slot;
  - HTTP POST bytes на `slot.url`;
  - `opcode=64` отправить сообщение с attach:

```json
{"_type": "FILE", "fileId": "..."}
```

### Telegram side

Используется Telegram Bot API через `requests`.

Файл:

- `tg.py`

Polling:

- `getUpdates`;
- allowed updates: `message`.

Forum topics:

- `createForumTopic`;
- `editForumTopic`;
- `sendMessage/sendPhoto/sendDocument/...` с `message_thread_id`.

## Topics Mode

Включён режим Topics.

Конфиг включает:

```json
{
  "telegram_topics_enabled": true,
  "telegram_forum_chat_id": -1004336585084,
  "telegram_fallback_chat_id": 2097379459,
  "telegram_preload_topics": true,
  "telegram_seed_last_messages": true,
  "telegram_preload_chat_count": 100
}
```

Поведение:

- каждый MAX chat id мапится на Telegram `message_thread_id`;
- mapping хранится в `state.json`;
- при рестарте темы не создаются повторно;
- если topic создать нельзя, fallback идёт в обычный Telegram chat id.

## Предзагрузка чатов

При старте:

- login MAX вызывается с `chatsSync=1`, `contactsSync=1`;
- MAX возвращает список последних чатов;
- мост создаёт недостающие Telegram topics;
- для уже созданных topics проверяет название и при необходимости переименовывает;
- `chat id = 0` фильтруется как служебный.

Была проблема:

- часть личных диалогов MAX не имела `title`;
- мост раньше создавал темы с цифрами вроде `14336601`, `165565406`;
- исправлено: для `DIALOG` берётся второй участник из `participants`, кроме own id, затем имя резолвится через `resolve_users`.

Переименованные темы:

- `14336601` → Александр
- `165565406` → Светлана
- `83325537` → Информер ЖКХ Липецкой области
- `18263449` → Елена
- `22560518` → GigaChat

## Seed Last Messages

Чтобы список Telegram topics выглядел как живые диалоги, а не как системная лента “Тема создана”:

- добавлен `telegram_seed_last_messages`;
- мост один раз отправляет последнее MAX-сообщение в тему;
- id последнего seeded MAX-сообщения хранится в `state.json`;
- при рестартах дублей нет.

Важно:

- системные Telegram сообщения “Тема создана” bot удалить не может;
- Telegram API возвращает `message can't be deleted`.

## Медиа MAX → Telegram

Файл: `attaches.py`

Поддерживается:

- text;
- photo;
- animation/GIF;
- sticker;
- video;
- voice/audio;
- document/file;
- share/link;
- contact/location как text notes;
- unknown attachments как text notes.

Для файлов/видео MAX:

- `mediamax.resolve_file_url`: `opcode=88`;
- `mediamax.resolve_video_url`: `opcode=83`;
- после resolve bridge загружает media в Telegram.

Telegram upload limit:

- hard cap около 49 MB.

Стикеры MAX:

- если Telegram принимает URL как sticker, отправляется sticker;
- если нет, fallback как document.

## Telegram → MAX

Текст:

- обычный текст внутри Telegram topic отправляется в связанный MAX chat;
- reply на пересланное сообщение отправляется в MAX как reply, если есть mapping в памяти.

Медиа:

- Telegram file/photo/video/audio/voice/sticker скачивается через Bot API:
  - `tg.getFile`;
  - `https://api.telegram.org/file/bot...`;
- затем загружается в MAX через `mediamax.send_uploaded_file`.

Telegram stickers:

- static → `.webp`;
- video → `.webm`;
- animated → `.tgs`;
- отправляются в MAX как file attachment, не как native MAX sticker.

Ограничение:

- Telegram sticker id и MAX sticker id несовместимы;
- “нативный” MAX sticker пока не реализован;
- при ошибке upload fallback: текстовая пометка `[Telegram sticker ...]`, `[Telegram file: ...]` и т.п.

## Важные файлы

- `main.py` — entrypoint, логирование, запуск bridge.
- `bridge.py` — основная логика маршрутизации MAX ↔ Telegram.
- `max_client.py` — browser-like MAX websocket client.
- `mediamax.py` — resolve MAX media и upload Telegram media в MAX.
- `attaches.py` — парсер MAX attachments.
- `tg.py` — минимальный Telegram Bot API client.
- `state.py` — persistent mapping MAX chat → Telegram topic.
- `config.py` — чтение `config.json` и env.
- `tests/test_topics.py` — topic routing tests.
- `tests/test_mediamax.py` — upload helper tests.

## Проверки

Последний прогон:

```powershell
venv\Scripts\python.exe -m unittest discover -s tests -v
venv\Scripts\python.exe -m py_compile main.py bridge.py config.py max_client.py state.py tg.py attaches.py mediamax.py
```

Результат:

- 13 tests OK;
- py_compile OK.

## Текущие ограничения и риски

1. MAX API неофициальный, reverse-engineered from `web.max.ru`.
2. Upload API MAX может измениться.
3. Telegram → MAX media сейчас отправляется как MAX file attachment, не native photo/sticker object.
4. Reply-map для конкретных пересланных сообщений хранится в памяти; после рестарта reply на старые Telegram-сообщения может не знать MAX message id. Обычное сообщение внутри topic всё равно уйдёт в правильный MAX chat.
5. Telegram Bot API polling конфликтует, если запущено больше одного процесса с тем же bot token. Следить за `409 Conflict`.
6. `config.json` содержит live Telegram/MAX tokens; не показывать и не коммитить.

## Как запускать

```powershell
cd C:\Claude\max2tg
venv\Scripts\python.exe main.py
```

Обычно запускается скрытым процессом через:

```powershell
Start-Process -FilePath "C:\Claude\max2tg\venv\Scripts\python.exe" `
  -ArgumentList "main.py" `
  -WorkingDirectory "C:\Claude\max2tg" `
  -WindowStyle Hidden
```

## Как проверить живой процесс

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'C:\\Claude\\max2tg|main\.py' } |
  Select-Object ProcessId, ParentProcessId, CommandLine |
  Format-List
```

## Что делать дальше

Приоритетные улучшения:

1. Протестировать Telegram sticker → MAX upload вручную в живом чате.
2. Если MAX принимает `.webp/.webm/.tgs` как файл, оставить как есть.
3. Если нужен именно native MAX sticker, надо реверсить endpoint/модель sticker assets и mapping stickerId.
4. Добавить persistent reply map, чтобы reply на старые Telegram-сообщения после рестарта мог цитировать MAX message id.
5. Добавить media album/grouped forwarding.
6. Добавить whitelist/blacklist MAX chats.
7. Добавить health command в Telegram, например `/status`.
