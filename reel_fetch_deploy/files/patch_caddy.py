#!/usr/bin/env python3
"""Добавляет в site-блок Caddyfile маршруты reel_fetch:

    handle /media/* { reverse_proxy 127.0.0.1:<port> }   # файлы отдаёт сайдкар
    handle /reel*   { reverse_proxy 127.0.0.1:<port> }   # /reel, /reel/, /reel?action=...

Почему так (v2, 20261101b):
  * /media/* идёт через сайдкар, а не file_server: сайдкар работает под
    владельцем каталога (750 auggievpn), Caddy доступ к каталогу не нужен,
    листинга файлов наружу нет;
  * /reel* вместо /reel/*: иначе запрос /reel?action=... не попадает в маршрут;
  * блок вставляется сразу после открывающей строки site-блока, на верхнем
    уровне (глубина 1), а не перед первым reverse_proxy — тот мог лежать
    внутри чужого handle {...}, и маршруты оказались бы вложенными.

Идемпотентно: блок обрамлён маркерами; старый блок (v1 или v2) заменяется.
С бэкапом. Валидация — снаружи (caddy validate в deploy.sh, откат при провале).

Коды выхода: 0 — готово/уже есть, 3 — site-блок не найден, 4 — скобки не сошлись.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import time
from pathlib import Path

BEGIN = "# --- reel_fetch: begin ---"
END = "# --- reel_fetch: end ---"
# v1 (исходная поставка) писала блок без конечного маркера.
V1_HEAD = re.compile(r"^\s*# --- reel_fetch: раздача скачанных файлов и REST-вход.*$")


def block_bounds(lines: list[str], domain: str) -> tuple[int, int] | None:
    """Индексы (открывающая строка, закрывающая строка) site-блока домена."""
    for idx, line in enumerate(lines):
        stripped = line.split("#", 1)[0].strip()
        if not stripped.endswith("{"):
            continue
        addresses = [a.strip().rstrip(",") for a in stripped[:-1].replace(",", " ").split()]
        hosts = {re.sub(r"^https?://", "", a).split(":")[0].split("/")[0] for a in addresses}
        if domain not in hosts:
            continue
        depth = 0
        for j in range(idx, len(lines)):
            code = lines[j].split("#", 1)[0]
            depth += code.count("{") - code.count("}")
            if depth == 0:
                return idx, j
        return None
    return None


def strip_old(lines: list[str]) -> tuple[list[str], bool]:
    """Убирает ранее вставленный блок reel_fetch (v2 по маркерам, v1 по шапке)."""
    out: list[str] = []
    i, removed = 0, False
    while i < len(lines):
        line = lines[i]
        if line.strip() == BEGIN:
            j = i
            while j < len(lines) and lines[j].strip() != END:
                j += 1
            i, removed = j + 1, True
            continue
        if V1_HEAD.match(line):
            # v1: шапка + handle_path /media/* {...} + handle /reel/* {...}
            j, blocks = i + 1, 0
            while j < len(lines) and blocks < 2:
                if lines[j].strip().startswith(("handle_path /media/*", "handle /reel/*")):
                    depth = 0
                    while j < len(lines):
                        depth += lines[j].count("{") - lines[j].count("}")
                        j += 1
                        if depth == 0:
                            break
                    blocks += 1
                else:
                    break
            i, removed = j, True
            continue
        out.append(line)
        i += 1
    return out, removed


def build_block(indent: str, port: int) -> str:
    inner = indent + "\t"
    return (
        f"{indent}{BEGIN}\n"
        f"{indent}# раздача скачанных файлов и REST-вход reel_fetch (deploy 20261101b)\n"
        f"{indent}handle /media/* {{\n"
        f"{inner}reverse_proxy 127.0.0.1:{port}\n"
        f"{indent}}}\n"
        f"{indent}handle /reel* {{\n"
        f"{inner}reverse_proxy 127.0.0.1:{port}\n"
        f"{indent}}}\n"
        f"{indent}{END}\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default="/etc/caddy/Caddyfile")
    parser.add_argument("--domain", required=True)
    parser.add_argument("--media-dir", default="/opt/social_connector/media", help="(не используется с v2, оставлен для совместимости)")
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    path = Path(args.file)
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"

    lines, removed = strip_old(lines)
    bounds = block_bounds(lines, args.domain)
    if not bounds:
        print(f"FAIL: не найден site-блок для {args.domain} в {path}")
        return 3
    start, end = bounds

    # отступ — как у первой непустой строки внутри блока
    indent = "\t"
    for idx in range(start + 1, end):
        if lines[idx].strip():
            indent = lines[idx][: len(lines[idx]) - len(lines[idx].lstrip())] or "\t"
            break

    block = build_block(indent, args.port)
    new_lines = lines[: start + 1] + [block] + lines[start + 1:]
    patched = "".join(new_lines)

    if patched == text:
        print("OK: маршруты reel_fetch уже на месте (правка не требуется)")
        return 0

    def balance(s: str) -> int:
        code = "\n".join(line.split("#", 1)[0] for line in s.splitlines())
        return code.count("{") - code.count("}")

    if balance(patched) != balance(text):
        print(f"FAIL: после правки разбалансированы скобки ({balance(patched)}), файл не изменён")
        return 4

    action = "заменён" if removed else "добавлен"
    print(f"ANCHOR: site-блок {args.domain} (строка {start + 1}), блок reel_fetch {action} на верхнем уровне")
    if args.dry_run:
        print(block)
        print("DRY-RUN: изменения не записаны")
        return 0

    backup = path.with_name(path.name + f".bak-reel-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)
    print(f"BACKUP: {backup}")
    path.write_text(patched, encoding="utf-8")
    print("OK: Caddyfile обновлён (валидация — отдельным шагом)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
