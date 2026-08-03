# cursor-chat-migrate

Ferramenta CLI em Python para migrar o histórico de chats do [Cursor](https://cursor.com) após renomear ou mover a pasta de **qualquer** projeto.

Repositório: [github.com/toledomg/cursor-chat-migrate](https://github.com/toledomg/cursor-chat-migrate)

Quando você renomeia um repositório ou altera o caminho no disco, o Cursor passa a tratá-lo como um workspace novo — e os chats antigos deixam de aparecer. Este script relinka **todos** os chats daquele workspace (tabela `composerHeaders` + JSON legado + glass) para o caminho novo. **Só metadados do Cursor** são alterados; as pastas do projeto no disco nunca são movidas ou apagadas.

![Python](https://img.shields.io/badge/python-3.10%2B-blue?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey?logo=windows)
![License](https://img.shields.io/badge/license-MIT-green)

---

## Índice

- [Pré-requisitos](#pré-requisitos)
- [Instalação](#instalação)
- [Configuração](#configuração)
- [Uso rápido](#uso-rápido)
- [Modos de operação](#modos-de-operação)
- [Referência da CLI](#referência-da-cli)
- [O que o script altera](#o-que-o-script-altera)
- [Estrutura do projeto](#estrutura-do-projeto)
- [Solução de problemas](#solução-de-problemas)
- [Avisos importantes](#avisos-importantes)

---

## Pré-requisitos

| Requisito | Detalhe |
|-----------|---------|
| **Sistema operacional** | Windows (usa `APPDATA`, junctions e `tasklist`) |
| **Python** | 3.10 ou superior |
| **Cursor** | Instalado; abra o **caminho novo** pelo menos uma vez |
| **Permissões** | Modo junction exige terminal como Administrador |

> **Antes de migrar:** abra o projeto no **caminho novo** no Cursor (`File → Open Folder`). Isso cria o workspace em `%APPDATA%\Cursor\User\workspaceStorage\`.

---

## Instalação

```bash
git clone https://github.com/toledomg/cursor-chat-migrate.git
cd cursor-chat-migrate

python -m venv .venv
.venv\Scripts\activate        # Windows (PowerShell/CMD)

pip install -r requirements.txt
```

---

## Configuração

Ordem de prioridade dos caminhos:

1. Flags `--from` / `--to`
2. Variáveis no `.env`
3. **Pergunta interativa** no terminal (se faltar algum)

`.env` opcional — útil só se for repetir a mesma migração:

```bash
cp .env.example .env
```

```env
OLD_PROJECT_PATH=F:\Projetos\meu-projeto-antigo
NEW_PROJECT_PATH=F:\Projetos\meu-projeto-novo
```

| Variável | Obrigatória | Descrição |
|----------|:-----------:|-----------|
| `OLD_PROJECT_PATH` | Não* | Caminho absoluto **antes** da renomeação |
| `NEW_PROJECT_PATH` | Não* | Caminho absoluto **depois** da renomeação |
| `CURSOR_USER_DATA` | Não | Pasta `User` do Cursor |
| `CURSOR_HOME_PROJECTS` | Não | Pasta `~/.cursor/projects` |

\* Se não estiver no `.env` nem na CLI, o script pergunta. Depois oferece salvar no `.env`.

> O arquivo `.env` está no `.gitignore` e **não deve** ser commitado.

---

## Uso rápido

1. **Feche o Cursor** (ou use `--quit-cursor`).
2. Rode:

```bash
python migrate-cursor-chats.py
```

Se o `.env` estiver vazio, o script pergunta:

```
Pasta ORIGEM (caminho antigo do projeto): F:\Projetos\antigo
Pasta DESTINO (caminho novo do projeto): F:\Projetos\novo
Salvar esses caminhos no .env para a próxima vez? [s/N]:
```

3. Confira o dry-run e aplique:

```bash
python migrate-cursor-chats.py --execute
```

4. Abra o projeto no **caminho novo**. Confira os chats.
5. Se estiver ok, você **pode apagar a pasta antiga** — o script não a remove.

---

## Modos de operação

### Migração de metadados (padrão)

Relinka chats no AppData / `~/.cursor`. **Não mexe nas pastas do projeto.**

```bash
python migrate-cursor-chats.py              # dry-run
python migrate-cursor-chats.py --execute    # aplica
```

### Repair

Quando a pasta antiga já foi renomeada/apagada, mas o workspace antigo ainda existe em AppData (ou só precisa relinkar composers):

```bash
python migrate-cursor-chats.py --repair --from "F:\caminho\antigo" --to "F:\caminho\novo"
python migrate-cursor-chats.py --repair --execute --from "..." --to "..."
```

### Revert

Restaura um backup criado pelo script (`manifest.json`):

```bash
python migrate-cursor-chats.py --revert                  # lista / pergunta
python migrate-cursor-chats.py --revert 20260802-120000  # por timestamp
python migrate-cursor-chats.py --revert --execute
```

### Junction (alternativa)

Cria junction Windows: caminho antigo → pasta nova (sem alterar SQLite).

```bash
python migrate-cursor-chats.py --junction
python migrate-cursor-chats.py --junction --execute
```

| Aspecto | Metadados | Junction |
|---------|:---------:|:--------:|
| Altera banco SQLite | Sim | Não |
| Move pastas do projeto | **Não** | Não |
| Requer Admin | Não | Sim |
| Abrir projeto por | Caminho **novo** | Caminho **antigo** |

---

## Referência da CLI

```
usage: migrate-cursor-chats.py [-h] [--from CAMINHO] [--to CAMINHO]
                               [--execute] [--repair] [--revert [BACKUP]]
                               [--junction] [--quit-cursor] [--force]
                               [--patch-disk-kv]
```

| Flag | Descrição |
|------|-----------|
| *(sem flags)* | **Dry-run** — simula sem gravar |
| `--from` / `-f` | Caminho antigo (sobrepõe `.env`) |
| `--to` / `-t` | Caminho novo (sobrepõe `.env`) |
| `--execute` | Aplica alterações |
| `--repair` | Relinka quando a pasta antiga já sumiu |
| `--revert [BACKUP]` | Restaura backup (lista se omitir o nome) |
| `--junction` | Junction Windows em vez da migração SQLite |
| `--quit-cursor` | Encerra o Cursor antes de gravar |
| `--force` | Segue com Cursor aberto (risco de desfazer) |
| `--patch-disk-kv` | Varredura lenta em `cursorDiskKV` (opcional; off por padrão) |

---

## O que o script altera

Com `--execute`, a migração de metadados faz:

```
1. Backup + manifest.json
   %APPDATA%\Cursor\User\backups\cursor-chat-migration-YYYYMMDD-HHMMSS\

2. workspaceStorage
   Copia dados do workspace antigo → novo (e espelhos se existirem)
   Atualiza workspace.json

3. globalStorage / state.vscdb
   Relinka tabela composerHeaders (fonte da UI, qualquer projeto)
   Atualiza JSON legado + glass.localAgentProjects
   Patch leve na ItemTable (paths/IDs)
   (cursorDiskKV só com --patch-disk-kv)

4. globalStorage / storage.json
   Atualiza referências do "Open Recent"

5. agent-transcripts
   Mescla ~/.cursor/projects/<slug>/agent-transcripts
```

**Não altera** arquivos dentro de `OLD_PROJECT_PATH` nem `NEW_PROJECT_PATH`.

### Caminhos relevantes no Windows

| Item | Caminho típico |
|------|----------------|
| Workspace storage | `%APPDATA%\Cursor\User\workspaceStorage\` |
| Banco global | `%APPDATA%\Cursor\User\globalStorage\state.vscdb` |
| Open Recent | `%APPDATA%\Cursor\User\globalStorage\storage.json` |
| Agent transcripts | `%USERPROFILE%\.cursor\projects\<slug>\agent-transcripts\` |

---

## Estrutura do projeto

```
cursor-chat-migrate/
├── migrate-cursor-chats.py
├── requirements.txt
├── .env.example
├── .env              # local (não versionado)
├── .gitignore
├── LICENSE
└── README.md
```

---

## Solução de problemas

<details>
<summary><strong>Workspace ID antigo não encontrado</strong></summary>

O AppData ainda precisa ter `workspace.json` apontando para o path antigo. Se a pasta já sumiu do disco, use `--repair`. Se o registro em AppData também foi apagado, restaure um backup ou use `--junction` (se o path antigo puder voltar como link).
</details>

<details>
<summary><strong>Workspace ID novo não encontrado</strong></summary>

Abra o projeto em `NEW_PROJECT_PATH` no Cursor uma vez e rode de novo.
</details>

<details>
<summary><strong>Cursor.exe está em execução</strong></summary>

Feche o Cursor, use `--quit-cursor`, ou (com risco) `--force`.
</details>

<details>
<summary><strong>Chats não aparecem após --execute</strong></summary>

Feche o Cursor e rode:

```bash
python migrate-cursor-chats.py --repair --execute --from "..." --to "..."
```

Se precisar, restaure com `--revert`.
</details>

<details>
<summary><strong>state.vscdb muito grande / demora</strong></summary>

Por padrão **não** se varre `cursorDiskKV`. Use `--patch-disk-kv` só se algum chat ainda faltar após o fluxo normal (pode levar horas com bancos > 1 GB).
</details>

<details>
<summary><strong>Erro ao criar junction</strong></summary>

Terminal como **Administrador**; `OLD_PROJECT_PATH` não deve existir no disco.
</details>

---

## Avisos importantes

- Sempre rode o **dry-run** antes de `--execute`.
- Backup automático com `manifest.json` é criado antes de gravar; use `--revert` se precisar desfazer.
- Pastas do projeto ficam intactas; apague a antiga **só depois** de validar os chats no caminho novo.
- Este projeto **não é afiliado** ao Cursor / Anysphere.
- Testado em **Windows**.

---

## Licença

MIT — veja [LICENSE](LICENSE).
