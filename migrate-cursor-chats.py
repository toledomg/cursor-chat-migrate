#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migra histórico de chats do Cursor após renomear a pasta do projeto.

Uso:
  1. Feche o Cursor completamente.
  2. Edite OLD_PROJECT_PATH e NEW_PROJECT_PATH abaixo.
  3. Dry-run:  python migrate-cursor-chats.py
  4. Executar:  python migrate-cursor-chats.py --execute

Requisito: abra o projeto NOVO no Cursor pelo menos uma vez antes de rodar,
para existir a pasta em workspaceStorage com o hash do caminho novo.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

# =============================================================================
# CONFIGURAÇÃO — edite apenas estas variáveis
# =============================================================================

OLD_PROJECT_PATH = r"F:\Projetos\meu-projeto-antigo"
NEW_PROJECT_PATH = r"F:\Projetos\meu-projeto-novo"

# Pasta base do Cursor no Windows (raramente precisa mudar)
CURSOR_USER_DATA = Path(os.environ.get("APPDATA", "")) / "Cursor" / "User"

# Pasta de projetos do Cursor (~/.cursor/projects)
CURSOR_HOME_PROJECTS = Path.home() / ".cursor" / "projects"

# =============================================================================


def norm_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def path_to_folder_uri(path: str) -> str:
    normalized = norm_path(path)
    if len(normalized) >= 2 and normalized[1] == ":":
        drive = normalized[0].lower()
        rest = normalized[2:].replace("\\", "/")
        if not rest.startswith("/"):
            rest = "/" + rest
        encoded = quote(f"{drive}:{rest}", safe="/:")
        return f"file:///{encoded}"
    encoded = quote(normalized.replace("\\", "/"), safe="/:")
    return f"file:///{encoded}"


def uri_to_fs_path(uri: str) -> str:
    if not uri:
        return ""
    raw = uri.replace("file:///", "").replace("file://", "")
    decoded = unquote(raw)
    if re.match(r"^[a-zA-Z]:", decoded):
        return norm_path(decoded)
    if decoded.startswith("/") and len(decoded) > 2 and decoded[2] == ":":
        return norm_path(decoded[1:])
    return norm_path(decoded)


def cursor_projects_slug(project_path: str) -> str:
    p = os.path.normpath(project_path)
    if len(p) >= 2 and p[1] == ":":
        p = p[0].lower() + p[2:]
    return p.replace(":", "").replace("\\", "-").replace("/", "-")


def is_cursor_running() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import subprocess

        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Cursor.exe"],
            capture_output=True,
            text=True,
            check=False,
        )
        return "Cursor.exe" in result.stdout
    except OSError:
        return False


def find_workspace_id(project_path: str, workspace_storage: Path) -> str | None:
    target = norm_path(project_path)
    target_uri = path_to_folder_uri(project_path)

    if not workspace_storage.is_dir():
        return None

    for entry in workspace_storage.iterdir():
        if not entry.is_dir():
            continue
        ws_json = entry / "workspace.json"
        if not ws_json.is_file():
            continue
        try:
            data = json.loads(ws_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        folder = data.get("folder", "")
        if not folder:
            continue
        fs_path = uri_to_fs_path(folder)
        if norm_path(fs_path) == target or folder == target_uri:
            return entry.name
    return None


def load_composer_headers(db_path: Path) -> dict[str, Any]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT value FROM ItemTable WHERE key = 'composer.composerHeaders'"
        )
        row = cur.fetchone()
        if not row:
            return {"allComposers": []}
        return json.loads(row[0])
    finally:
        conn.close()


def save_composer_headers(db_path: Path, data: dict[str, Any]) -> None:
    payload = json.dumps(data, ensure_ascii=False)
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)",
            ("composer.composerHeaders", payload),
        )
        conn.commit()
    finally:
        conn.close()


def patch_workspace_identifier(
    composer: dict[str, Any],
    old_path: str,
    new_path: str,
    old_ws_id: str,
    new_ws_id: str,
) -> bool:
    ws = composer.get("workspaceIdentifier")
    if not isinstance(ws, dict):
        return False

    uri = ws.get("uri") if isinstance(ws.get("uri"), dict) else {}
    fs_path = uri.get("fsPath") or ws.get("fsPath") or ""
    ws_id = ws.get("id", "")

    old_norm = norm_path(old_path)
    new_norm = norm_path(new_path)
    fs_norm = norm_path(str(fs_path)) if fs_path else ""

    matches_path = fs_norm == old_norm
    matches_id = ws_id == old_ws_id

    if not matches_path and not matches_id:
        return False

    new_uri = path_to_folder_uri(new_path)
    new_fs = new_norm
    if sys.platform == "win32" and len(new_fs) >= 2 and new_fs[1] == ":":
        display_path = "/" + new_fs[0].upper() + new_fs[2:].replace("\\", "/")
    else:
        display_path = "/" + new_fs.replace("\\", "/")

    win_fs = new_path if os.sep == "\\" else new_fs
    ws["id"] = new_ws_id
    ws["uri"] = {
        "$mid": 1,
        "fsPath": win_fs,
        "_sep": 1,
        "external": new_uri,
        "path": display_path,
        "scheme": "file",
    }
    composer["workspaceIdentifier"] = ws
    return True


