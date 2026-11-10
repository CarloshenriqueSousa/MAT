# MAT — Threat Model

## O que é não confiável

| Ativo | Nível de confiança | Justificativa |
|---|---|---|
| **Repositório-alvo** (código-fonte, deps, config) | ❌ Não confiável | Pode conter prompt injection indireta via README, issues, docstrings, conftest.py, hooks, scripts |
| **Saída das ferramentas** (stdout, stderr, arquivos gerados) | ❌ Não confiável | Gerada a partir de código não confiável executado no sandbox |
| **Saída do modelo** (código gerado, edições, testes) | ⚠️ Semi-confiável | Pode ser influenciada por prompt injection do repo-alvo |
| **Config e prompts do MAT** | ✅ Confiável | Controlados pelo operador |
| **Banco SQLite** | ✅ Confiável | Escrito apenas pelo MAT, no filesystem do host |

## Controles de segurança

### 1. Isolamento de execução (container Docker)
- `--network none`: sem acesso à internet durante execução
- Rootfs somente leitura (`--read-only`) com `/tmp` gravável
- Limites de CPU, memória e pids (`--cpus`, `--memory`, `--pids-limit`)
- `--user` com uid/gid do host para não criar arquivos como root
- Timeout por comando; ao expirar, o runner envia `docker kill` ao container nomeado
- O worktree é montado com escrita habilitada para ferramentas de edição e execução; portanto, o container pode alterar qualquer arquivo desse worktree

### 2. Separação host vs container
- **No host**: edição de arquivos (view/str_replace/create/insert) com resolução de path que bloqueia escape do worktree por traversal ou symlink, e match único no str_replace
- **No container**: toda execução (bash, pytest, ruff, coverage)
- **Git no host**: hooks desabilitados (`core.hooksPath=/dev/null`), configuração global/sistema ignorada, diff externo/textconv desabilitados, arquivo de atributos global neutralizado e filtros `clean`/`smudge`/`process` declarados na configuração local sobrescritos como vazios

### 3. Preparação de dependências
- Ainda não implementada no fluxo da CLI
- Os comandos de execução usam `--network none`; a imagem de sandbox e suas dependências precisam ser preparadas externamente pelo operador

### 4. Gate final
- O gate determinístico possui checagens para alterações em testes existentes, `conftest.py`, configurações comuns de teste/cobertura e marcadores/chamadas de skip
- A proteção depende do diff fornecido e ainda não está conectada ao fluxo da CLI
- O runner não restaura um snapshot original de testes/configuração; ele executa sobre o worktree atual. Não considerar esta checagem um gate de release até a integração e o snapshot imutável serem implementados

### 5. Saída controlada
- O Programador pode criar um commit na branch do worktree
- A CLI atual usa stubs e não cria PR nem publica/deploya mudanças
- A aprovação do pipeline atual é simulada; não representa validação do código

## Riscos residuais
- Container compartilha kernel com o host (defesa em camadas, não fronteira para código não confiável)
- Worktree gravável expõe todos os arquivos daquele worktree ao processo no container
- A CLI instancia agentes stub e o orquestrador ainda usa resultado simulado para o gate determinístico
- O diff inclui arquivos novos regulares, mas symlinks novos são omitidos da evidência e devem ser rejeitados antes de uma revisão final
- Evolução futura: gVisor ou microVM para isolamento mais forte
- Se `docker_runner.py` mudar, todo o modelo de ameaça precisa ser reavaliado
