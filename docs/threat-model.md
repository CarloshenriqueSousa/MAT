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

### 0. Agente CLI externo (planejado; fronteira ainda não validada)
- Claude Code será a primeira integração e deve operar a partir do worktree no WSL
- O modo não interativo `claude -p` pode carregar hooks e servidores MCP do repositório sem um diálogo de confiança; iniciar o protótipo sem configurações não confiáveis do alvo (por exemplo, bare/restricted mode e fontes de settings explícitas)
- A sandbox nativa WSL2 protege Bash/PowerShell e seus subprocessos, não as ferramentas internas Read/Edit/WebFetch, hooks, MCPs ou o processo completo da CLI
- Diretório de trabalho e regras de ferramentas não provam confinamento do processo; testar acesso fora do worktree nas ferramentas internas e nos subprocessos
- Validar que sandboxing falha fechado (`failIfUnavailable`), não permite retry fora da sandbox, e que os comandos ficam sujeitos a uma allowlist explícita de egress; allowlists por hostname não inspecionam TLS e não eliminam domain fronting
- JSON/stream JSON pode fornecer resultado, sessão, uso de tokens e estimativa de custo; custo não é valor faturado. Um supervisor externo precisa impor timeout e enviar SIGTERM, verificando o encerramento dos subprocessos
- Bare mode evita carregar customizações do projeto, mas não lê OAuth salvo; verificar como a autenticação escolhida será disponibilizada sem expor credenciais aos comandos do agente
- Antes de execução real, o protótipo descartável deve testar permissões, variáveis de ambiente/credenciais, conexões diretas e via proxy, timeout/cancelamento e sobrevivência de processos filhos
- Até essa validação, não afirmar que o agente está isolado nem habilitar execução em repositórios de trabalho
- Distinguir a conexão do processo Claude ao provedor da rede concedida a scripts e testes do projeto

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
- A política desejada não permite commit, push ou PR pelo MAT; a pessoa responsável executa essas ações manualmente após revisar o worktree
- A CLI atual usa stubs e não cria PR nem publica/deploya mudanças
- A aprovação do pipeline atual é simulada; não representa validação do código

## Riscos residuais
- Container compartilha kernel com o host (defesa em camadas, não fronteira para código não confiável)
- A futura CLI externa no WSL poderá ter acesso maior que o worktree; permissões da CLI não devem ser confundidas com isolamento forte até o protótipo validar os limites
- Worktree gravável expõe todos os arquivos daquele worktree ao processo no container
- A CLI instancia agentes stub e o orquestrador ainda usa resultado simulado para o gate determinístico
- O diff inclui arquivos novos regulares, mas symlinks novos são omitidos da evidência e devem ser rejeitados antes de uma revisão final
- Evolução futura: gVisor ou microVM para isolamento mais forte
- Se `docker_runner.py` mudar, todo o modelo de ameaça precisa ser reavaliado
