#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migra histórico de chats do Cursor após renomear a pasta do projeto.

Só metadados do Cursor são alterados (workspaceStorage, globalStorage e
agent-transcripts). As pastas do projeto no disco nunca são movidas,
renomeadas ou apagadas.

Uso:
  1. Feche o Cursor completamente.
  2. Rode:  python migrate-cursor-chats.py
     (se não houver .env, o script pergunta origem e destino)
  3. Confira o dry-run e rode:  python migrate-cursor-chats.py --execute

Requisito: abra o projeto NOVO no Cursor pelo menos uma vez antes de rodar,
para existir a pasta em workspaceStorage com o hash do caminho novo.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import quote, unquote

from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(SCRIPT_DIR / ".env")


def _clean_path(value: str) -> str:
    """Remove aspas e espaços de caminhos colados no terminal."""
    cleaned = value.strip().strip('"').strip("'").strip()
    return cleaned


def _env_path(name: str) -> str:
    return _clean_path(os.getenv(name, ""))


def _ask_path(prompt: str) -> str:
    if not sys.stdin.isatty():
        print(
            f"ERRO: {prompt} não informado.\n"
            "  Defina no .env, passe --from/--to, ou rode em um terminal interativo."
        )
        raise SystemExit(1)

    while True:
        try:
            answer = _clean_path(input(f"{prompt}: "))
        except EOFError:
            print("\nCancelado.")
            raise SystemExit(1) from None
        if not answer:
            print("  Informe um caminho (ou Ctrl+C para cancelar).")
            continue
        return answer


def _offer_save_env(old_path: str, new_path: str) -> None:
    """Pergunta se deve gravar os caminhos no .env para a próxima execução."""
    if not sys.stdin.isatty():
        return
    try:
        answer = input("Salvar esses caminhos no .env para a próxima vez? [s/N]: ")
    except EOFError:
        return
    if answer.strip().lower() not in ("s", "sim", "y", "yes"):
        return

    env_path = SCRIPT_DIR / ".env"
    lines = [
        f"OLD_PROJECT_PATH={old_path}",
        f"NEW_PROJECT_PATH={new_path}",
        "",
    ]
    try:
        env_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"  Salvo em {env_path}")
    except OSError as exc:
        print(f"  AVISO: não consegui gravar .env: {exc}")


def _project_paths(old_path: str | None, new_path: str | None) -> tuple[str, str]:
    """Caminhos: CLI > .env > pergunta interativa."""
    old_value = _clean_path(old_path or "") or _env_path("OLD_PROJECT_PATH")
    new_value = _clean_path(new_path or "") or _env_path("NEW_PROJECT_PATH")
    asked = False

    if not old_value:
        print("Caminho de origem não definido no .env.")
        old_value = _ask_path("Pasta ORIGEM (caminho antigo do projeto)")
        asked = True
    if not new_value:
        print("Caminho de destino não definido no .env.")
        new_value = _ask_path("Pasta DESTINO (caminho novo do projeto)")
        asked = True

    if asked:
        _offer_save_env(old_value, new_value)

    return old_value, new_value


COMPOSER_HEADERS_KEY = "composer.composerHeaders"
BACKUP_PREFIX = "cursor-chat-migration-"

_default_cursor_user = Path(os.environ.get("APPDATA", "")) / "Cursor" / "User"
CURSOR_USER_DATA = Path(os.getenv("CURSOR_USER_DATA", "") or _default_cursor_user)

# Pasta de projetos do Cursor (~/.cursor/projects)
CURSOR_HOME_PROJECTS = Path(
    os.getenv("CURSOR_HOME_PROJECTS", "") or (Path.home() / ".cursor" / "projects")
)


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
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Cursor.exe"],
            capture_output=True,
            text=True,
            check=False,
        )
        return "Cursor.exe" in result.stdout
    except OSError:
        return False


def quit_cursor(timeout_s: int = 15) -> bool:
    """Encerra o Cursor: primeiro pedido normal, depois forçado."""
    if not is_cursor_running():
        return True
    if sys.platform != "win32":
        return False

    for args in (
        ["taskkill", "/IM", "Cursor.exe"],
        ["taskkill", "/IM", "Cursor.exe", "/T", "/F"],
    ):
        print(f"Encerrando o Cursor ({' '.join(args[1:])})...", flush=True)
        try:
            subprocess.run(args, capture_output=True, text=True, check=False)
        except OSError as exc:
            print(f"AVISO: falha ao chamar taskkill: {exc}")
            return False

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not is_cursor_running():
                return True
            time.sleep(1)

    return not is_cursor_running()


def ensure_cursor_closed(execute: bool, quit_flag: bool, force: bool) -> bool:
    """Garante Cursor fechado antes de gravar (dry-run só avisa)."""
    if not is_cursor_running():
        return True

    if not execute:
        print("AVISO: Cursor aberto — dry-run ok; para --execute, feche o Cursor.")
        return True

    if force:
        print(
            "AVISO: Cursor aberto (--force) — o Cursor pode desfazer as "
            "alterações ao ser fechado."
        )
        return True

    if not quit_flag and sys.stdin.isatty():
        answer = input("Cursor está aberto. Encerrar agora? [s/N]: ").strip().lower()
        quit_flag = answer in ("s", "sim", "y", "yes")

    if quit_flag:
        if quit_cursor():
            return True
        print("ERRO: não consegui encerrar o Cursor. Feche manualmente e rode de novo.")
        return False

    print(
        "ERRO: Cursor.exe está em execução. Feche o Cursor antes de --execute\n"
        "  (ou use --quit-cursor para encerrar automaticamente)."
    )
    return False


class WorkspaceMatch(NamedTuple):
    id: str
    size: int
    mtime: float


def find_workspace_matches(
    project_path: str,
    workspace_storage: Path,
) -> list[WorkspaceMatch]:
    """Workspaces cujo workspace.json aponta para o projeto, maior banco primeiro."""
    target = norm_path(project_path)
    target_uri = path_to_folder_uri(project_path)
    matches: list[WorkspaceMatch] = []

    if not workspace_storage.is_dir():
        return matches

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
        if norm_path(fs_path) != target and folder != target_uri:
            continue

        db = entry / "state.vscdb"
        stat = db.stat() if db.is_file() else None
        matches.append(
            WorkspaceMatch(
                entry.name,
                stat.st_size if stat else 0,
                stat.st_mtime if stat else 0.0,
            )
        )

    return sorted(matches, key=lambda match: match.size, reverse=True)


