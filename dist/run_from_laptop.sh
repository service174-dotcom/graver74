#!/usr/bin/env bash
# Полное выполнение ТЗ reel_fetch с ноутбука (Windows + Git Bash).
#
#   cd /c/Users/User/reel_deploy      # здесь лежат: этот скрипт, accept_remote.sh, reel_fetch_deploy_20261101b.tar.gz
#   bash run_from_laptop.sh           # всё: копирование, --check, установка, права, vosk при необходимости, приёмка, отчёт
#   bash run_from_laptop.sh --revert  # откат
#
# Переменные (необязательно): REEL_URL, WITH_WHISPER=1, AUTO_VOSK=0.
set -uo pipefail
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

HOST=routeradmin@192.168.1.100
KEY_SSH="$HOME/.ssh/hackerai_router"
SSH=(ssh -i "$KEY_SSH" -o BatchMode=yes -o ConnectTimeout=10 "$HOST")
SCP=(scp -q -i "$KEY_SSH" -o BatchMode=yes -o ConnectTimeout=10)
TAR=reel_fetch_deploy_20261101b.tar.gz
SHA=78aeed244db8ad3fb1fd991d929c47e1bcb1a60d8d7fd7fb532a98426424971b
RDIR=/opt/social_connector/tools/reel_fetch_20261101
REEL_URL="${REEL_URL:-https://www.instagram.com/reel/Dc_yQ6LoA1f/}"
UTC="$(date -u +%Y%m%dT%H%M%SZ)"
HERE="$(cd "$(dirname "$0")" && pwd)"
EV="$HERE/evidence-$UTC"
REPORT="$HERE/REPORT-$UTC.md"
mkdir -p "$EV"
LOG="$EV/run.log"
step() { printf '\n\033[1m== %s\033[0m\n' "$*" | tee -a "$LOG"; }
die()  { printf '\033[31mСТОП:\033[0m %s\n' "$*" | tee -a "$LOG"; exit 1; }
rsh()  { "${SSH[@]}" "$@" 2>&1 | sed 's/\x1b\[[0-9;]*m//g' | tee -a "$LOG"; return "${PIPESTATUS[0]}"; }

cd "$HERE" || exit 1

if [ "${1:-}" = "--revert" ]; then
  step "Откат"; rsh "sudo -n bash $RDIR/deploy.sh --revert"; exit $?
fi

step "0. Проверки на ноутбуке"
[ -f "$TAR" ] || die "нет $TAR в $HERE"
[ -f accept_remote.sh ] || die "нет accept_remote.sh в $HERE"
got="$(sha256sum "$TAR" | cut -d' ' -f1)"
[ "$got" = "$SHA" ] || die "sha256 не совпадает: $got"
echo "sha256 OK: $SHA" | tee -a "$LOG"
rsh "hostname; sudo -n true && echo sudo-ok" || die "нет SSH/sudo до $HOST"

step "0. Копирование на роутер"
"${SCP[@]}" "$TAR" accept_remote.sh "$HOST:/tmp/" || die "scp не прошёл"

step "1. Распаковка в $RDIR"
rsh "sudo -n mkdir -p $RDIR && sudo -n tar -xzf /tmp/$TAR -C $RDIR && sudo -n install -m 755 /tmp/accept_remote.sh $RDIR/accept_remote.sh && sudo -n ls -la $RDIR" || die "распаковка не удалась"

step "2. deploy.sh --check"
rsh "sudo -n bash $RDIR/deploy.sh --check" > "$EV/check.txt"; cat "$EV/check.txt"
grep -q FAIL "$EV/check.txt" && die "--check дал FAIL — см. выше"
if grep -q 'ffmpeg не найден' "$EV/check.txt"; then
  step "2a. Установка ffmpeg (единственный разрешённый пакет)"
  rsh "sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y -q ffmpeg" || die "ffmpeg не установился"
fi

step "3. Свободное место"
rsh "df -h /opt | tail -1; sudo -n du -sh /opt/social_connector 2>/dev/null"
FREE_MB="$("${SSH[@]}" "df -Pm /opt | awk 'NR==2{print \$4}'" 2>/dev/null)"
[ "${FREE_MB:-0}" -ge 500 ] || die "свободно ${FREE_MB:-?} МБ < 500 МБ"
EXTRA=""
if [ "${WITH_WHISPER:-0}" = 1 ]; then
  [ "$FREE_MB" -ge 1536 ] && EXTRA="--with-whisper" || echo "WITH_WHISPER пропущен: свободно $FREE_MB МБ < 1,5 ГБ" | tee -a "$LOG"
fi

step "4. Установка (deploy.sh $EXTRA --smoke-url)"
rsh "sudo -n bash $RDIR/deploy.sh $EXTRA --smoke-url=$REEL_URL" > "$EV/install.txt"; rc=$?; cat "$EV/install.txt"
[ $rc -eq 0 ] || echo "deploy.sh завершился с кодом $rc — продолжаю приёмку, чтобы зафиксировать состояние" | tee -a "$LOG"

step "5. Права"
rsh "sudo -n stat -c '%U:%G %a %n' /opt/social_connector/src/connector/addons_bridge.py /opt/social_connector/src/connector/reel_fetch.py /opt/social_connector/src/connector/reel_sidecar.py /opt/social_connector/media; sudo -n ls -la /opt/social_connector/backups | tail -3" > "$EV/perms_after_install.txt"
cat "$EV/perms_after_install.txt"
if grep -q '^root:root.*addons_bridge.py' "$EV/perms_after_install.txt"; then
  rsh "sudo -n chown auggievpn:auggievpn /opt/social_connector/src/connector/addons_bridge.py" && echo "владелец addons_bridge.py возвращён" | tee -a "$LOG"