def replace_in_blob(data: bytes, old: bytes, new: bytes) -> bytes:
    return data.replace(old, new)


def build_path_replacements(
    old_path: str,
    new_path: str,
    old_ws_id: str,
    new_ws_id: str,
) -> list[tuple[str, str]]:
    old_norm = norm_path(old_path)
    new_norm = norm_path(new_path)
    pairs: list[tuple[str, str]] = [
        (old_ws_id, new_ws_id),
        (old_path, new_path),
        (old_norm, new_norm),
        (old_norm.replace("\\", "/"), new_norm.replace("\\", "/")),
        (path_to_folder_uri(old_path), path_to_folder_uri(new_path)),
    ]
    if len(old_norm) > 3 and old_norm[1] == ":":
        old_uri_path = "/" + old_norm[0].upper() + old_norm[2:].replace("\\", "/")
        new_uri_path = "/" + new_norm[0].upper() + new_norm[2:].replace("\\", "/")
        pairs.append((old_uri_path, new_uri_path))
        pairs.append((old_uri_path.lower(), new_uri_path.lower()))
    unique: list[tuple[str, str]] = []
    seen: set[str] = set()
    for old_v, new_v in pairs:
        if old_v and old_v not in seen and old_v != new_v:
            seen.add(old_v)
            unique.append((old_v, new_v))
    unique.sort(key=lambda x: len(x[0]), reverse=True)
    return unique


def apply_replacements_text(text: str, pairs: list[tuple[str, str]]) -> str:
    result = text
    for old_v, new_v in pairs:
        if old_v in result:
            result = result.replace(old_v, new_v)
    return result


def fetch_disk_kv_candidates(
    cur: sqlite3.Cursor,
    needles: list[str],
) -> list[tuple[str, str | bytes]]:
    seen_keys: set[str] = set()
    rows: list[tuple[str, str | bytes]] = []
    for needle in needles:
        if not needle or len(needle) < 3:
            continue
        pattern = f"%{needle}%"
        cur.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE ? OR value LIKE ?",
            (pattern, pattern),
        )
        for key, value in cur.fetchall():
            if key not in seen_keys:
                seen_keys.add(key)
                rows.append((key, value))
    return rows


def patch_global_disk_kv(
    db_path: Path,
    old_path: str,
    new_path: str,
    old_ws_id: str,
    new_ws_id: str,
    dry_run: bool,
) -> tuple[int, int]:
    pairs = build_path_replacements(old_path, new_path, old_ws_id, new_ws_id)
    pair_bytes = [(a.encode("utf-8"), b.encode("utf-8")) for a, b in pairs]
    needles = [old for old, _ in pairs]

    conn = sqlite3.connect(db_path)
    updated = 0
    scanned = 0
    try:
        cur = conn.cursor()
        print("  - Contando cursorDiskKV (banco grande, aguarde)...", flush=True)
        cur.execute("SELECT COUNT(*) FROM cursorDiskKV")
        scanned = int(cur.fetchone()[0])
        print(
            f"  - Buscando registros com path antigo ({scanned} linhas no total)...",
            flush=True,
        )
        rows = fetch_disk_kv_candidates(cur, needles)
        print(f"  - Candidatos encontrados: {len(rows)}", flush=True)

        for index, (key, value) in enumerate(rows, start=1):
            if value is None:
                continue
            new_key = key
            new_value = value
            changed = False

            if isinstance(key, str):
                patched = apply_replacements_text(key, pairs)
                if patched != key:
                    new_key = patched
                    changed = True

            if isinstance(value, str):
                patched = apply_replacements_text(value, pairs)
                if patched != value:
                    new_value = patched
                    changed = True
            elif isinstance(value, bytes):
                patched = value
                for old_b, new_b in pair_bytes:
                    if old_b in patched:
                        patched = replace_in_blob(patched, old_b, new_b)
                        changed = True

            if changed:
                updated += 1
                if not dry_run:
                    cur.execute(
                        "UPDATE cursorDiskKV SET key = ?, value = ? WHERE key = ?",
                        (new_key, new_value, key),
                    )
                if index % 25 == 0 or index == len(rows):
                    print(f"  - Progresso: {index}/{len(rows)}", flush=True)

        if not dry_run:
            print("  - Gravando alteracoes no banco...", flush=True)
            conn.commit()
    finally:
        conn.close()

    return scanned, updated


