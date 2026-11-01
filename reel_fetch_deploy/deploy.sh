#!/usr/bin/env bash
# reel_fetch: полное развёртывание приёма видео в social_connector.
#
#   sudo bash deploy.sh --check                 # только осмотр, без изменений
#   sudo bash deploy.sh                         # установка
#   sudo bash deploy.sh --with-whisper          # + faster-whisper для транскрипта
#   sudo bash deploy.sh --smoke-url <URL>       # установка + живая проверка на ролике
#   sudo bash deploy.sh --revert                # полный откат
#
# Идемпотентно: повторный запуск ничего не ломает и не дублирует.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/social_connector}"
CADDYFILE="${CADDYFILE:-/etc/caddy/Caddyfile}"
DOMAIN="${DOMAIN:-social.78.29.36.63.sslip.io}"
SIDECAR_PORT="${SIDECAR_PORT:-8791}"
MEDIA_DIR="${MEDIA_DIR:-$APP_DIR/media}"
TS="$(date -u +%Y%m%d-%H%M%S)"
BACKUP_DIR="$APP_DIR/backups/reel-$TS"
UNIT=/etc/systemd/system/reel-fetch.service
CRON=/etc/cron.d/reel-fetch-cleanup
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FILES="$HERE/files"
CONNECTOR_DIR="$APP_DIR/src/connector"

MODE=install
WITH_WHISPER=0
SMOKE_URL=""
for arg in "$@"; do
  case "$arg" in
    --check) MODE=check ;;
    --revert) MODE=revert ;;
    --with-whisper) WITH_WHISPER=1 ;;
    --smoke-url=*) SMOKE_URL="${arg#*=}" ;;
    --*) echo "неизвестный флаг: $arg"; exit 2 ;;
    *) SMOKE_URL="$arg" ;;
  esac
done

# Запуск от владельца приложения (не от root): файлы в media/ и lock не должны стать root-овыми.
as_owner() { if command -v runuser >/dev/null 2>&1; then runuser -u "$OWNER" -- "$@"; else sudo -n -u "$OWNER" -- "$@"; fi; }

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '   \033[32mOK\033[0m %s\n' "$*"; }
warn() { printf '   \033[33mWARN\033[0m %s\n' "$*"; }
die()  { printf '   \033[31mFAIL\033[0m %s\n' "$*" >&2; exit 1; }

have_systemd() { command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; }
svc() { have_systemd && systemctl "$@" || true; }
svc_active() { have_systemd && systemctl is-active --quiet "$1"; }

[ "$(id -u)" -eq 0 ] || die "нужен root (sudo)"

# ------------------------------------------------------------------ откат
if [ "$MODE" = revert ]; then
  say "Откат reel_fetch"
  svc disable --now reel-fetch.service
  rm -f "$UNIT" "$CRON"
  svc daemon-reload
  newest_backup() { ls -1t $1 2>/dev/null | head -1 || true; }
  for target in "$CONNECTOR_DIR/addons_bridge.py" "$CADDYFILE"; do
    bak="$(newest_backup "${target}.bak-reel-*")"
    if [ -n "$bak" ]; then cp -a "$bak" "$target"; ok "восстановлен $(basename "$target") из $(basename "$bak")"; else warn "бэкап для $target не найден"; fi
  done
  rm -f "$CONNECTOR_DIR/reel_fetch.py" "$CONNECTOR_DIR/reel_sidecar.py"
  ok "файлы модуля удалены"
  if command -v caddy >/dev/null 2>&1; then
    caddy validate --adapter caddyfile --config "$CADDYFILE" >/dev/null 2>&1 && svc reload caddy && ok "caddy перезагружен"
  fi
  svc restart social-connector-bridge social-connector-mcp-http
  ok "сервисы перезапущены"
  echo; echo "Медиа в $MEDIA_DIR не удалялись. Удалить вручную, если не нужно."
  exit 0
fi

