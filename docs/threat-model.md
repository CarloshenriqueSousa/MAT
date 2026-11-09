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
- Timeout por comando

### 2. Separação host vs container
- **No host**: edição de arquivos (view/str_replace/create/insert) com validação de path — sem `..`, sem symlinks que saiam do worktree, match único no str_replace
- **No container**: toda execução (bash, pytest, ruff, coverage)
- **Nunca no host**: scripts do projeto-alvo, hooks, conftest.py

### 3. Preparação de dependências
- Etapa isolada COM rede para instalar deps do alvo
- Deps ficam em imagem ou volume em cache
- Loop do agente roda inteiramente offline

### 4. Gate final
- Roda em container NOVO e limpo
- Usa testes e config ORIGINAIS (snapshot antes das mudanças)
- Reprova diffs que mexam em arquivos de teste existentes, config de cobertura, ou adicionem skip/xfail

### 5. Saída controlada
- Mudanças só saem do sandbox como branch Git + PR revisado por humano
- Nenhum deploy automático sem aprovação explícita

## Riscos residuais
- Container compartilha kernel com o host (defesa em camadas, não fronteira para código não confiável)
- Evolução futura: gVisor ou microVM para isolamento mais forte
- Se `docker_runner.py` mudar, todo o modelo de ameaça precisa ser reavaliado
