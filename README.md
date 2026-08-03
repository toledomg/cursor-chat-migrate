# cursor-chat-migrate

CLI em Python para recuperar o histórico de chats do [Cursor](https://cursor.com) depois de renomear ou mover a pasta de um projeto.

Quando o caminho do workspace muda, o Cursor trata o projeto como novo e os chats somem da UI. Esta ferramenta **relinka os metadados** do workspace antigo para o novo — sem mover, renomear ou apagar pastas do projeto.

![Python](https://img.shields.io/badge/python-3.10%2B-blue?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey?logo=windows)
![License](https://img.shields.io/badge/license-MIT-green)

## Features

- Relinka **todos** os chats do workspace (`composerHeaders`, JSON legado e glass)
- Dry-run por padrão — nada é gravado sem `--execute`
- Backup automático com `--revert`
- Modo `--repair` quando a pasta antiga já não existe
- Alternativa `--junction` (Windows) sem alterar o SQLite
- Paths via flags, `.env` ou prompt interativo

## Prerequisites

| | |
|---|---|
| OS | Windows |
| Python | 3.10+ |
| Cursor | Abra o **caminho novo** ao menos uma vez (`File → Open Folder`) |
| Admin | Só necessário para `--junction` |

Feche o Cursor antes de aplicar alterações (ou use `--quit-cursor`).

## Install

```bash
git clone https://github.com/toledomg/cursor-chat-migrate.git
cd cursor-chat-migrate

python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Quick start

```bash
# 1. Simula (dry-run)
python migrate-cursor-chats.py --from "F:\Projetos\antigo" --to "F:\Projetos\novo"

# 2. Aplica
python migrate-cursor-chats.py --execute --from "F:\Projetos\antigo" --to "F:\Projetos\novo"
```

Sem `--from` / `--to`, o script usa o `.env` ou pergunta no terminal.

Abra o projeto no caminho novo e confira os chats. Só então apague a pasta antiga, se quiser — o script **não** a remove.

## Usage

### Migração (padrão)

Relinka metadados em AppData / `~/.cursor`. Não mexe nas pastas do projeto.

```bash
python migrate-cursor-chats.py                          # dry-run
python migrate-cursor-chats.py --execute                # aplica
python migrate-cursor-chats.py --quit-cursor --execute  # fecha o Cursor e aplica
```

### Repair

Use quando a pasta antiga já foi renomeada ou apagada:

```bash
python migrate-cursor-chats.py --repair --from "F:\caminho\antigo" --to "F:\caminho\novo"
python migrate-cursor-chats.py --repair --execute --from "..." --to "..."
```

### Revert

Restaura um backup criado pelo script:

```bash
python migrate-cursor-chats.py --revert                  # lista / pergunta
python migrate-cursor-chats.py --revert 20260802-120000  # por timestamp
python migrate-cursor-chats.py --revert --execute
```

### Junction

Cria um junction Windows (`caminho antigo → pasta nova`) sem alterar o SQLite. Requer terminal como Administrador.

```bash
python migrate-cursor-chats.py --junction
python migrate-cursor-chats.py --junction --execute
```

| | Metadados | Junction |
|---|:---:|:---:|
| Altera SQLite | Sim | Não |
| Move pastas do projeto | Não | Não |
| Requer Admin | Não | Sim |
| Abrir projeto por | Caminho **novo** | Caminho **antigo** |

## Configuration

Prioridade: `--from` / `--to` → variáveis no `.env` → prompt interativo.

```bash
cp .env.example .env
```

```env
OLD_PROJECT_PATH=F:\Projetos\meu-projeto-antigo
NEW_PROJECT_PATH=F:\Projetos\meu-projeto-novo
```

| Variável | Descrição |
|---|---|
| `OLD_PROJECT_PATH` | Caminho absoluto antes da renomeação |
| `NEW_PROJECT_PATH` | Caminho absoluto depois da renomeação |
| `CURSOR_USER_DATA` | Pasta `User` do Cursor (opcional) |
| `CURSOR_HOME_PROJECTS` | Pasta `~/.cursor/projects` (opcional) |

O `.env` está no `.gitignore` e não deve ser commitado.

## CLI

```
migrate-cursor-chats.py [-h] [--from CAMINHO] [--to CAMINHO]
                        [--execute] [--repair] [--revert [BACKUP]]
                        [--junction] [--quit-cursor] [--force]
                        [--patch-disk-kv]
```

| Flag | Descrição |
|---|---|
| *(sem flags)* | Dry-run — simula sem gravar |
| `--from`, `-f` | Caminho antigo |
| `--to`, `-t` | Caminho novo |
| `--execute` | Aplica alterações |
| `--repair` | Relinka quando a pasta antiga já sumiu |
| `--revert [BACKUP]` | Restaura backup (lista se omitir o nome) |
| `--junction` | Junction Windows em vez da migração SQLite |
| `--quit-cursor` | Encerra o Cursor antes de gravar |
| `--force` | Segue com Cursor aberto (risco de desfazer) |
| `--patch-disk-kv` | Varredura lenta em `cursorDiskKV` (off por padrão) |

## What gets changed

Com `--execute`, a migração de metadados:

1. Cria backup + `manifest.json` em `%APPDATA%\Cursor\User\backups\cursor-chat-migration-YYYYMMDD-HHMMSS\`
2. Copia dados do workspace antigo → novo e atualiza `workspace.json`
3. Relinka `composerHeaders` em `state.vscdb`, atualiza JSON legado / glass e faz patch leve na `ItemTable`
4. Atualiza referências do Open Recent em `storage.json`
5. Mescla `agent-transcripts` em `~/.cursor/projects/<slug>/`

**Não altera** arquivos dentro de `OLD_PROJECT_PATH` nem `NEW_PROJECT_PATH`.

| Item | Caminho típico |
|---|---|
| Workspace storage | `%APPDATA%\Cursor\User\workspaceStorage\` |
| Banco global | `%APPDATA%\Cursor\User\globalStorage\state.vscdb` |
| Open Recent | `%APPDATA%\Cursor\User\globalStorage\storage.json` |
| Agent transcripts | `%USERPROFILE%\.cursor\projects\<slug>\agent-transcripts\` |

## Troubleshooting

<details>
<summary>Workspace ID antigo não encontrado</summary>

O AppData ainda precisa ter `workspace.json` apontando para o path antigo. Se a pasta já sumiu do disco, use `--repair`. Se o registro em AppData também foi apagado, restaure um backup ou use `--junction`.
</details>

<details>
<summary>Workspace ID novo não encontrado</summary>

Abra o projeto em `NEW_PROJECT_PATH` no Cursor uma vez e rode de novo.
</details>

<details>
<summary>Cursor.exe está em execução</summary>

Feche o Cursor, use `--quit-cursor`, ou (com risco) `--force`.
</details>

<details>
<summary>Chats não aparecem após --execute</summary>

```bash
python migrate-cursor-chats.py --repair --execute --from "..." --to "..."
```

Se precisar, restaure com `--revert`.
</details>

<details>
<summary>state.vscdb muito grande / demora</summary>

Por padrão **não** se varre `cursorDiskKV`. Use `--patch-disk-kv` só se algum chat ainda faltar após o fluxo normal (pode levar horas com bancos > 1 GB).
</details>

<details>
<summary>Erro ao criar junction</summary>

Terminal como **Administrador**; `OLD_PROJECT_PATH` não deve existir no disco.
</details>

## Notes

- Sempre rode o dry-run antes de `--execute`.
- Backup automático com `manifest.json` é criado antes de gravar.
- Projeto testado em Windows. Não afiliado ao Cursor / Anysphere.

## License

[MIT](LICENSE)