# ------------------------------------------------------------------ preflight
say "1/8 Preflight"
[ -d "$APP_DIR" ] || die "нет каталога $APP_DIR"
VENV_PY="$APP_DIR/.venv/bin/python"
[ -x "$VENV_PY" ] || VENV_PY="$(command -v python3)"
ok "python: $($VENV_PY -V 2>&1) ($VENV_PY)"
if command -v ffmpeg >/dev/null; then ok "ffmpeg: $(ffmpeg -version | head -1 | cut -c1-45)"; else warn "ffmpeg не найден — аудио и транскрипт не будут работать (apt install ffmpeg)"; fi
if [ -f "$CADDYFILE" ]; then ok "Caddyfile: $CADDYFILE"; else warn "нет $CADDYFILE — публичные URL не появятся"; fi
if svc_active social-connector-bridge; then ok "social-connector-bridge active"; else warn "bridge не active (проверьте перед установкой)"; fi
OWNER="$(awk -F= '/^User=/{print $2; exit}' /etc/systemd/system/social-connector-bridge.service 2>/dev/null || true)"
OWNER="${OWNER:-auggievpn}"
ok "владелец файлов: $OWNER"
YTDL="$($VENV_PY -c 'import yt_dlp;print(yt_dlp.version.__version__)' 2>/dev/null || echo none)"
ok "yt-dlp в venv: $YTDL"
FREE="$(df -Pm "$APP_DIR" | awk 'NR==2{print $4}')"
ok "свободно на $APP_DIR: ${FREE} МБ"
[ "${FREE:-0}" -lt 300 ] && warn "мало места (<300 МБ): ролики и модель ASR могут не влезть"

if [ "$MODE" = check ]; then
  say "Только осмотр (--check). Изменений не вносилось."
  for f in "$CONNECTOR_DIR/addons_bridge.py" "$CADDYFILE"; do
    [ -f "$f" ] && echo "   якоря в $f:" && grep -n -m4 -E 'media_transcribe|monitor|osint_domain|reverse_proxy' "$f" | sed 's/^/     /'
  done
  exit 0
fi

mkdir -p "$BACKUP_DIR" "$MEDIA_DIR"
chown -R "$OWNER:$OWNER" "$MEDIA_DIR" 2>/dev/null || true   # в т.ч. lock/файлы от прежних запусков от root
chmod 750 "$MEDIA_DIR"
ok "каталог медиа: $MEDIA_DIR"

# ------------------------------------------------------------------ зависимости
say "2/8 Зависимости в venv"
VENV_DIR="$(dirname "$(dirname "$VENV_PY")")"
VENV_OWNER="$(stat -c %U "$VENV_DIR" 2>/dev/null || echo root)"
"$VENV_PY" -m pip install -q --upgrade yt-dlp && ok "yt-dlp: $($VENV_PY -c 'import yt_dlp;print(yt_dlp.version.__version__)')" || warn "yt-dlp не обновился"
if [ "$WITH_WHISPER" = 1 ]; then
  if "$VENV_PY" -m pip install -q faster-whisper; then ok "faster-whisper установлен (модель скачается при первом транскрипте)"; else warn "faster-whisper не установился"; fi
fi
# pip работал от root: вернуть владельца venv, если он был не root (ловушка №5 из ТЗ)
if [ "$VENV_OWNER" != root ] && [ -d "$VENV_DIR/bin" ]; then
  chown -R "$VENV_OWNER:$VENV_OWNER" "$VENV_DIR" && ok "владелец venv восстановлен: $VENV_OWNER"
fi

# ------------------------------------------------------------------ файлы модуля
say "3/8 Модуль reel_fetch"
for f in reel_fetch.py reel_sidecar.py; do
  cp -a "$FILES/$f" "$CONNECTOR_DIR/$f"
  chown "$OWNER:$OWNER" "$CONNECTOR_DIR/$f" 2>/dev/null || true
  chmod 644 "$CONNECTOR_DIR/$f"
  ok "$f -> src/connector/"
done
"$VENV_PY" -m py_compile "$CONNECTOR_DIR/reel_fetch.py" "$CONNECTOR_DIR/reel_sidecar.py" || die "модуль не компилируется — дальше не идём"
ok "py_compile пройден"
as_owner env REEL_MEDIA_DIR="$MEDIA_DIR" "$VENV_PY" -c "import sys;sys.path.insert(0,'$CONNECTOR_DIR');import reel_fetch;h=reel_fetch.health();print('   health ok=%s yt_dlp=%s ffmpeg=%s media=%s asr=%s' % (h['ok'],h['yt_dlp'],bool(h['ffmpeg']),h['media_writable'],h['asr']))" \
  || die "reel_fetch не импортируется в venv"
ok "импорт и health() работают"

