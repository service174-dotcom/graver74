# reel_fetch — приём видео в social_connector

Модуль забирает ролик (Instagram Reels, YouTube Shorts, TikTok, VK, RuTube и др.) на диск
роутера, отдаёт публичную ссылку на файл и транскрипт речи. Никакой авторизации Instagram
не требует: yt-dlp работает анонимно.

Зачем это вместо прежнего пути: браузерный `blob:` — это не ссылка на поток, а указатель
на байты в памяти страницы; og-скрапер в `instagram.py` (public-only ветка) Meta больше
не отдаёт теги `og:video` без логина. Поэтому «загрузчик требует авторизацию» — свойство
конкретного метода, а не самого видео.

## Установка

```bash
tar -xzf reel_fetch_deploy_20261101.tar.gz
cd reel_fetch_deploy
sudo bash deploy.sh --check                 # осмотр: что найдено, якоря, место на диске
sudo bash deploy.sh                         # установка
sudo bash deploy.sh --with-whisper          # + faster-whisper (лучше качество транскрипта)
sudo bash deploy.sh --smoke-url https://www.instagram.com/reel/<ID>/   # живая проверка
```

Все изменяемые файлы копируются в `/opt/social_connector/backups/reel-<UTC>/`.
Повторный запуск идемпотентен.

## Что появляется

| Компонент | Где |
|---|---|
| Модуль и REST-сайдкар | `/opt/social_connector/src/connector/reel_fetch.py`, `reel_sidecar.py` |
| Сервис | `reel-fetch.service`, `127.0.0.1:8791` (loopback, наружу только через Caddy) |
| Публичные маршруты | `https://social.78.29.36.63.sslip.io/media/*` (файлы), `/reel?action=...` (API) |
| Регистрация в MCP | `addons_call(module="reel_fetch", ...)`, если в `addons_bridge.py` найден реестр |
| Чистка | `/etc/cron.d/reel-fetch-cleanup`, удаляет файлы старше 7 дней |

## Действия

`fetch`, `probe`, `transcribe`, `list`, `clean`.

```bash
# REST
KEY=$(cat /home/routeradmin/.bridge_api_key)
curl -H "X-API-Key: $KEY" \
  "https://social.78.29.36.63.sslip.io/reel?action=fetch&url=https%3A%2F%2Fwww.instagram.com%2Freel%2F<ID>%2F&transcribe=1"

# MCP (ChatGPT / Claude)
addons_call(module="reel_fetch", action="fetch",
            params={"url": "https://www.instagram.com/reel/<ID>/", "transcribe": true})
```

Ответ:

```json
{"ok": true, "id": "…", "title": "…", "uploader": "…",
 "file": "/opt/social_connector/media/Instagram_<ID>.mp4", "file_bytes": 4000614,
 "file_url": "https://social.78.29.36.63.sslip.io/media/Instagram_<ID>.mp4",
 "audio_url": "https://social.78.29.36.63.sslip.io/media/Instagram_<ID>.m4a",
 "transcript": "…", "transcript_engine": "vosk"}
```

## Ограничения и оговорки

- Хосты загрузки ограничены списком (`instagram.com`, `tiktok.com`, `youtube.com`, `vk.com`,
  `rutube.ru`, `x.com`, …). Снять: `REEL_ALLOW_ANY=1` в юните.
- Скачивание сериализовано файловой блокировкой: один ролик за раз, без веера запросов.
- На 429 от сервиса возвращается понятная ошибка: подождать 10–15 минут, cookies не нужны.
- `/media/*` отдаётся без ключа (имена по id ролика). Нужна закрытая раздача —
  заменить `file_server` на прокси через сайдкар с проверкой ключа.
- Egress роутера идёт через VPS: Instagram на DC-IP отвечает, но при массовых прогонах
  возможен троттлинг.
- Транскрипт: по умолчанию используется уже развёрнутый `media_transcribe`; если он не умеет —
  `faster-whisper` (флаг `--with-whisper`) либо `vosk` (лёгкая модель ~45 МБ, качество ниже).

## Откат

```bash
sudo bash deploy.sh --revert
```

Снимает юнит и cron, восстанавливает `addons_bridge.py` и `Caddyfile` из последних
`*.bak-reel-*`, удаляет файлы модуля, перезапускает коннектор. Скачанные файлы в
`/opt/social_connector/media` остаются — удалить вручную при необходимости.