def find_workspace_id(project_path: str, workspace_storage: Path) -> str | None:
    """Melhor origem de histórico: o workspace com o maior banco."""
    matches = find_workspace_matches(project_path, workspace_storage)
    return matches[0].id if matches else None


def find_active_workspace_id(project_path: str, workspace_storage: Path) -> str | None:
    """Workspace que o Cursor usa hoje no caminho: o mais recente."""
    matches = find_workspace_matches(project_path, workspace_storage)
    if not matches:
        return None
    return max(matches, key=lambda match: match.mtime).id


def folder_birthtime_ms(path: Path) -> int | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    birthtime = getattr(stat, "st_birthtime", None) or stat.st_ctime
    return int(birthtime * 1000)


def workspace_id_candidates(project_path: str) -> list[str]:
    """IDs que o Cursor pode gerar: md5(caminho + birthtime), com folga de 1 ms."""
    birthtime = folder_birthtime_ms(Path(project_path))
    if birthtime is None:
        return []

    base = os.path.normpath(project_path)
    candidates: list[str] = []
    for delta in (0, -1, 1):
        digest = hashlib.md5(f"{base}{birthtime + delta}".encode("utf-8")).hexdigest()
        if digest not in candidates:
            candidates.append(digest)
    return candidates


def mirror_workspace_ids(
    project_path: str,
    workspace_storage: Path,
    primary_id: str,
) -> list[str]:
    """Destinos da cópia: o principal e outras pastas já existentes do mesmo projeto."""
    ids = [primary_id]
    for match in find_workspace_matches(project_path, workspace_storage):
        if match.id not in ids:
            ids.append(match.id)
    for candidate in workspace_id_candidates(project_path):
        if candidate not in ids and (workspace_storage / candidate).is_dir():
            ids.append(candidate)
    return ids


def load_composer_headers(db_path: Path) -> dict[str, Any]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT value FROM ItemTable WHERE key = ?",
            (COMPOSER_HEADERS_KEY,),
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
            (COMPOSER_HEADERS_KEY, payload),
        )
        conn.commit()
    finally:
        conn.close()