# ------------------------------------------------------------------ реестр аддонов
say "4/8 Регистрация в диспетчере аддонов"
BRIDGE="$CONNECTOR_DIR/addons_bridge.py"
if [ -f "$BRIDGE" ]; then
  cp -a "$BRIDGE" "$BACKUP_DIR/addons_bridge.py"
  set +e
  "$VENV_PY" "$FILES/patch_addons.py" --file "$BRIDGE"
  rc=$?
  set -e
  case "$rc" in
    0) ok "addons_call(module=reel_fetch) доступен" ;;
    3) warn "реестр аддонов устроен иначе — остаётся REST-вход /reel (работает независимо)" ;;
    *) warn "регистрация не удалась (код $rc), файл не изменён" ;;
  esac
else
  warn "нет $BRIDGE — только REST-вход /reel"
fi

# ------------------------------------------------------------------ systemd
say "5/8 Сервис reel-fetch"
RESOLV_BIND=""
if [ -f /etc/social_connector-resolv.conf ]; then
  RESOLV_BIND="BindReadOnlyPaths=/etc/social_connector-resolv.conf:/etc/resolv.conf"
  ok "чистый DNS: /etc/social_connector-resolv.conf (как у bridge)"
else
  warn "нет /etc/social_connector-resolv.conf — сервис будет на системном DNS (провайдер подменяет instagram.com)"
fi
cat > "$UNIT" <<EOF
[Unit]
Description=reel_fetch sidecar (social_connector)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$OWNER
Group=$OWNER
WorkingDirectory=$APP_DIR
EnvironmentFile=-$APP_DIR/.env
Environment=REEL_MEDIA_DIR=$MEDIA_DIR
Environment=REEL_PUBLIC_BASE=https://$DOMAIN
Environment=PYTHONUNBUFFERED=1
$RESOLV_BIND
ExecStart=$VENV_PY $CONNECTOR_DIR/reel_sidecar.py --host 127.0.0.1 --port $SIDECAR_PORT
Restart=always
RestartSec=3
NoNewPrivileges=yes
PrivateTmp=yes
ReadWritePaths=$MEDIA_DIR

[Install]
WantedBy=multi-user.target
EOF
svc daemon-reload
svc enable --now reel-fetch.service
sleep 2
if svc_active reel-fetch.service; then ok "reel-fetch.service active (127.0.0.1:$SIDECAR_PORT)"; else warn "reel-fetch не active: journalctl -u reel-fetch -n 30"; fi

cat > "$CRON" <<EOF
# reel_fetch: чистка скачанного старше 7 дней, ежедневно 03:40
40 3 * * * $OWNER find $MEDIA_DIR -maxdepth 1 -type f -mtime +7 -delete
EOF
chmod 644 "$CRON"
ok "cron-чистка: $CRON"

# ------------------------------------------------------------------ caddy
say "6/8 Caddy: /media/* и /reel/*"
if [ -f "$CADDYFILE" ]; then
  cp -a "$CADDYFILE" "$BACKUP_DIR/Caddyfile"
  set +e
  "$VENV_PY" "$FILES/patch_caddy.py" --file "$CADDYFILE" --domain "$DOMAIN" --media-dir "$MEDIA_DIR" --port "$SIDECAR_PORT"
  rc=$?
  set -e
  if [ "$rc" -eq 0 ]; then
    if command -v caddy >/dev/null 2>&1; then
      if caddy validate --adapter caddyfile --config "$CADDYFILE" >/tmp/caddy_validate.$$ 2>&1; then
        svc reload caddy && ok "caddy перезагружен, конфиг валиден"
      else
        warn "caddy validate не прошёл — откатываю Caddyfile"
        tail -5 /tmp/caddy_validate.$$ | sed 's/^/     /'
        cp -a "$BACKUP_DIR/Caddyfile" "$CADDYFILE"
        svc reload caddy
      fi
      rm -f /tmp/caddy_validate.$$
    else
      warn "caddy не установлен в этой среде — правка записана, валидация вручную"
    fi
  else
    warn "маршруты не добавлены (код $rc)"
  fi
else
  warn "Caddyfile отсутствует — публичные URL работать не будут"
fi

# ------------------------------------------------------------------ рестарт
say "7/8 Перезапуск коннектора"
svc restart social-connector-bridge social-connector-mcp-http
sleep 2
for u in social-connector-bridge social-connector-mcp-http reel-fetch; do
  if svc_active "$u"; then ok "$u active"; else warn "$u НЕ active"; fi
done

