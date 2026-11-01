#!/usr/bin/env python3
"""Добавляет в нужный site-блок Caddyfile два маршрута:

    handle_path /media/* { root * <media_dir>; file_server }
    handle /reel/*       { reverse_proxy 127.0.0.1:<port> }

Правка идемпотентна, делается внутри блока домена (счётчик скобок),
с бэкапом. Проверка валидности Caddyfile — снаружи, в deploy.sh
(caddy validate --config ...), при провале файл откатывается.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

MARKER = "/media/*"


def block_bounds(lines: list[str], domain: str) -> tuple[int, int] | None:
    """Индексы (открывающая строка, закрывающая строка) блока домена."""
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(domain) and "{" in stripped:
            depth = 0
            for j in range(idx, len(lines)):
                depth += lines[j].count("{") - lines[j].count("}")
                if depth == 0 and j > idx:
                    return idx, j
            return idx, len(lines) - 1
    return None


def build_block(domain: str, media_dir: str, port: int) -> str:
    return (
        f"\t# --- reel_fetch: раздача скачанных файлов и REST-вход (добавлено {time.strftime('%Y-%m-%d')}) ---\n"
        f"\thandle_path /media/* {{\n"
        f"\t\troot * {media_dir}\n"
        f"\t\tfile_server browse\n"
        f"\t\theader Cache-Control \"private, max-age=3600\"\n"
        f"\t}}\n"
        f"\thandle /reel/* {{\n"
        f"\t\treverse_proxy 127.0.0.1:{port}\n"
        f"\t}}\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default="/etc/caddy/Caddyfile")
    parser.add_argument("--domain", required=True)
    parser.add_argument("--media-dir", default="/opt/social_connector/media")
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    path = Path(args.file)
    text = path.read_text(encoding="utf-8")
    if MARKER in text and "/reel/*" in text:
        print("OK: маршруты /media/* и /reel/* уже есть в Caddyfile")
        return 0

    lines = text.splitlines(keepends=True)
    bounds = block_bounds(lines, args.domain)
    if not bounds:
        print(f"FAIL: не найден site-блок для {args.domain} в {path}")
        return 3
    start, end = bounds

    insert_at = end
    for idx in range(start, end):
        if "reverse_proxy" in lines[idx]:
            insert_at = idx
            break

    block = build_block(args.domain, args.media_dir, args.port)
    if insert_at == end and end > 0 and not lines[end - 1].endswith("\n"):
        lines[end - 1] += "\n"

    new_lines = lines[:insert_at] + [block] + lines[insert_at:]
    patched = "".join(new_lines)

    added_open = patched.count("{") - text.count("{")
    added_close = patched.count("}") - text.count("}")
    if added_open != added_close or added_open <= 0:
        print(f"FAIL: после правки разбалансированы скобки (+{added_open}/-{added_close}), файл не изменён")
        return 4

    print(f"ANCHOR: site-блок {args.domain} (строки {start + 1}..{end + 1}), вставка перед строкой {insert_at + 1}")
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
