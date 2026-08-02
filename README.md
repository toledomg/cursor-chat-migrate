# cursor-chat-migrate

Ferramenta CLI em Python para migrar o histórico de chats do [Cursor](https://cursor.com) após renomear ou mover a pasta de um projeto.

Repositório: [github.com/toledomg/cursor-chat-migrate](https://github.com/toledomg/cursor-chat-migrate)

Quando você renomeia um repositório ou altera o caminho no disco, o Cursor passa a tratá-lo como um workspace novo — e os chats antigos deixam de aparecer. Este script relinka conversas, metadados e transcripts para o caminho atualizado.

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
| **Cursor** | Instalado e já utilizado nos dois caminhos (antigo e novo) |
| **Permissões** | Modo junction exige terminal como Administrador |

> **Antes de migrar:** abra o projeto no **caminho novo** no Cursor pelo menos uma vez (`File → Open Folder`). Isso cria o workspace em `%APPDATA%\Cursor\User\workspaceStorage\`, necessário para o script encontrar o ID do workspace destino.

---

## Instalação

```bash
git clone https://github.com/toledomg/cursor-chat-migrate.git
cd cursor-chat-migrate

python -m venv .venv
.venv\Scripts\activate        # Windows (PowerShell/CMD)
# source .venv/bin/activate   # Linux/macOS (não suportado pelo script)

pip install -r requirements.txt
```

---

## Configuração

Copie o template de ambiente e preencha os caminhos do projeto:

```bash
cp .env.example .env
```

Edite o arquivo `.env`:

```env
# Obrigatórias
OLD_PROJECT_PATH=F:\Projetos\meu-projeto-antigo
NEW_PROJECT_PATH=F:\Projetos\meu-projeto-novo

# Opcionais (valores padrão já funcionam na maioria dos casos)
# CURSOR_USER_DATA=C:\Users\SeuUsuario\AppData\Roaming\Cursor\User
# CURSOR_HOME_PROJECTS=C:\Users\SeuUsuario\.cursor\projects
```

| Variável | Obrigatória | Descrição |
|----------|:-----------:|-----------|
| `OLD_PROJECT_PATH` | Sim | Caminho absoluto da pasta **antes** da renomeação |
| `NEW_PROJECT_PATH` | Sim | Caminho absoluto da pasta **depois** da renomeação |
| `CURSOR_USER_DATA` | Não | Pasta `User` do Cursor (padrão: `%APPDATA%\Cursor\User`) |
| `CURSOR_HOME_PROJECTS` | Não | Pasta de projetos do Cursor (padrão: `~/.cursor/projects`) |

> O arquivo `.env` está no `.gitignore` e **não deve** ser commitado.

---

## Uso rápido

1. **Feche o Cursor** completamente (todas as janelas).
2. Confirme que o `.env` está correto.
3. Execute em modo simulação:

```bash
python migrate-cursor-chats.py
```

4. Se a saída estiver correta, aplique as alterações:

```bash
python migrate-cursor-chats.py --execute
```

5. Abra o projeto no Cursor usando `NEW_PROJECT_PATH`. O histórico de chats deve aparecer normalmente.

---

## Modos de operação

### Migração completa (padrão)

Atualiza o banco SQLite do Cursor, copia dados de workspace e mescla agent transcripts. Recomendado quando você quer abrir o projeto **pelo caminho novo**.

```bash
python migrate-cursor-chats.py              # dry-run
python migrate-cursor-chats.py --execute    # aplica
```

### Modo junction (alternativa simples)

Cria um **junction point** do Windows: o caminho antigo passa a apontar para a pasta nova, sem alterar o banco de dados.

```bash
python migrate-cursor-chats.py --junction
python migrate-cursor-chats.py --junction --execute
```

| Aspecto | Migração completa | Junction |
|---------|:-----------------:|:--------:|
| Altera banco SQLite | Sim | Não |
| Requer Admin | Não | Sim |
| Abrir projeto por | Caminho **novo** | Caminho **antigo** |
| Complexidade | Maior | Menor |

---

## Referência da CLI

```
usage: migrate-cursor-chats.py [-h] [--execute] [--junction] [--skip-disk-kv]

Migra histórico de chats do Cursor após renomear pasta do projeto.

options:
  -h, --help       Exibe esta ajuda e sai
  --execute        Aplica alterações (sem isso, só simula)
  --junction       Cria junction do caminho antigo → novo
  --skip-disk-kv   Pula patch do cursorDiskKV (etapa lenta)
```

| Flag | Descrição |
|------|-----------|
| *(sem flags)* | **Dry-run** — simula todas as etapas sem modificar arquivos |
| `--execute` | Aplica as alterações de fato |
| `--junction` | Usa modo junction em vez da migração completa |
| `--skip-disk-kv` | Ignora a varredura de `cursorDiskKV` (1–5 min). A UI de chats usa principalmente `composer.composerHeaders`, então na maioria dos casos esta flag é segura |

---

## O que o script altera

Com `--execute`, a migração completa executa as seguintes etapas:

```
┌─────────────────────────────────────────────────────────────┐
│  1. Backup automático                                       │
│     %APPDATA%\Cursor\User\backups\                          │
│     cursor-chat-migration-YYYYMMDD-HHMMSS\                  │
├─────────────────────────────────────────────────────────────┤
│  2. workspaceStorage                                        │
│     Copia dados do workspace antigo → novo                  │
│     Atualiza workspace.json com o URI do caminho novo        │
├─────────────────────────────────────────────────────────────┤
│  3. globalStorage / state.vscdb                             │
│     Relinka chats em composer.composerHeaders               │
│     Atualiza referências de path em cursorDiskKV            │
├─────────────────────────────────────────────────────────────┤
│  4. agent-transcripts                                       │
│     Mescla ~/.cursor/projects/<slug>/agent-transcripts      │
└─────────────────────────────────────────────────────────────┘
```

### Caminhos relevantes no Windows

| Item | Caminho típico |
|------|----------------|
| Workspace storage | `%APPDATA%\Cursor\User\workspaceStorage\` |
| Banco global | `%APPDATA%\Cursor\User\globalStorage\state.vscdb` |
| Agent transcripts | `%USERPROFILE%\.cursor\projects\<slug>\agent-transcripts\` |

O `<slug>` é derivado do caminho do projeto (ex.: `F:\Projetos\meu-app` → `f-Projetos-meu-app`).

---

## Estrutura do projeto

```
cursor-chat-migrate/
├── migrate-cursor-chats.py   # Script principal
├── requirements.txt          # Dependências Python
├── .env.example              # Template de configuração
├── .env                      # Sua configuração local (não versionado)
├── .gitignore
├── LICENSE
└── README.md
```

---

## Solução de problemas

<details>
<summary><strong>Workspace ID antigo não encontrado</strong></summary>

O Cursor ainda precisa ter um registro em `workspaceStorage` apontando para `OLD_PROJECT_PATH`. Se você já apagou manualmente essa pasta, a migração completa pode não funcionar — considere o modo `--junction`.
</details>

<details>
<summary><strong>Workspace ID novo não encontrado</strong></summary>

Abra o projeto em `NEW_PROJECT_PATH` no Cursor (`File → Open Folder`) e execute o script novamente.
</details>

<details>
<summary><strong>Cursor.exe está em execução</strong></summary>

Feche todas as janelas do Cursor antes de rodar com `--execute`. O dry-run funciona com o Cursor aberto, mas a execução real pode corromper o banco SQLite.
</details>

<details>
<summary><strong>Etapa cursorDiskKV demora muito</strong></summary>

Use `--skip-disk-kv`. A listagem de chats na interface depende principalmente de `composer.composerHeaders`, que é atualizado independentemente.
</details>

<details>
<summary><strong>Erro ao criar junction</strong></summary>

Execute o terminal como **Administrador** e confirme que `OLD_PROJECT_PATH` ainda não existe no disco.
</details>

<details>
<summary><strong>Variável não definida no .env</strong></summary>

```
ERRO: variável OLD_PROJECT_PATH não definida.
  Copie .env.example para .env e preencha os caminhos do projeto.
```

Certifique-se de que o `.env` está na mesma pasta do script e contém ambos os caminhos.
</details>

---

## Avisos importantes

- **Sempre rode o dry-run primeiro** (`python migrate-cursor-chats.py`) e revise a saída antes de usar `--execute`.
- Um **backup automático** é criado antes de qualquer alteração, mas recomenda-se também fechar o Cursor e não interromper o script durante a execução.
- Este projeto **não é afiliado** ao Cursor ou à Anysphere Inc.
- Testado em **Windows**. Outros sistemas operacionais não são suportados no momento.

---

## Licença

MIT — veja o arquivo [LICENSE](LICENSE) para detalhes.