def copy_workspace_files(
    src_dir: Path,
    dst_dir: Path,
    new_project_path: str,
    dry_run: bool,
) -> list[str]:
    actions: list[str] = []
    if not src_dir.is_dir():
        raise FileNotFoundError(f"Pasta workspace antiga não encontrada: {src_dir}")

    dst_dir.mkdir(parents=True, exist_ok=True)

    for item in src_dir.iterdir():
        dest = dst_dir / item.name
        if item.name in ("state.vscdb-shm", "state.vscdb-wal"):
            actions.append(f"Ignorar WAL/SHM: {item.name}")
            continue
        if item.is_dir():
            actions.append(f"Copiar pasta: {item.name}")
            if not dry_run:
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(item, dest)
        else:
            actions.append(f"Copiar arquivo: {item.name}")
            if not dry_run:
                shutil.copy2(item, dest)

    ws_json = dst_dir / "workspace.json"
    content = (
        json.dumps(
            {"folder": path_to_folder_uri(new_project_path)},
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )
    actions.append(f"Atualizar workspace.json -> {new_project_path}")
    if not dry_run:
        ws_json.write_text(content, encoding="utf-8")

    return actions


def migrate_agent_transcripts(
    old_path: str,
    new_path: str,
    dry_run: bool,
) -> list[str]:
    actions: list[str] = []
    old_slug = cursor_projects_slug(old_path)
    new_slug = cursor_projects_slug(new_path)
    src = CURSOR_HOME_PROJECTS / old_slug / "agent-transcripts"
    dst = CURSOR_HOME_PROJECTS / new_slug / "agent-transcripts"

    if not src.is_dir():
        actions.append(f"Sem agent-transcripts em {src} (opcional)")
        return actions

    actions.append(f"Transcripts: {src} -> {dst}")
    if not dry_run:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            for child in src.iterdir():
                target = dst / child.name
                if child.is_dir():
                    if target.exists():
                        shutil.rmtree(target)
                    shutil.copytree(child, target)
                else:
                    shutil.copy2(child, target)
        else:
            shutil.copytree(src, dst)

    return actions


def create_backup(
    backup_root: Path,
    paths: list[Path],
    dry_run: bool,
) -> tuple[Path, list[str]]:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = backup_root / f"cursor-chat-migration-{timestamp}"
    actions: list[str] = []

    for src in paths:
        if not src.exists():
            actions.append(f"Pular (não existe): {src}")
            continue
        dest = backup_dir / src.name if src.is_file() else backup_dir / src.name
        actions.append(f"Backup: {src} -> {dest}")
        if not dry_run:
            backup_dir.mkdir(parents=True, exist_ok=True)
            if src.is_file():
                shutil.copy2(src, dest)
            else:
                dest.mkdir(parents=True, exist_ok=True)
                shutil.copytree(src, dest, dirs_exist_ok=True)

    return backup_dir, actions


def run_migration(execute: bool, skip_disk_kv: bool = False) -> int:
    dry_run = not execute

    print("=" * 60)
    print("Migração de chats Cursor")
    print("=" * 60)
    print(f"Modo: {'EXECUÇÃO' if execute else 'DRY-RUN (simulação)'}")
    print(f"Antigo: {OLD_PROJECT_PATH}")
    print(f"Novo:   {NEW_PROJECT_PATH}")
    print()

    if not CURSOR_USER_DATA.is_dir():
        print(f"ERRO: Cursor User não encontrado em {CURSOR_USER_DATA}")
        return 1

    if not Path(NEW_PROJECT_PATH).is_dir():
        print(f"ERRO: Pasta nova não existe: {NEW_PROJECT_PATH}")
        return 1

    if execute and is_cursor_running():
        print("ERRO: Cursor.exe está em execução. Feche o Cursor antes de --execute.")
        return 1
    if not execute and is_cursor_running():
        print("AVISO: Cursor aberto — dry-run ok; para --execute, feche o Cursor.")

    workspace_storage = CURSOR_USER_DATA / "workspaceStorage"
    global_db = CURSOR_USER_DATA / "globalStorage" / "state.vscdb"

    old_ws_id = find_workspace_id(OLD_PROJECT_PATH, workspace_storage)
    new_ws_id = find_workspace_id(NEW_PROJECT_PATH, workspace_storage)

    print(f"Workspace ID antigo: {old_ws_id or 'NÃO ENCONTRADO'}")
    print(f"Workspace ID novo:   {new_ws_id or 'NÃO ENCONTRADO'}")
    print()

    if not old_ws_id:
        print(
            "ERRO: Não achei workspaceStorage do caminho antigo.\n"
            "  O workspace.json ainda precisa existir em AppData apontando para OLD_PROJECT_PATH."
        )
        return 1

    if not new_ws_id:
        print(
            "ERRO: Não achei workspace do caminho novo.\n"
            "  Abra o projeto NOVO no Cursor uma vez (File -> Open Folder) e rode de novo."
        )
        return 1

    if old_ws_id == new_ws_id:
        print(
            "AVISO: IDs antigo e novo são iguais — nada a migrar no workspaceStorage."
        )
        return 0

    old_ws_dir = workspace_storage / old_ws_id
    new_ws_dir = workspace_storage / new_ws_id

    headers_data = load_composer_headers(global_db)
    composers = headers_data.get("allComposers", [])
    if not isinstance(composers, list):
        composers = []

    patched_composers = 0
    for c in composers:
        if not isinstance(c, dict):
            continue
        if patch_workspace_identifier(
            c, OLD_PROJECT_PATH, NEW_PROJECT_PATH, old_ws_id, new_ws_id
        ):
            patched_composers += 1

    print(f"Chats a relinkar em composer.composerHeaders: {patched_composers}")

    backup_targets = [
        old_ws_dir,
        new_ws_dir,
        global_db,
    ]
    backup_parent = CURSOR_USER_DATA / "backups"
    backup_dir, backup_actions = create_backup(backup_parent, backup_targets, dry_run)

    print(f"\nBackup em: {backup_dir}")
    for line in backup_actions:
        print(f"  - {line}")

    print("\n--- workspaceStorage ---")
    for line in copy_workspace_files(old_ws_dir, new_ws_dir, NEW_PROJECT_PATH, dry_run):
        print(f"  - {line}")

    print("\n--- agent-transcripts ---")
    for line in migrate_agent_transcripts(OLD_PROJECT_PATH, NEW_PROJECT_PATH, dry_run):
        print(f"  - {line}")

    print("\n--- globalStorage (composer.composerHeaders) ---")
    if dry_run:
        print(f"  - Atualizaria {patched_composers} conversas")
    else:
        save_composer_headers(global_db, headers_data)
        print(f"  - Atualizado {patched_composers} conversas")

    if skip_disk_kv:
        print("\n--- globalStorage (cursorDiskKV) ---")
        print("  - Pulado (--skip-disk-kv). Lista de chats ja foi relinkada acima.")
    else:
        print("\n--- globalStorage (cursorDiskKV, paths antigos) ---")
        print("  - Etapa lenta (1-5 min). Pode pular com --skip-disk-kv se travar.")
        scanned, kv_updated = patch_global_disk_kv(
            global_db,
            OLD_PROJECT_PATH,
            NEW_PROJECT_PATH,
            old_ws_id,
            new_ws_id,
            dry_run,
        )
        print(f"  - Total no cursorDiskKV: {scanned}")
        print(f"  - Registros com path antigo (atualizados): {kv_updated}")

    print()
    if dry_run:
        print("Simulação concluída. Rode com --execute para aplicar.")
    else:
        print("Migração concluída.")
        print(f"Abra no Cursor: {NEW_PROJECT_PATH}")
        print("Os chats antigos devem aparecer no histórico do projeto novo.")

    return 0


def run_junction(execute: bool) -> int:
    dry_run = not execute
    link = OLD_PROJECT_PATH
    target = NEW_PROJECT_PATH

    print("Modo JUNCTION (atalho do caminho antigo -> pasta nova)")
    print(f"Link:   {link}")
    print(f"Alvo:   {target}")

    if Path(link).exists():
        print(f"ERRO: Já existe: {link}")
        return 1
    if not Path(target).is_dir():
        print(f"ERRO: Pasta alvo não existe: {target}")
        return 1
    if is_cursor_running():
        print("ERRO: Feche o Cursor antes.")
        return 1

    cmd = f'cmd /c mklink /J "{link}" "{target}"'
    print(f"Comando: {cmd}")
    if dry_run:
        print("Dry-run. Use --execute para criar o junction.")
        print(f"Depois abra no Cursor: {link}")
        return 0

    code = os.system(cmd)
    if code != 0:
        print("ERRO ao criar junction. Execute o terminal como Administrador.")
        return 1

    print(f"Junction criado. Abra no Cursor: {link}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migra histórico de chats do Cursor após renomear pasta do projeto."
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Aplica alterações (sem isso, só simula).",
    )
    parser.add_argument(
        "--junction",
        action="store_true",
        help="Cria junction do caminho antigo -> novo (método simples, sem mexer no DB).",
    )
    parser.add_argument(
        "--skip-disk-kv",
        action="store_true",
        help="Pula patch do cursorDiskKV (etapa lenta; chats na UI usam composerHeaders).",
    )
    args = parser.parse_args()

    if args.junction:
        return run_junction(args.execute)
    return run_migration(args.execute, skip_disk_kv=args.skip_disk_kv)


if __name__ == "__main__":
    raise SystemExit(main())
