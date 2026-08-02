# Migrar chats do Cursor após renomear pasta

Script: `migrate-cursor-chats.py`

## Antes de rodar

1. **Feche o Cursor** (todas as janelas).
2. Abra o projeto **novo** no Cursor **pelo menos uma vez** (para criar o workspace em AppData).
3. Edite no topo do script:

```python
OLD_PROJECT_PATH = r"F:\Projetos\meu-projeto-antigo"
NEW_PROJECT_PATH = r"F:\Projetos\meu-projeto-novo"
```

## Comandos

Na pasta do script:

```bash
cd F:\Projetos\meu-projeto-novo\.cursor\scripts

# Simulação (não altera nada)
python migrate-cursor-chats.py

# Aplicar migração
python migrate-cursor-chats.py --execute
```

## Modo junction (alternativa simples)

Não mexe no banco. Cria atalho do caminho antigo apontando para a pasta nova:

```bash
python migrate-cursor-chats.py --junction
python migrate-cursor-chats.py --junction --execute
```

Requer terminal **como Administrador**. Depois abra no Cursor o caminho **antigo** (`OLD_PROJECT_PATH`).

## O que o script faz (--execute)

1. Backup em `%APPDATA%\Cursor\User\backups\cursor-chat-migration-YYYYMMDD-HHMMSS\`
2. Copia `workspaceStorage` antigo → novo
3. Atualiza `workspace.json` do workspace novo
4. Relinka chats em `globalStorage\state.vscdb`
5. Atualiza referências de path em `cursorDiskKV`
6. Mescla `agent-transcripts` em `~\.cursor\projects\`

## Caminhos completos (referência)

| Item | Caminho |
|------|---------|
| Workspace storage | `C:\Users\SeuUsuario\AppData\Roaming\Cursor\User\workspaceStorage\` |
| Global DB | `C:\Users\SeuUsuario\AppData\Roaming\Cursor\User\globalStorage\state.vscdb` |
| Transcripts | `C:\Users\SeuUsuario\.cursor\projects\f-Projetos-...\agent-transcripts\` |

## Depois da migração

Abra: `NEW_PROJECT_PATH` no Cursor.