# ------------------------------------------------------------------ проверка
say "8/8 Проверка"
KEY="$(cat /home/routeradmin/.bridge_api_key 2>/dev/null || true)"
if [ -z "$KEY" ] && [ -f "$APP_DIR/.env" ]; then
  KEY="$(grep -m1 '^BRIDGE_API_KEY=' "$APP_DIR/.env" | cut -d= -f2- | tr -d '"'"'"' ' || true)"
fi
if curl -fsS -m 5 "http://127.0.0.1:$SIDECAR_PORT/health" >/tmp/reel_health.$$ 2>/dev/null; then
  head -c 400 /tmp/reel_health.$$; echo; ok "сайдкар отвечает локально"
else
  warn "сайдкар не отвечает: journalctl -u reel-fetch -n 30"
fi
rm -f /tmp/reel_health.$$

if [ -n "$KEY" ]; then
  code="$(curl -s -o /dev/null -w '%{http_code}' -m 10 -H "X-API-Key: $KEY" "http://127.0.0.1:8787/addons/reel_fetch/health" || true)"
  [ "$code" = "200" ] && ok "addons-роут: 200" || warn "addons-роут: HTTP $code (нормально, если реестр не патчился — есть /reel)"
  code="$(curl -s -o /dev/null -w '%{http_code}' -m 10 -H "X-API-Key: $KEY" "http://127.0.0.1:$SIDECAR_PORT/reel?action=list&limit=3" || true)"
  [ "$code" = "200" ] && ok "REST /reel: 200" || warn "REST /reel: HTTP $code"
  code="$(curl -s -o /dev/null -w '%{http_code}' -m 10 "http://127.0.0.1:$SIDECAR_PORT/reel?action=list" || true)"
  [ "$code" = "401" ] && ok "без ключа: 401 (auth работает)" || warn "без ключа: HTTP $code"
else
  warn "API-ключ не найден — проверки через bridge пропущены"
fi

if [ -n "$SMOKE_URL" ]; then
  say "Живая проверка на ролике (через сервис: uid $OWNER, его DNS и egress)"
  if [ -z "$KEY" ]; then
    warn "нет API-ключа — смоук через сайдкар невозможен"
  else
    ENC_URL="$("$VENV_PY" -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1],safe=""))' "$SMOKE_URL")"
    out="$(curl -s -m 600 -H "X-API-Key: $KEY" "http://127.0.0.1:$SIDECAR_PORT/reel?action=fetch&transcribe=1&url=$ENC_URL" || true)"
    [ -n "$out" ] || out='{"ok": false, "error": "сайдкар не ответил"}'
    echo "$out" | head -c 1500; echo
    FURL="$(printf '%s' "$out" | "$VENV_PY" -c 'import json,sys
try:
    d = json.load(sys.stdin)
    print(d.get("file_url", "") if d.get("ok") else "")
except Exception:
    print("")')"
    if [ -n "$FURL" ]; then
      NAME="${FURL##*/}"
      code="$(curl -s -o /dev/null -w '%{http_code}' -m 15 -I "http://127.0.0.1:$SIDECAR_PORT/media/$NAME" || true)"
      [ "$code" = "200" ] && ok "сайдкар отдаёт файл: /media/$NAME" || warn "сайдкар /media: HTTP $code"
      code="$(curl -s -o /dev/null -w '%{http_code}' -m 15 -I "$FURL" || true)"
      [ "$code" = "200" ] && ok "публичный файл доступен: $FURL" || warn "публичный URL с роутера: HTTP $code ($FURL) — проверьте снаружи, hairpin через Xiaomi может не работать"
    else
      warn "скачивание не удалось — см. вывод выше (429 → подождать 10–15 мин, повторить один раз)"
    fi
  fi
fi

# Страховка: всё в media/ принадлежит владельцу сервиса (в т.ч. lock от прежних запусков от root)
chown -R "$OWNER:$OWNER" "$MEDIA_DIR" 2>/dev/null || true

echo
echo "Как пользоваться:"
echo "  REST : curl -H \"X-API-Key: \$KEY\" 'https://$DOMAIN/reel?action=fetch&url=<REEL_URL>&transcribe=1'"
echo "  MCP  : addons_call(module=\"reel_fetch\", action=\"fetch\", params={\"url\":\"<REEL_URL>\",\"transcribe\":true})"
echo "  Файлы: https://$DOMAIN/media/<имя>.mp4   (отдаёт сайдкар, без листинга каталога)"
echo "Откат:  sudo bash $HERE/deploy.sh --revert"
echo "Бэкапы этого запуска: $BACKUP_DIR"
