#!/usr/bin/env python3
"""Регистрация reel_fetch в реестре диспетчера аддонов social_connector.

Стратегия: находим в addons_bridge.py литерал уже существующего модуля
(по умолчанию "media_transcribe") и добавляем рядом "reel_fetch" в том же
списке/словаре. Ничего не переписываем целиком: правка ровно в одном месте,
с проверкой ast.parse и автоматическим откатом при любой ошибке.

Коды выхода:
    0 — зарегистрировано или уже было
    3 — якорь не найден (реестр устроен иначе), файл не изменён
    4 — правка сломала синтаксис, файл восстановлен из бэкапа
"""

from __future__ import annotations

import argparse
import ast
import re
import shutil
import sys
import time
from pathlib import Path

MODULE = "reel_fetch"
DEFAULT_ANCHORS = ("media_transcribe", "monitor", "osint_domain")


def find_anchor(text: str) -> tuple[str, re.Match[str]] | None:
    for anchor in DEFAULT_ANCHORS:
        for quote in ('"', "'"):
            pattern = re.compile(rf"({quote}{re.escape(anchor)}{quote})")
            match = pattern.search(text)
            if match:
                return anchor, match
    return None


def patch(path: Path, dry_run: bool = False) -> int:
    if not path.exists():
        print(f"FAIL: нет файла {path}")
        return 3
    text = path.read_text(encoding="utf-8")
    if re.search(rf"[\"']{MODULE}[\"']", text):
        print(f"OK: {MODULE} уже зарегистрирован в {path.name} (правка не требуется)")
        return 0

    found = find_anchor(text)
    if not found:
        print(f"SKIP: в {path.name} не найден ни один якорь {DEFAULT_ANCHORS} — реестр устроен иначе")
        return 3
    anchor, match = found
    quote = match.group(1)[0]
    insert_at = match.end(1)
    patched = text[:insert_at] + f", {quote}{MODULE}{quote}" + text[insert_at:]

    try:
        ast.parse(patched)
    except SyntaxError as exc:
        print(f"FAIL: после правки синтаксис битый ({exc}); файл не изменён")
        return 4

    line_start = text.rfind("\n", 0, match.start()) + 1
    line_end = text.find("\n", insert_at)
    before = text[line_start:line_end if line_end != -1 else None].strip()
    print(f"ANCHOR: {anchor} в {path.name}")
    print(f"  было:  {before[:160]}")
    after_start = patched.rfind("\n", 0, insert_at) + 1
    after_end = patched.find("\n", insert_at)
    print(f"  стало: {patched[after_start:after_end if after_end != -1 else None].strip()[:160]}")

    if dry_run:
        print("DRY-RUN: изменения не записаны")
        return 0

    backup = path.with_suffix(path.suffix + f".bak-reel-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)
    print(f"BACKUP: {backup}")
    path.write_text(patched, encoding="utf-8")
    try:
        ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        shutil.copy2(backup, path)
        print("ROLLBACK: файл восстановлен из бэкапа")
        return 4
    print(f"OK: {MODULE} добавлен в {path.name}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, help="путь к addons_bridge.py")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return patch(Path(args.file), dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