def has_composer_headers_table(db_path: Path) -> bool:
    """Cursor recente: lista de chats mora na tabela composerHeaders (não no JSON)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='composerHeaders'"
        )
        return cur.fetchone() is not None
    finally:
        conn.close()


def table_gate_enabled(db_path: Path) -> bool:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT value FROM ItemTable WHERE key = 'composer.composerHeaders.tableGateEnabled'"
        )
        row = cur.fetchone()
        if not row:
            return False
        value = row[0]
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        return str(value).strip().lower() in ("true", "1")
    finally:
        conn.close()


def count_composer_headers_table(db_path: Path, workspace_id: str) -> int:
    if not workspace_id or not has_composer_headers_table(db_path):
        return 0
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM composerHeaders WHERE workspaceId = ?",
            (workspace_id,),
        )
        return int(cur.fetchone()[0])
    finally:
        conn.close()


def list_composer_ids_from_headers_table(
    db_path: Path,
    workspace_id: str,
) -> list[str]:
    if not workspace_id or not has_composer_headers_table(db_path):
        return []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT composerId FROM composerHeaders WHERE workspaceId = ? "
            "ORDER BY COALESCE(recency, lastUpdatedAt, createdAt) DESC",
            (workspace_id,),
        )
        return [row[0] for row in cur.fetchall() if row[0]]
    finally:
        conn.close()


def migrate_composer_headers_table(
    db_path: Path,
    old_ws_id: str,
    new_ws_id: str,
    old_path: str,
    new_path: str,
    dry_run: bool,
) -> tuple[int, int]:
    """
    Relinka chats na tabela composerHeaders (fonte da UI quando tableGate=true).
    Funciona para qualquer projeto: só troca workspaceId (+ paths no JSON value).
    Retorna (linhas_afetadas, values_com_path_atualizado).
    """
    if not old_ws_id or not new_ws_id or old_ws_id == new_ws_id:
        return 0, 0
    if not has_composer_headers_table(db_path):
        return 0, 0

    pairs = build_path_replacements(old_path, new_path, old_ws_id, new_ws_id)
    conn = sqlite3.connect(db_path)
    moved = 0
    value_patched = 0
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT composerId, value FROM composerHeaders WHERE workspaceId = ?",
            (old_ws_id,),
        )
        rows = cur.fetchall()
        moved = len(rows)
        if dry_run:
            for _, value in rows:
                if isinstance(value, str) and apply_replacements_text(value, pairs) != value:
                    value_patched += 1
            return moved, value_patched

        for composer_id, value in rows:
            new_value = value
            if isinstance(value, str):
                patched = apply_replacements_text(value, pairs)
                if patched != value:
                    new_value = patched
                    value_patched += 1
            cur.execute(
                "UPDATE composerHeaders SET workspaceId = ?, value = ? "
                "WHERE composerId = ? AND workspaceId = ?",
                (new_ws_id, new_value, composer_id, old_ws_id),
            )
        conn.commit()
    finally:
        conn.close()
    return moved, value_patched


def patch_glass_local_agent_projects(
    db_path: Path,
    old_path: str,
    new_path: str,
    old_ws_id: str,
    new_ws_id: str,
    dry_run: bool,
) -> int:
    """Atualiza glass.localAgentProjects.v1 (projetos do Agents Window)."""
    key = "glass.localAgentProjects.v1"
    conn = sqlite3.connect(db_path)
    updated = 0
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM ItemTable WHERE key = ?", (key,))
        row = cur.fetchone()
        if not row or not isinstance(row[0], str):
            return 0
        try:
            projects = json.loads(row[0])
        except json.JSONDecodeError:
            return 0
        if not isinstance(projects, list):
            return 0

        for project in projects:
            if not isinstance(project, dict):
                continue
            workspace = project.get("workspace")
            if not isinstance(workspace, dict):
                continue
            matches = False
            if old_ws_id and workspace.get("id") == old_ws_id:
                matches = True
            uri = workspace.get("uri")
            uri_dict = uri if isinstance(uri, dict) else {}
            fs_path = str(uri_dict.get("fsPath") or workspace.get("fsPath") or "")
            if fs_path and norm_path(fs_path) == norm_path(old_path):
                matches = True
            if not matches:
                continue
            project["workspace"] = make_workspace_identifier(new_path, new_ws_id)
            updated += 1

        if updated and not dry_run:
            cur.execute(
                "INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)",
                (key, json.dumps(projects, ensure_ascii=False)),
            )
            conn.commit()
    finally:
        conn.close()
    return updated


def composer_matches(
    composer: dict[str, Any],
    ws_id: str,
    project_path: str,
) -> bool:
    ws = composer.get("workspaceIdentifier")
    if not isinstance(ws, dict):
        return False
    if ws_id and ws.get("id") == ws_id:
        return True
    uri_raw = ws.get("uri")
    uri: dict[str, Any] = uri_raw if isinstance(uri_raw, dict) else {}
    fs_path = uri.get("fsPath") or ws.get("fsPath") or ""
    return bool(fs_path) and norm_path(str(fs_path)) == norm_path(project_path)


def count_composers(
    composers: list[Any],
    ws_id: str,
    project_path: str,
) -> int:
    return sum(
        1
        for c in composers
        if isinstance(c, dict) and composer_matches(c, ws_id, project_path)
    )


def make_workspace_identifier(project_path: str, workspace_id: str) -> dict[str, Any]:
    new_norm = norm_path(project_path)
    new_uri = path_to_folder_uri(project_path)
    if sys.platform == "win32" and len(new_norm) >= 2 and new_norm[1] == ":":
        display_path = "/" + new_norm[0].upper() + new_norm[2:].replace("\\", "/")
    else:
        display_path = "/" + new_norm.replace("\\", "/")
    win_fs = project_path if os.sep == "\\" else new_norm
    return {
        "id": workspace_id,
        "uri": {
            "$mid": 1,
            "fsPath": win_fs,
            "_sep": 1,
            "external": new_uri,
            "path": display_path,
            "scheme": "file",
        },
    }


def patch_workspace_identifier(
    composer: dict[str, Any],
    old_path: str,
    new_path: str,
    old_ws_id: str,
    new_ws_id: str,
) -> bool:
    ws = composer.get("workspaceIdentifier")
    if not isinstance(ws, dict) or not composer_matches(composer, old_ws_id, old_path):
        return False
    composer["workspaceIdentifier"] = make_workspace_identifier(new_path, new_ws_id)
    return True


def collect_composer_ids_from_workspace(ws_dir: Path) -> list[str]:
    """IDs de chats no state.vscdb local (composerData + painéis)."""
    db_path = ws_dir / "state.vscdb"
    if not db_path.is_file():
        return []

    ids: list[str] = []
    seen: set[str] = set()

    def add(composer_id: str) -> None:
        composer_id = (composer_id or "").strip()
        if not composer_id or composer_id in seen:
            return
        seen.add(composer_id)
        ids.append(composer_id)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT value FROM ItemTable WHERE key = 'composer.composerData'"
        )
        row = cur.fetchone()
        if row and isinstance(row[0], str):
            try:
                data = json.loads(row[0])
            except json.JSONDecodeError:
                data = {}
            for key in ("selectedComposerIds", "lastFocusedComposerIds"):
                for composer_id in data.get(key) or []:
                    if isinstance(composer_id, str):
                        add(composer_id)

        cur.execute("SELECT value FROM ItemTable WHERE key = 'cursor/pinnedComposers'")
        row = cur.fetchone()
        if row and isinstance(row[0], str):
            try:
                pinned = json.loads(row[0])
            except json.JSONDecodeError:
                pinned = []
            if isinstance(pinned, list):
                for composer_id in pinned:
                    if isinstance(composer_id, str):
                        add(composer_id)

        cur.execute(
            "SELECT key FROM ItemTable WHERE key LIKE 'workbench.panel.composerChatViewPane.%'"
        )
        for (key,) in cur.fetchall():
            add(key.rsplit(".", 1)[-1])
        cur.execute(
            "SELECT key FROM ItemTable WHERE key LIKE 'workbench.panel.aichat.%'"
        )
        for (key,) in cur.fetchall():
            # workbench.panel.aichat.<uuid>.numberOfVisibleViews
            parts = key.split(".")
            if len(parts) >= 4:
                add(parts[3])
    finally:
        conn.close()

    return ids


def load_composer_data_blob(db_path: Path, composer_id: str) -> dict[str, Any] | None:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT value FROM cursorDiskKV WHERE key = ?",
            (f"composerData:{composer_id}",),
        )
        row = cur.fetchone()
        if not row or row[0] is None:
            return None
        value = row[0]
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if not isinstance(value, str):
            return None
        data = json.loads(value)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, sqlite3.Error):
        return None
    finally:
        conn.close()


HEADER_FIELDS = (
    "composerId",
    "name",
    "subtitle",
    "createdAt",
    "lastUpdatedAt",
    "conversationCheckpointLastUpdatedAt",
    "unifiedMode",
    "forceMode",
    "type",
    "contextUsagePercent",
    "totalLinesAdded",
    "totalLinesRemoved",
    "filesChangedCount",
    "hasUnreadMessages",
    "hasBlockingPendingActions",
    "hasPendingPlan",
    "isArchived",
    "isDraft",
    "isWorktree",
    "isSpec",
    "isProject",
    "isBestOfNSubcomposer",
    "numSubComposers",
)


def header_from_composer_data(
    data: dict[str, Any],
    new_path: str,
    new_ws_id: str,
) -> dict[str, Any]:
    header: dict[str, Any] = {"type": data.get("type") or "head"}
    for field in HEADER_FIELDS:
        if field in data and data[field] is not None:
            header[field] = data[field]
    if "composerId" not in header and data.get("composerId"):
        header["composerId"] = data["composerId"]
    # Defaults exigidos pela UI do Agents (null/ausente esconde o chat).
    header.setdefault("isArchived", False)
    header.setdefault("isDraft", False)
    header.setdefault("isWorktree", False)
    header.setdefault("isSpec", False)
    header.setdefault("isProject", False)
    header.setdefault("isBestOfNSubcomposer", False)
    header.setdefault("hasUnreadMessages", False)
    header.setdefault("hasBlockingPendingActions", False)
    header.setdefault("hasPendingPlan", False)
    header.setdefault("worktreeStartedReadOnly", False)
    header.setdefault("numSubComposers", 0)
    header.setdefault("referencedPlans", [])
    header.setdefault("trackedGitRepos", [])
    header["workspaceIdentifier"] = make_workspace_identifier(new_path, new_ws_id)
    return header


def merge_composers_list(
    existing: list[Any],
    by_id: dict[str, dict[str, Any]],
) -> list[Any]:
    """Atualiza headers existentes e coloca os migrados no topo (mais recentes)."""
    migrated_ids = set(by_id)
    others = [
        c
        for c in existing
        if isinstance(c, dict)
        and (c.get("composerId") or c.get("id")) not in migrated_ids
    ]
    migrated = sorted(
        by_id.values(),
        key=lambda c: int(c.get("lastUpdatedAt") or c.get("createdAt") or 0),
        reverse=True,
    )
    return migrated + others


def sync_workspace_composer_data(
    ws_dir: Path,
    composer_ids: list[str],
    dry_run: bool,
) -> list[str]:
    """Garante selectedComposerIds no state.vscdb do workspace destino."""
    actions: list[str] = []
    db_path = ws_dir / "state.vscdb"
    if not db_path.is_file():
        return [f"Sem state.vscdb em {ws_dir}"]
    if not composer_ids:
        return ["Sem composer IDs para sincronizar no workspace"]

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT value FROM ItemTable WHERE key = 'composer.composerData'"
        )
        row = cur.fetchone()
        try:
            data = json.loads(row[0]) if row and isinstance(row[0], str) else {}
        except json.JSONDecodeError:
            data = {}
        if not isinstance(data, dict):
            data = {}

        selected = [cid for cid in composer_ids if cid]
        data["selectedComposerIds"] = selected
        data["lastFocusedComposerIds"] = selected[:1]
        data["hasMigratedComposerData"] = True
        data["hasMigratedMultipleComposers"] = True
        payload = json.dumps(data, ensure_ascii=False)
        actions.append(
            f"Sincronizar composer.composerData ({len(selected)} chats) em {ws_dir.name}"
        )
        if not dry_run:
            cur.execute(
                "INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)",
                ("composer.composerData", payload),
            )
            conn.commit()
    finally:
        conn.close()
    return actions


def save_composer_data_blob(
    db_path: Path,
    composer_id: str,
    data: dict[str, Any],
) -> None:
    payload = json.dumps(data, ensure_ascii=False)
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO cursorDiskKV (key, value) VALUES (?, ?)",
            (f"composerData:{composer_id}", payload),
        )
        conn.commit()
    finally:
        conn.close()


def rebuild_composer_headers_from_workspace(
    global_db: Path,
    ws_dir: Path | None,
    old_path: str,
    new_path: str,
    old_ws_id: str,
    new_ws_id: str,
    composers: list[Any],
    dry_run: bool,
) -> tuple[int, int, list[Any], list[str]]:
    """
    Quando composer.composerHeaders não lista os chats do projeto, reconstrói
    a partir dos IDs do workspace + blobs composerData:* no banco global.
    Retorna (criados, blobs_atualizados, lista_headers, ids_migrados).
    """
    if ws_dir is None:
        return 0, 0, composers, []

    composer_ids = collect_composer_ids_from_workspace(ws_dir)
    if not composer_ids:
        return 0, 0, composers, []

    migrated: dict[str, dict[str, Any]] = {}
    created = 0
    updated_blobs = 0

    # Também corrige headers já apontando para o destino (re-run).
    existing_by_id: dict[str, dict[str, Any]] = {}
    for item in composers:
        if not isinstance(item, dict):
            continue
        cid = item.get("composerId") or item.get("id")
        if isinstance(cid, str) and cid:
            existing_by_id[cid] = item

    for composer_id in composer_ids:
        data = load_composer_data_blob(global_db, composer_id)
        if not data:
            continue

        blob_ws = data.get("workspaceIdentifier")
        matches_old = True
        matches_new = False
        if isinstance(blob_ws, dict):
            blob_id = blob_ws.get("id", "")
            uri_raw = blob_ws.get("uri")
            uri = uri_raw if isinstance(uri_raw, dict) else {}
            fs_path = str(uri.get("fsPath") or blob_ws.get("fsPath") or "")
            matches_old = (old_ws_id and blob_id == old_ws_id) or (
                fs_path and norm_path(fs_path) == norm_path(old_path)
            )
            matches_new = (blob_id == new_ws_id) or (
                fs_path and norm_path(fs_path) == norm_path(new_path)
            )
            if not matches_old and not matches_new:
                continue

        data["workspaceIdentifier"] = make_workspace_identifier(new_path, new_ws_id)
        if matches_old or matches_new:
            updated_blobs += 1
            if not dry_run:
                save_composer_data_blob(global_db, composer_id, data)

        header = header_from_composer_data(data, new_path, new_ws_id)
        existing = existing_by_id.get(composer_id)
        if existing is None:
            created += 1
        else:
            # Mescla defaults em cima do header antigo (corrige isArchived null etc.).
            merged = dict(existing)
            merged.update(header)
            header = merged
        migrated[composer_id] = header

    if migrated:
        composers = merge_composers_list(composers, migrated)

    return created, updated_blobs, composers, list(migrated.keys())


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

    # Paths gravados dentro de JSON aparecem com barras escapadas (F:\\Projetos).
    pairs.extend(
        (old_v.replace("\\", "\\\\"), new_v.replace("\\", "\\\\"))
        for old_v, new_v in list(pairs)
        if "\\" in old_v
    )

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
    *,
    ws_ids: list[str] | None = None,
) -> list[tuple[str, str | bytes]]:
    seen_keys: set[str] = set()
    rows: list[tuple[str, str | bytes]] = []

    # IDs de workspace são o filtro mais seletivo (padrão usado na comunidade).
    search_terms: list[str] = []
    for ws_id in ws_ids or []:
        if ws_id and ws_id not in search_terms:
            search_terms.append(ws_id)
    for needle in needles:
        if needle and needle not in search_terms:
            search_terms.append(needle)

    for needle in search_terms:
        if not needle or len(needle) < 3:
            continue
        pattern = f"%{needle}%"
        print(f"  - Buscando cursorDiskKV com: {needle[:48]}...", flush=True)
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

    db_size_gb = db_path.stat().st_size / (1024**3)
    if db_size_gb >= 1:
        print(
            f"  - AVISO: state.vscdb tem {db_size_gb:.1f} GB — varredura pode levar horas.",
            flush=True,
        )

    conn = sqlite3.connect(db_path)
    updated = 0
    try:
        cur = conn.cursor()
        print("  - Buscando registros por workspace ID e paths antigos...", flush=True)
        rows = fetch_disk_kv_candidates(
            cur, needles, ws_ids=[old_ws_id, new_ws_id]
        )
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

    return len(rows), updated


def patch_global_item_table(
    db_path: Path,
    old_path: str,
    new_path: str,
    old_ws_id: str,
    new_ws_id: str,
    dry_run: bool,
) -> int:
    """Troca paths/IDs antigos na ItemTable (tabela pequena, rápida)."""
    pairs = build_path_replacements(old_path, new_path, old_ws_id, new_ws_id)
    pair_bytes = [(a.encode("utf-8"), b.encode("utf-8")) for a, b in pairs]

    conn = sqlite3.connect(db_path)
    updated = 0
    try:
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM ItemTable")
        for key, value in cur.fetchall():
            if key == COMPOSER_HEADERS_KEY:
                continue

            new_key = apply_replacements_text(key, pairs) if isinstance(key, str) else key
            new_value = value
            if isinstance(value, str):
                new_value = apply_replacements_text(value, pairs)
            elif isinstance(value, bytes):
                for old_b, new_b in pair_bytes:
                    if old_b in new_value:
                        new_value = replace_in_blob(new_value, old_b, new_b)

            if new_key == key and new_value == value:
                continue

            updated += 1
            if dry_run:
                continue
            if new_key != key:
                cur.execute("DELETE FROM ItemTable WHERE key = ?", (key,))
            cur.execute(
                "INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)",
                (new_key, new_value),
            )

        if not dry_run:
            conn.commit()
    finally:
        conn.close()

    return updated


def patch_storage_json(
    storage_json: Path,
    old_path: str,
    new_path: str,
    old_ws_id: str,
    new_ws_id: str,
    dry_run: bool,
) -> list[str]:
    """Atualiza o menu 'Open Recent' e mapeamentos de janela."""
    if not storage_json.is_file():
        return [f"Sem storage.json em {storage_json} (opcional)"]

    try:
        text = storage_json.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"Falha ao ler storage.json: {exc}"]

    pairs = build_path_replacements(old_path, new_path, old_ws_id, new_ws_id)
    patched = apply_replacements_text(text, pairs)
    if patched == text:
        return ["Nenhuma referência antiga em storage.json"]

    if dry_run:
        return ["Atualizaria referências de path em storage.json"]

    storage_json.write_text(patched, encoding="utf-8")
    return ["Referências de path atualizadas em storage.json"]


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


def backup_entry_name(src: Path, used: set[str]) -> str:
    if src.name not in used:
        return src.name
    candidate = f"{src.parent.name}-{src.name}"
    suffix = 2
    while candidate in used:
        candidate = f"{src.parent.name}-{src.name}-{suffix}"
        suffix += 1
    return candidate


def create_backup(
    backup_root: Path,
    paths: list[Path],
    dry_run: bool,
    meta: dict[str, Any] | None = None,
) -> tuple[Path, list[str]]:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = backup_root / f"{BACKUP_PREFIX}{timestamp}"
    actions: list[str] = []
    artifacts: list[dict[str, str]] = []
    used: set[str] = set()

    for src in paths:
        if not src.exists():
            actions.append(f"Pular (não existe): {src}")
            continue
        name = backup_entry_name(src, used)
        used.add(name)
        dest = backup_dir / name
        actions.append(f"Backup: {src} -> {dest}")
        artifacts.append(
            {
                "name": name,
                "source": str(src),
                "kind": "file" if src.is_file() else "dir",
            }
        )
        if not dry_run:
            backup_dir.mkdir(parents=True, exist_ok=True)
            if src.is_file():
                shutil.copy2(src, dest)
            else:
                dest.mkdir(parents=True, exist_ok=True)
                shutil.copytree(src, dest, dirs_exist_ok=True)

    if not artifacts:
        return backup_dir, actions

    actions.append(f"Gravar manifest.json ({len(artifacts)} itens)")
    if not dry_run:
        manifest = {
            "timestamp": timestamp,
            "cursor_user_data": str(CURSOR_USER_DATA),
            "cursor_home_projects": str(CURSOR_HOME_PROJECTS),
            **(meta or {}),
            "artifacts": artifacts,
        }
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    return backup_dir, actions


def list_backups(backup_root: Path) -> list[Path]:
    if not backup_root.is_dir():
        return []
    return sorted(
        (
            entry
            for entry in backup_root.iterdir()
            if entry.is_dir() and entry.name.startswith(BACKUP_PREFIX)
        ),
        reverse=True,
    )


def load_backup_manifest(backup_dir: Path) -> dict[str, Any] | None:
    manifest = backup_dir / "manifest.json"
    if not manifest.is_file():
        return None
    try:
        return json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def infer_backup_artifacts(backup_dir: Path) -> list[dict[str, str]]:
    """Reconstrói a lista de itens de backups antigos, sem manifest.json."""
    global_storage = CURSOR_USER_DATA / "globalStorage"
    workspace_storage = CURSOR_USER_DATA / "workspaceStorage"
    artifacts: list[dict[str, str]] = []

    for entry in sorted(backup_dir.iterdir()):
        if entry.is_file() and entry.name in ("state.vscdb", "storage.json"):
            source = global_storage / entry.name
            artifacts.append(
                {"name": entry.name, "source": str(source), "kind": "file"}
            )
        elif entry.is_dir() and re.fullmatch(r"[0-9a-f]{32}", entry.name):
            source = workspace_storage / entry.name
            artifacts.append({"name": entry.name, "source": str(source), "kind": "dir"})

    return artifacts


def is_managed_path(path: Path) -> bool:
    """Restauração só pode tocar pastas de dados do Cursor."""
    target = norm_path(str(path))
    for root in (CURSOR_USER_DATA, CURSOR_HOME_PROJECTS):
        root_norm = norm_path(str(root))
        if target == root_norm or target.startswith(root_norm + os.sep):
            return True
    return False


def select_backup(backups: list[Path], selection: str) -> Path | None:
    if selection:
        for backup in backups:
            if backup.name == selection or backup.name.endswith(selection):
                return backup
        print(f"ERRO: backup não encontrado: {selection}")
        return None

    for index, backup in enumerate(backups, start=1):
        print(f"  {index}) {backup.name}")
    print()

    if not sys.stdin.isatty():
        print("ERRO: sem terminal interativo. Use --revert <nome-ou-timestamp>.")
        return None

    answer = input("Número do backup a restaurar (Enter cancela): ").strip()
    if not answer.isdigit() or not 1 <= int(answer) <= len(backups):
        print("Cancelado.")
        return None
    return backups[int(answer) - 1]


def run_revert(
    execute: bool,
    selection: str,
    quit_flag: bool = False,
    force: bool = False,
) -> int:
    dry_run = not execute
    backup_root = CURSOR_USER_DATA / "backups"

    print("=" * 60)
    print("Restauração de backup do Cursor")
    print("=" * 60)
    print(f"Modo: {'EXECUÇÃO' if execute else 'DRY-RUN (simulação)'}")
    print(f"Backups em: {backup_root}")
    print()

    backups = list_backups(backup_root)
    if not backups:
        print("ERRO: nenhum backup encontrado.")
        return 1

    backup_dir = select_backup(backups, selection)
    if backup_dir is None:
        return 1

    if not ensure_cursor_closed(execute, quit_flag, force):
        return 1

    manifest = load_backup_manifest(backup_dir)
    if manifest:
        artifacts = manifest.get("artifacts", [])
        print(f"\nBackup: {backup_dir.name} (modo {manifest.get('mode', '?')})")
        print(f"  Origem:  {manifest.get('from', '?')}")
        print(f"  Destino: {manifest.get('to', '?')}")
    else:
        artifacts = infer_backup_artifacts(backup_dir)
        print(f"\nBackup: {backup_dir.name} (sem manifest.json — restauração parcial)")

    if not artifacts:
        print("ERRO: nada restaurável neste backup.")
        return 1

    print()
    restored = 0
    for artifact in artifacts:
        src = backup_dir / str(artifact.get("name", ""))
        dest = Path(str(artifact.get("source", "")))
        if not src.exists():
            print(f"  - Pular (ausente no backup): {artifact.get('name', '?')}")
            continue
        if not is_managed_path(dest):
            print(f"  - Pular (fora das pastas do Cursor): {dest}")
            continue

        print(f"  - Restaurar: {src.name} -> {dest}")
        restored += 1
        if dry_run:
            continue

        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.is_file():
            shutil.copy2(src, dest)
        else:
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(src, dest)

    print()
    if dry_run:
        print(f"Simulação concluída ({restored} itens).")
        print("Rode com --execute para restaurar de fato.")
    else:
        print(f"Restauração concluída ({restored} itens).")
    print("Nenhuma pasta de projeto foi tocada.")

    return 0


def print_project_folders_intact(
    old_project_path: str,
    new_project_path: str,
    dry_run: bool,
) -> None:
    verb = "não seriam alteradas" if dry_run else "não foram alteradas"
    print(f"\nAs pastas do projeto no disco {verb}:")
    print(f"  - Origem (intacta):  {old_project_path}")
    print(f"  - Destino (intacto): {new_project_path}")
    print("Só metadados do Cursor entram na migração.")
    if not dry_run:
        print(
            "Depois de conferir os chats no caminho novo, você pode apagar "
            "a pasta antiga se quiser."
        )


def run_migration(
    execute: bool,
    skip_disk_kv: bool = True,
    old_path: str | None = None,
    new_path: str | None = None,
    repair: bool = False,
    quit_flag: bool = False,
    force: bool = False,
) -> int:
    dry_run = not execute
    old_project_path, new_project_path = _project_paths(old_path, new_path)

    print("=" * 60)
    print("Reparo de chats Cursor" if repair else "Migração de chats Cursor")
    print("=" * 60)
    print(f"Modo: {'EXECUÇÃO' if execute else 'DRY-RUN (simulação)'}")
    print(f"Antigo: {old_project_path}")
    print(f"Novo:   {new_project_path}")
    print()

    if not CURSOR_USER_DATA.is_dir():
        print(f"ERRO: Cursor User não encontrado em {CURSOR_USER_DATA}")
        return 1

    if not Path(new_project_path).is_dir():
        print(f"ERRO: Pasta nova não existe: {new_project_path}")
        return 1

    if not ensure_cursor_closed(execute, quit_flag, force):
        return 1

    workspace_storage = CURSOR_USER_DATA / "workspaceStorage"
    global_db = CURSOR_USER_DATA / "globalStorage" / "state.vscdb"
    storage_json = CURSOR_USER_DATA / "globalStorage" / "storage.json"
    if global_db.is_file():
        db_size_gb = global_db.stat().st_size / (1024**3)
        if db_size_gb >= 1:
            print(
                f"AVISO: globalStorage/state.vscdb = {db_size_gb:.1f} GB "
                f"(banco inchado; etapa cursorDiskKV é opcional e muito lenta)."
            )
            print()

    old_ws_id = find_workspace_id(old_project_path, workspace_storage)
    new_ws_id = find_active_workspace_id(new_project_path, workspace_storage)

    print(f"Workspace ID antigo: {old_ws_id or 'NÃO ENCONTRADO'}")
    print(f"Workspace ID novo:   {new_ws_id or 'NÃO ENCONTRADO'}")
    print()

    if not old_ws_id and not repair:
        print(
            "ERRO: Não achei workspaceStorage do caminho antigo.\n"
            "  O workspace.json ainda precisa existir em AppData apontando para OLD_PROJECT_PATH.\n"
            "  Se a pasta antiga já sumiu, rode com --repair."
        )
        return 1

    if not new_ws_id:
        print(
            "ERRO: Não achei workspace do caminho novo.\n"
            "  Abra o projeto NOVO no Cursor uma vez (File -> Open Folder) e rode de novo."
        )
        return 1

    if old_ws_id == new_ws_id and not repair:
        print(
            "AVISO: IDs antigo e novo são iguais — nada a migrar no workspaceStorage."
        )
        return 0

    target_ws_ids = mirror_workspace_ids(new_project_path, workspace_storage, new_ws_id)
    if len(target_ws_ids) > 1:
        print(f"Workspaces do caminho novo: {', '.join(target_ws_ids)}")
        print()

    can_copy_workspace = bool(old_ws_id) and old_ws_id != new_ws_id
    old_ws_dir = workspace_storage / old_ws_id if old_ws_id else None
    if repair and not can_copy_workspace:
        print(
            "AVISO: sem workspace antigo distinto para copiar — "
            "reparo segue só relinkando metadados."
        )

    # Fonte atual da UI (Cursor recente): tabela composerHeaders.workspaceId
    # Funciona para QUALQUER projeto — só depende dos workspace IDs dos paths.
    gate_on = table_gate_enabled(global_db)
    table_chats_old = count_composer_headers_table(global_db, old_ws_id or "")
    table_chats_new = count_composer_headers_table(global_db, new_ws_id)
    print(
        f"Tabela composerHeaders: {table_chats_old} chat(s) no workspace antigo, "
        f"{table_chats_new} no novo "
        f"(tableGate={'ON' if gate_on else 'OFF'})"
    )

    # JSON legado ItemTable (ainda existe; tableGate=true faz a UI preferir a tabela)
    headers_data = load_composer_headers(global_db)
    composers = headers_data.get("allComposers", [])
    if not isinstance(composers, list):
        composers = []

    candidates = max(
        table_chats_old,
        count_composers(composers, old_ws_id or "", old_project_path),
    )

    patched_composers = 0
    for c in composers:
        if not isinstance(c, dict):
            continue
        if patch_workspace_identifier(
            c, old_project_path, new_project_path, old_ws_id or "", new_ws_id
        ):
            patched_composers += 1

    print(f"JSON legado composer.composerHeaders: {patched_composers} chat(s)")

    # Fallback: IDs no workspace local + blobs composerData:* no banco global
    created_headers, updated_blobs, composers, migrated_ids = (
        rebuild_composer_headers_from_workspace(
            global_db,
            old_ws_dir,
            old_project_path,
            new_project_path,
            old_ws_id or "",
            new_ws_id,
            composers,
            dry_run,
        )
    )
    headers_data["allComposers"] = composers
    if created_headers or updated_blobs or migrated_ids:
        print(
            f"Índice reconstruído a partir do workspace: "
            f"{len(migrated_ids)} chats "
            f"(+{created_headers} novos, {updated_blobs} composerData)"
        )
        candidates = max(candidates, len(migrated_ids))
        patched_composers = max(patched_composers, len(migrated_ids))
    elif patched_composers == 0 and table_chats_old == 0 and old_ws_dir is not None:
        local_ids = collect_composer_ids_from_workspace(old_ws_dir)
        print(
            f"AVISO: {len(local_ids)} IDs no workspace antigo, mas nenhum "
            f"composerData correspondente no banco global."
        )

    # IDs da tabela (fonte real) para sincronizar no workspace destino
    table_ids = list_composer_ids_from_headers_table(global_db, old_ws_id or "")
    if table_ids:
        seen_ids = set(table_ids)
        migrated_ids = table_ids + [cid for cid in migrated_ids if cid not in seen_ids]

    transcripts_dirs = [
        CURSOR_HOME_PROJECTS / cursor_projects_slug(path) / "agent-transcripts"
        for path in (old_project_path, new_project_path)
    ]
    target_ws_dirs = [workspace_storage / ws_id for ws_id in target_ws_ids]
    backup_targets = [
        path
        for path in (
            old_ws_dir,
            *target_ws_dirs,
            global_db,
            storage_json,
            *transcripts_dirs,
        )
        if path is not None
    ]
    backup_parent = CURSOR_USER_DATA / "backups"
    backup_dir, backup_actions = create_backup(
        backup_parent,
        backup_targets,
        dry_run,
        meta={
            "mode": "repair" if repair else "migrate",
            "from": old_project_path,
            "to": new_project_path,
            "old_workspace_id": old_ws_id or "",
            "new_workspace_id": new_ws_id,
        },
    )

    print(f"\nBackup em: {backup_dir}")
    for line in backup_actions:
        print(f"  - {line}")

    print("\n--- workspaceStorage ---")
    if can_copy_workspace and old_ws_dir is not None:
        for target_dir in target_ws_dirs:
            if target_dir == old_ws_dir:
                continue
            print(f"  Destino {target_dir.name}:")
            for line in copy_workspace_files(
                old_ws_dir, target_dir, new_project_path, dry_run
            ):
                print(f"    - {line}")
            for line in sync_workspace_composer_data(
                target_dir, migrated_ids, dry_run
            ):
                print(f"    - {line}")
    else:
        print("  - Pulado (sem workspace antigo distinto para copiar)")
        # Mesmo sem cópia completa, sincroniza a lista local do destino.
        for target_dir in target_ws_dirs:
            for line in sync_workspace_composer_data(
                target_dir, migrated_ids, dry_run
            ):
                print(f"  - {line}")

    print("\n--- agent-transcripts ---")
    for line in migrate_agent_transcripts(old_project_path, new_project_path, dry_run):
        print(f"  - {line}")

    print("\n--- globalStorage (tabela composerHeaders) ---")
    table_moved, table_values = migrate_composer_headers_table(
        global_db,
        old_ws_id or "",
        new_ws_id,
        old_project_path,
        new_project_path,
        dry_run,
    )
    if table_moved:
        verb = "Relinkaria" if dry_run else "Relinkado"
        print(
            f"  - {verb} {table_moved} chat(s) "
            f"({table_values} values com path atualizado)"
        )
    else:
        print("  - Nenhum chat para relinkar nesta tabela")

    glass_updated = patch_glass_local_agent_projects(
        global_db,
        old_project_path,
        new_project_path,
        old_ws_id or "",
        new_ws_id,
        dry_run,
    )
    verb = "Atualizaria" if dry_run else "Atualizado"
    print(f"  - {verb} {glass_updated} projeto(s) em glass.localAgentProjects")

    print("\n--- globalStorage (composer.composerHeaders JSON legado) ---")
    if dry_run:
        print(f"  - Atualizaria {patched_composers} conversas")
    else:
        save_composer_headers(global_db, headers_data)
        print(f"  - Atualizado {patched_composers} conversas")

    print("\n--- globalStorage (ItemTable) ---")
    item_updated = patch_global_item_table(
        global_db,
        old_project_path,
        new_project_path,
        old_ws_id or "",
        new_ws_id,
        dry_run,
    )
    verb = "Atualizaria" if dry_run else "Atualizado"
    print(f"  - {verb} {item_updated} registros com paths/IDs antigos")

    print("\n--- globalStorage (storage.json) ---")
    for line in patch_storage_json(
        storage_json,
        old_project_path,
        new_project_path,
        old_ws_id or "",
        new_ws_id,
        dry_run,
    ):
        print(f"  - {line}")

    if skip_disk_kv:
        print("\n--- globalStorage (cursorDiskKV) ---")
        print(
            "  - Varredura completa pulada (padrão). "
            "composerData dos chats do workspace já é atualizado de forma pontual."
        )
        print("  - Use --patch-disk-kv só se ainda faltar algum chat.")
    else:
        print("\n--- globalStorage (cursorDiskKV, paths antigos) ---")
        print(
            "  - Varredura profunda (--patch-disk-kv). Pode levar horas se state.vscdb > 1 GB."
        )
        scanned, kv_updated = patch_global_disk_kv(
            global_db,
            old_project_path,
            new_project_path,
            old_ws_id or "",
            new_ws_id,
            dry_run,
        )
        print(f"  - Total no cursorDiskKV: {scanned}")
        print(f"  - Registros com path antigo (atualizados): {kv_updated}")

    if not dry_run:
        remapped_table = count_composer_headers_table(global_db, new_ws_id)
        remaining_old = count_composer_headers_table(global_db, old_ws_id or "")
        remaining = load_composer_headers(global_db).get("allComposers", [])
        remapped_json = count_composers(
            remaining if isinstance(remaining, list) else [],
            new_ws_id,
            new_project_path,
        )
        remapped = max(remapped_table, remapped_json)
        print(
            f"\nVerificação: tabela={remapped_table} | JSON legado={remapped_json} "
            f"apontam para {new_ws_id}"
        )
        if remaining_old:
            print(f"AVISO: ainda restam {remaining_old} chat(s) no workspace antigo")
        if candidates > 0 and remapped == 0:
            print(
                f"ERRO: {candidates} conversas continuam no workspace antigo.\n"
                "  Feche o Cursor completamente e rode de novo com --repair.\n"
                f"  Backup disponível em: {backup_dir}"
            )
            return 1

    print()
    if dry_run:
        print("Simulação concluída. Rode com --execute para aplicar.")
    else:
        print("Reparo concluído." if repair else "Migração concluída.")
        print(f"Abra no Cursor: {new_project_path}")
        print("Os chats antigos devem aparecer no histórico do projeto novo.")

    print_project_folders_intact(old_project_path, new_project_path, dry_run)

    return 0


def run_junction(
    execute: bool,
    old_path: str | None = None,
    new_path: str | None = None,
    quit_flag: bool = False,
    force: bool = False,
) -> int:
    dry_run = not execute
    link, target = _project_paths(old_path, new_path)

    print("Modo JUNCTION (atalho do caminho antigo -> pasta nova)")
    print(f"Link:   {link}")
    print(f"Alvo:   {target}")

    if Path(link).exists():
        print(f"ERRO: Já existe: {link}")
        return 1
    if not Path(target).is_dir():
        print(f"ERRO: Pasta alvo não existe: {target}")
        return 1
    if not ensure_cursor_closed(execute, quit_flag, force):
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
        description=(
            "Migra histórico de chats do Cursor após renomear pasta do projeto. "
            "As pastas do projeto no disco nunca são movidas ou apagadas."
        )
    )
    parser.add_argument(
        "--from",
        "-f",
        dest="old_path",
        default=None,
        metavar="CAMINHO",
        help="Caminho antigo do projeto (sobrepõe OLD_PROJECT_PATH do .env).",
    )
    parser.add_argument(
        "--to",
        "-t",
        dest="new_path",
        default=None,
        metavar="CAMINHO",
        help="Caminho novo do projeto (sobrepõe NEW_PROJECT_PATH do .env).",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Aplica alterações (sem isso, só simula).",
    )
    parser.add_argument(
        "--repair",
        action="store_true",
        help=(
            "Relinka chats quando a pasta antiga já foi renomeada/apagada "
            "(não exige workspace antigo em AppData)."
        ),
    )
    parser.add_argument(
        "--revert",
        nargs="?",
        const="",
        default=None,
        metavar="BACKUP",
        help=(
            "Restaura um backup anterior. Sem valor, lista e pergunta; "
            "ou informe o nome/timestamp do backup."
        ),
    )
    parser.add_argument(
        "--junction",
        action="store_true",
        help="Cria junction do caminho antigo -> novo (método simples, sem mexer no DB).",
    )
    parser.add_argument(
        "--quit-cursor",
        action="store_true",
        help="Encerra o Cursor automaticamente antes de aplicar alterações.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Segue mesmo com o Cursor aberto (o Cursor pode desfazer as alterações).",
    )
    parser.add_argument(
        "--patch-disk-kv",
        action="store_true",
        help=(
            "Varredura lenta em cursorDiskKV (pode levar horas com state.vscdb grande). "
            "Normalmente desnecessário — a UI usa a tabela composerHeaders."
        ),
    )
    parser.add_argument(
        "--skip-disk-kv",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    if args.revert is not None:
        return run_revert(args.execute, args.revert, args.quit_cursor, args.force)
    if args.junction:
        return run_junction(
            args.execute,
            args.old_path,
            args.new_path,
            args.quit_cursor,
            args.force,
        )
    skip_disk_kv = not args.patch_disk_kv or args.skip_disk_kv
    return run_migration(
        args.execute,
        skip_disk_kv=skip_disk_kv,
        old_path=args.old_path,
        new_path=args.new_path,
        repair=args.repair,
        quit_flag=args.quit_cursor,
        force=args.force,
    )


if __name__ == "__main__":
    raise SystemExit(main())
