#!/usr/bin/env bash
# Приёмка reel_fetch на роутере (раздел 5 ТЗ). Запуск: sudo -n bash accept_remote.sh <EVIDENCE_DIR> [REEL_URL]
# Пишет results.tsv (№, проверка, статус, факт) и сырые доказательства. Ключ нигде не печатается.
set -u
EV="${1:?нужен каталог для доказательств}"
REEL_URL="${2:-https://www.instagram.com/reel/Dc_yQ6LoA1f/}"
APP_DIR="${APP_DIR:-/opt/social_connector}"
TOOLS="${TOOLS:-$APP_DIR/tools/reel_fetch_20261101}"
CADDYFILE="${CADDYFILE:-/etc/caddy/Caddyfile}"
KEYFILE="${KEYFILE:-/home/routeradmin/.bridge_api_key}"
AUTO_VOSK="${AUTO_VOSK:-1}"
PY="$APP_DIR/.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
SC=http://127.0.0.1:8791

mkdir -p "$EV"; cd "$EV" || exit 1
export K="$(cat "$KEYFILE" 2>/dev/null || true)"
# маскирование ключа в любом выводе
mask() { "$PY" -c 'import os,sys
k=os.environ.get("K","");d=sys.stdin.read()
sys.stdout.write(d.replace(k,"***KEY***") if k else d)'; }
# одна строка результата: № | проверка | OK/FAIL/WARN | факт (до 300 символов, без переводов строк)
res() { printf '%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$(printf '%s' "$4" | tr '\n\t' '  ' | cut -c1-300 | mask)" >> results.tsv; }
code() { curl -s -o /dev/null -w '%{http_code}' -m "${T:-15}" "$@" || true; }
jget() { "$PY" -c 'import json,sys
try: d=json.load(open(sys.argv[1]))
except Exception: d={}
for p in sys.argv[2].split("."): d=d.get(p) if isinstance(d,dict) else None
print("" if d is None else d)' "$1" "$2"; }
: > results.tsv

[ -n "$K" ] || res 0 "API-ключ" FAIL "не прочитан $KEYFILE — проверки с ключом не пройдут"

# 1 deploy --check
bash "$TOOLS/deploy.sh" --check > check.txt 2>&1
if grep -q FAIL check.txt; then s=FAIL; elif grep -q media_transcribe check.txt; then s=OK; else s=WARN; fi
res 1 "deploy.sh --check" "$s" "$(sed 's/\x1b\[[0-9;]*m//g' check.txt | grep -E 'FAIL|WARN|media_transcribe' | head -4)"

# 2 сервис
a="$(systemctl is-active reel-fetch 2>&1)"; res 2 "systemctl is-active reel-fetch" "$([ "$a" = active ] && echo OK || echo FAIL)" "$a"

# 3 health
curl -s -m 10 "$SC/health" > health.json
okh="$(jget health.json reel.ok)"; ff="$(jget health.json reel.ffmpeg)"
res 3 "/health сайдкара" "$([ "$okh" = True ] && [ -n "$ff" ] && echo OK || echo FAIL)" "ok=$okh ffmpeg=$ff yt_dlp=$(jget health.json reel.yt_dlp) asr=$(jget health.json reel.asr)"

# 4 /reel с ключом / без
c1="$(code -H "X-API-Key: $K" "$SC/reel?action=list")"; c2="$(code "$SC/reel?action=list")"
res 4 "/reel с ключом / без" "$([ "$c1" = 200 ] && [ "$c2" = 401 ] && echo OK || echo FAIL)" "$c1 / $c2"

# 5 MCP-путь
c="$(code -H "X-API-Key: $K" "http://127.0.0.1:8787/addons/reel_fetch/health")"
res 5 "/addons/reel_fetch/health" "$([ "$c" = 200 ] && echo OK || echo FAIL)" "$c"

# 6 живое скачивание (через сервис), при 429 — одна повторная попытка через 15 минут
ENC="$("$PY" -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1],safe=""))' "$REEL_URL")"
fetch() { curl -s -m 900 -H "X-API-Key: $K" "$SC/reel?action=fetch&transcribe=1&url=$ENC" | mask > fetch.json; }
fetch
if [ "$(jget fetch.json ok)" != True ] && grep -q '429' fetch.json; then
  echo "429 от Instagram — жду 15 минут и повторяю один раз" >&2; cp fetch.json fetch_429.json; sleep 900; fetch
fi
FILE="$(jget fetch.json file)"; FURL="$(jget fetch.json file_url)"; FB="$(jget fetch.json file_bytes)"
res 6 "action=fetch по Reels" "$([ "$(jget fetch.json ok)" = True ] && [ "${FB:-0}" -gt 0 ] 2>/dev/null && [ -n "$FURL" ] && echo OK || echo FAIL)" \
  "ok=$(jget fetch.json ok) file_bytes=$FB file_url=$FURL error=$(jget fetch.json error)"

# 8 транскрипт; при пустом — vosk (шаг 6 ТЗ) и повторная расшифровка уже скачанного файла
TR="$(jget fetch.json transcript)"
if [ "${#TR}" -lt 100 ] && [ -n "$FILE" ] && [ "$AUTO_VOSK" = 1 ] && [ ! -d "$APP_DIR/models/vosk-ru" ]; then
  echo "транскрипт пустой/короткий — ставлю vosk-model-small-ru (шаг 6 ТЗ)" >&2
  {
    VOWNER="$(stat -c %U "$APP_DIR/.venv")"
    "$APP_DIR/.venv/bin/pip" install -q vosk && chown -R "$VOWNER:$VOWNER" "$APP_DIR/.venv"
    mkdir -p "$APP_DIR/models" && cd /tmp &&
    curl -sSL -o vru.zip https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip &&
    unzip -q -o vru.zip -d "$APP_DIR/models" && rm -rf "$APP_DIR/models/vosk-ru" &&
    mv "$APP_DIR/models/vosk-model-small-ru-0.22" "$APP_DIR/models/vosk-ru" && rm -f vru.zip &&
    chown -R auggievpn:auggievpn "$APP_DIR/models"
    mkdir -p /etc/systemd/system/reel-fetch.service.d
    printf '[Service]\nEnvironment=REEL_VOSK_MODEL=%s/models/vosk-ru\n' "$APP_DIR" > /etc/systemd/system/reel-fetch.service.d/10-asr.conf
    systemctl daemon-reload && systemctl restart reel-fetch && sleep 3
    cd "$EV"
  } > vosk_install.txt 2>&1
  EF="$("$PY" -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1],safe=""))' "$FILE")"
  curl -s -m 900 -H "X-API-Key: $K" "$SC/reel?action=transcribe&file=$EF" | mask > transcribe.json
  TR="$(jget transcribe.json transcript)"
  [ -n "$TR" ] || TR="$(jget transcribe.json text)"
fi
printf '%s\n' "$TR" > transcript.txt
res 8 "transcript ≥ 100 символов" "$([ "${#TR}" -ge 100 ] && echo OK || echo FAIL)" "длина=${#TR} engine=$(jget fetch.json transcript_engine)$( [ -f transcribe.json ] && echo "→$(jget transcribe.json engine)") err=$(jget fetch.json transcript_error) | ${TR:0:150}"

# 7 публичный файл (с роутера; основная проверка — с ноутбука)
if [ -n "$FURL" ]; then
  curl -sI -m 20 "$FURL" > public_head_router.txt 2>&1
  curl -sI -m 10 "$SC/media/${FURL##*/}" > sidecar_head.txt 2>&1
  h="$(head -1 public_head_router.txt | tr -d '\r')"; ct="$(grep -i '^content-type' public_head_router.txt | tr -d '\r')"
  res 7 "curl -I публичного file_url (с роутера)" "$(echo "$h" | grep -q ' 200' && echo "$ct" | grep -qi video/mp4 && echo OK || echo WARN)" "$h; $ct; сайдкар: $(head -1 sidecar_head.txt | tr -d '\r')"
  [ -f "$FILE" ] && command -v ffprobe >/dev/null && ffprobe -v error -show_entries format=duration,size:stream=codec_name,width,height -of compact "$FILE" > ffprobe.txt 2>&1
else
  res 7 "curl -I публичного file_url" FAIL "file_url пуст — скачивание не удалось"
fi

# 9 caddy validate
caddy validate --adapter caddyfile --config "$CADDYFILE" > caddy_validate.txt 2>&1
res 9 "caddy validate" "$(grep -q 'Valid configuration' caddy_validate.txt && echo OK || echo FAIL)" "$(tail -1 caddy_validate.txt)"
grep -n -A8 'reel_fetch: begin' "$CADDYFILE" > caddy_block.txt 2>&1

# 10 журналы
for u in reel-fetch social-connector-bridge social-connector-mcp-http; do journalctl -u "$u" -n 50 --no-pager 2>&1 | mask > "journal_$u.txt"; done
bad="$(grep -lE 'Traceback| 500 |HTTP/1\.[01]" 500' journal_*.txt 2>/dev/null | tr '\n' ' ')"
res 10 "журналы без Traceback/500" "$([ -z "$bad" ] && echo OK || echo FAIL)" "${bad:-чисто}"

# 11 права
st="$(stat -c '%U:%G %a %n' "$APP_DIR/src/connector/addons_bridge.py" "$APP_DIR/src/connector/reel_fetch.py" "$APP_DIR/src/connector/reel_sidecar.py" "$APP_DIR/media" 2>&1)"
echo "$st" > perms.txt
res 11 "права auggievpn:auggievpn" "$(echo "$st" | grep -vq '^auggievpn:auggievpn' && echo FAIL || echo OK)" "$st"

# 12 bridge/MCP
s12=OK; f12=""
for pair in social-connector-bridge:8787 social-connector-mcp-http:8788; do
  u="${pair%%:*}"; p="${pair##*:}"
  a="$(systemctl is-active "$u")"; y="$(code -H "X-API-Key: $K" "http://127.0.0.1:$p/health")"; n="$(code "http://127.0.0.1:$p/health")"
  f12="$f12 $u=$a key=$y nokey=$n;"
  [ "$a" = active ] && [ "$y" = 200 ] && [ "$n" = 401 ] || s12=FAIL
done
res 12 "bridge/MCP после работ" "$s12" "$f12"

# статусы юнитов, список бэкапов
for u in reel-fetch social-connector-bridge social-connector-mcp-http caddy; do systemctl status "$u" --no-pager -n 0 2>&1 | head -5; echo; done | mask > systemctl_status.txt
{ ls -la "$APP_DIR"/src/connector/*.bak-reel-* "$CADDYFILE".bak-reel-* 2>&1; ls -1d "$APP_DIR"/backups/reel-* 2>&1; } > backups.txt
printf '%s\n' "$FURL" > file_url.txt

chown -R routeradmin:routeradmin "$EV" 2>/dev/null || true
cat results.tsv
