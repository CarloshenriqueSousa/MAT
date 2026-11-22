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

### 0. Agentes do MAT (D47)
- Os agentes usam o loop próprio com o SDK Anthropic; não há CLI externa nem hooks/MCP do repositório
- Ferramentas: `text_editor` no host (caminho validado contra escape do worktree) e `bash` somente no container
- A chave da API fica no processo do MAT no host e não é repassada ao container

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

### 6. Interface web local (planejada, D57)
- A API escuta apenas em `127.0.0.1` no host Linux; o acesso da estação é por túnel SSH, nunca por porta exposta
- Toda requisição exige um token local gerado no host; WebSocket usa o mesmo token
- Conteúdo vindo do repositório-alvo (nomes de branch, mensagens, diffs) é exibido como texto, nunca como HTML
- A interface não ganha ações além das da CLI: sem commit, push ou PR

### 7. Workspace por solicitação (D58)
- O MAT cria no repositório-alvo apenas a branch `mat/<run_id>` e um worktree fora dele (`paths.worktrees`); o checkout original não é alterado
- Checkout original sujo pausa a run antes de criar qualquer coisa; nada é copiado, movido ou apagado
- `git status` roda com `--no-optional-locks` e com o Git endurecido (sem hooks, fsmonitor, filtros nem config global)

### 8. Modo planning (D59)
- Só o Analista roda com LLM; os demais papéis são `NotWiredAgent` e recusam qualquer chamada, então nenhum agente edita o worktree
- O baseline executa os testes do repositório-alvo, mas só no container do sandbox (D16/D17), nunca no host
- O Analista recebe apenas a lista de arquivos e as métricas do baseline, não o conteúdo dos arquivos
- O plano vem do LLM e é exibido como texto literal (sem markup); aprovar não dispara execução

### 9. Modo implementation (D61)
- Nenhuma edição antes da aprovação humana do plano; a aprovação é um comando explícito (`mat resume`)
- O Programador edita só arquivos do worktree (handlers com validação de path, D16) e executa comandos só no container; o MAT não faz commit, push nem PR
- O Corretor recebe as mesmas ferramentas, mas a instrução é não editar; isso **não** é uma garantia técnica: ele pode alterar o worktree, e o gate e a revisão humana valem sobre o estado final
- O gate determinístico (anti-cheat, Ruff, pytest, cobertura vs baseline) roda no sandbox sobre o próprio worktree, não ainda em container novo com testes originais (D19)
- Gate aprovado não conclui a run: Tester e Verificador rodam em seguida (D62), e mesmo com o Verificador aprovando a run pausa em `final_review` para revisão humana do diff; o veredito do Verificador é evidência, não autorização
- O Verificador lê o diff, que pode conter texto escrito para manipulá-lo (prompt injection); por isso sua aprovação nunca conclui a run sozinha
- Na rodada de correção, a falha de validação (incluindo o final da saída do pytest, que vem do código do repositório) volta ao Programador como feedback, limitada a 4000 caracteres; isso é conteúdo não confiável dentro do prompt, com o mesmo poder que o Programador já tem ao ler os arquivos do worktree

## Riscos residuais
- Container compartilha kernel com o host (defesa em camadas, não fronteira para código não confiável)
- Worktree gravável expõe todos os arquivos daquele worktree ao processo no container
- A CLI usa o conjunto simulado (`GateStub`) sem projeto e, com `--repo`, os conjuntos `implementation` (todos os agentes reais, pausa em `final_review`) ou `planning` (`--plan-only`); o conjunto real completo (`build_real_agents`) ainda não é exposto pela CLI
- Ferramentas do Corretor permitem escrita no worktree (ver seção 9)
- O diff inclui arquivos novos regulares, mas symlinks novos são omitidos da evidência e devem ser rejeitados antes de uma revisão final
- Evolução futura: gVisor ou microVM para isolamento mais forte
- Se `docker_runner.py` mudar, todo o modelo de ameaça precisa ser reavaliado