fi

step "6–7. Приёмка на роутере (vosk ставится сам, если транскрипт пустой)"
REV="$RDIR/evidence-$UTC"
rsh "sudo -n env AUTO_VOSK=${AUTO_VOSK:-1} bash $RDIR/accept_remote.sh $REV '$REEL_URL'" > "$EV/accept_stdout.txt"
"${SCP[@]}" -r "$HOST:$REV/." "$EV/" || echo "не удалось забрать доказательства с роутера" | tee -a "$LOG"

step "7.5 Публичный файл — снаружи, с ноутбука"
FURL="$(cat "$EV/file_url.txt" 2>/dev/null | tr -d '\r\n')"
if [ -n "$FURL" ]; then
  curl -sI -m 30 "$FURL" > "$EV/public_head_laptop.txt" 2>&1
  h="$(head -1 "$EV/public_head_laptop.txt" | tr -d '\r')"; ct="$(grep -i '^content-type' "$EV/public_head_laptop.txt" | tr -d '\r')"
  s=FAIL; echo "$h" | grep -q ' 200' && echo "$ct" | grep -qi video/mp4 && s=OK
  printf '7\tcurl -I публичного file_url (с ноутбука)\t%s\t%s; %s\n' "$s" "$h" "$ct" >> "$EV/results.tsv"
  curl -s -m 15 -o /dev/null -w 'без ключа /reel снаружи: %{http_code}\n' "${FURL%%/media/*}/reel?action=list" | tee -a "$EV/public_head_laptop.txt"
fi

step "Отчёт"
{
  echo "# Отчёт: развёртывание reel_fetch на vpn-router-01"
  echo
  echo "- Время (UTC): $UTC"
  echo "- Архив: \`$TAR\`, sha256 \`$SHA\` (проверен)"
  echo "- Ролик для проверки: $REEL_URL"
  echo "- Публичный файл: ${FURL:-нет}"
  echo
  echo "## Приёмка (раздел 5 ТЗ)"
  echo
  echo "| № | Проверка | Итог | Факт |"
  echo "|---|---|---|---|"
  sort -t$'\t' -k1,1n -s "$EV/results.tsv" 2>/dev/null | awk -F'\t' '{gsub(/\|/,"\\|",$4); printf "| %s | %s | %s | %s |\n",$1,$2,$3,$4}'
  echo
  echo "## Изменённые файлы и бэкапы"
  echo; echo '```'; cat "$EV/backups.txt" 2>/dev/null; echo '```'
  echo; echo "Новые: \`/etc/systemd/system/reel-fetch.service\`, \`/etc/cron.d/reel-fetch-cleanup\`, \`src/connector/reel_fetch.py\`, \`src/connector/reel_sidecar.py\`, каталог \`media/\`; при vosk — \`models/vosk-ru\`, \`reel-fetch.service.d/10-asr.conf\`."
  echo; echo "Откат: \`ssh -i ~/.ssh/hackerai_router $HOST 'sudo -n bash $RDIR/deploy.sh --revert'\`"
  echo; echo "## systemctl status (кратко)"; echo; echo '```'; cat "$EV/systemctl_status.txt" 2>/dev/null; echo '```'
  echo; echo "## Транскрипт"; echo; echo '```'; head -c 2000 "$EV/transcript.txt" 2>/dev/null; echo; echo '```'
  echo; echo "## ffprobe"; echo; echo '```'; cat "$EV/ffprobe.txt" 2>/dev/null; echo '```'
  echo; echo "## deploy.sh --check"; echo; echo '```'; cat "$EV/check.txt"; echo '```'
  echo; echo "## Лог установки"; echo; echo '```'; cat "$EV/install.txt"; echo '```'
  echo; echo "## Не удалось"; echo
  awk -F'\t' '$3=="FAIL"||$3=="WARN"{n++; printf "- %s. %s: %s — %s\n",$1,$2,$3,$4} END{if(!n) print "- ничего"}' "$EV/results.tsv" 2>/dev/null
  echo; echo "## MCP-клиенты"; echo
  echo "Список инструментов закэширован: коннектор ChatGPT/Claude пересоздать или открыть новый чат, затем"
  echo "\`addons_call(module=\"reel_fetch\", action=\"fetch\", params={\"url\": \"<REEL>\", \"transcribe\": true})\`."
} > "$REPORT"
# ключ в отчёт не попадает (accept_remote.sh маскирует), но проверим
grep -qF -- '***KEY***' "$REPORT" && echo "в отчёте замаскирован ключ — это нормально" | tee -a "$LOG"

"${SCP[@]}" "$REPORT" "$HOST:/tmp/" && rsh "sudo -n install -m 644 /tmp/$(basename "$REPORT") $RDIR/$(basename "$REPORT") && rm -f /tmp/$(basename "$REPORT")"

step "Итог"
sort -t$'\t' -k1,1n -s "$EV/results.tsv" | awk -F'\t' '{printf "%-3s %-45s %s\n",$1,$2,$3}'
echo
echo "Отчёт:          $REPORT"
echo "Доказательства: $EV"
