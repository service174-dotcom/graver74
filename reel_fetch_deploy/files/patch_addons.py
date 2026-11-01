#!/usr/bin/env python3
"""Регистрация reel_fetch в реестре диспетчера аддонов social_connector.

Стратегия (v2, 20261101b): через ast находим в addons_bridge.py литерал уже
существующего модуля (по порядку "media_transcribe", "monitor", "osint_domain")
и смотрим, где он стоит:

  * элемент списка/кортежа/множества → рядом дописывается "reel_fetch";
  * ключ словаря, запись в одну строку → строка клонируется по образцу
    соседней записи (имя якоря в ней заменяется на reel_fetch);
  * любое другое место (аргумент функции, сравнение, лог) — пропускается,
    чтобы не вписать модуль в случайную строку.

Правка ровно в одном месте, проверка ast.parse до и после записи, бэкап,
автоматический откат.

Коды выхода:
    0 — зарегистрировано или уже было
    3 — подходящий якорь не найден (реестр устроен иначе), файл не изменён
    4 — правка сломала синтаксис, файл не изменён / восстановлен из бэкапа
"""

from __future__ import annotations

import argparse
import os
import ast
import re
import shutil
import time
from pathlib import Path

MODULE = "reel_fetch"
DEFAULT_ANCHORS = ("media_transcribe", "monitor", "osint_domain")


def _offset(lines: list[str], lineno: int, col_bytes: int) -> int:
    """Позиция в тексте по (lineno, col_offset в байтах UTF-8) из ast."""
    head = sum(len(line) for line in lines[: lineno - 1])
    return head + len(lines[lineno - 1].encode("utf-8")[:col_bytes].decode("utf-8"))


def plan_patch(text: str) -> tuple[str, str, str] | None:
    """Возвращает (patched_text, anchor, описание) или None."""
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node

    for anchor in DEFAULT_ANCHORS:
        nodes = sorted((n for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value == anchor),
                       key=lambda n: (n.lineno, n.col_offset))
        for node in nodes:
            parent = parents.get(id(node))
            if isinstance(parent, (ast.List, ast.Tuple, ast.Set)):
                pos = _offset(lines, node.end_lineno, node.end_col_offset)
                quote = text[pos - 1] if text[pos - 1] in "'\"" else '"'
                patched = text[:pos] + f", {quote}{MODULE}{quote}" + text[pos:]
                return patched, anchor, f"элемент {type(parent).__name__.lower()} (строка {node.lineno})"
            if isinstance(parent, ast.Dict) and any(k is node for k in parent.keys):
                value = parent.values[[i for i, k in enumerate(parent.keys) if k is node][0]]
                if value.end_lineno != node.lineno:
                    continue  # многострочная запись — по образцу не клонируем
                line = lines[node.lineno - 1]
                if not line.rstrip().endswith(","):
                    continue  # последняя запись без запятой — не трогаем
                clone = line.replace(anchor, MODULE)
                patched = "".join(lines[: node.lineno] + [clone if clone.endswith("\n") else clone + "\n"]
                                  + lines[node.lineno:])
                return patched, anchor, f"запись словаря по образцу (строка {node.lineno})"
    return None


def patch(path: Path, dry_run: bool = False) -> int:
    if not path.exists():
        print(f"FAIL: нет файла {path}")
        return 3
    text = path.read_text(encoding="utf-8")
    if re.search(rf"[\"']{MODULE}[\"']", text):
        print(f"OK: {MODULE} уже зарегистрирован в {path.name} (правка не требуется)")
        return 0
    try:
        plan = plan_patch(text)
    except SyntaxError as exc:
        print(f"FAIL: {path.name} не разбирается как Python ({exc}); файл не изменён")
        return 4
    if not plan:
        print(f"SKIP: в {path.name} нет якоря {DEFAULT_ANCHORS} в списке/словаре — реестр устроен иначе")
        return 3
    patched, anchor, where = plan
    try:
        ast.parse(patched)
    except SyntaxError as exc:
        print(f"FAIL: после правки синтаксис битый ({exc}); файл не изменён")
        return 4

    old_lines, new_lines = text.splitlines(), patched.splitlines()
    print(f"ANCHOR: {anchor} в {path.name}: {where}")
    for i, (a, b) in enumerate(zip(old_lines + [""], new_lines)):
        if a != b:
            if len(new_lines) > len(old_lines):
                print(f"  добавлено: {b.strip()[:160]}")
            else:
                print(f"  было:  {a.strip()[:160]}")
                print(f"  стало: {b.strip()[:160]}")
            break

    if dry_run:
        print("DRY-RUN: изменения не записаны")
        return 0

    backup = path.with_suffix(path.suffix + f".bak-reel-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)
    st = path.stat()
    try:
        os.chown(backup, st.st_uid, st.st_gid)  # бэкап с тем же владельцем, что и оригинал
    except OSError:
        pass
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
