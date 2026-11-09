# MAT — Registro de Decisões

Cada linha é uma decisão fechada, com data e motivo. Não volta atrás sem nova ADR.

| # | Data | Decisão | Motivo |
|---|---|---|---|
| D01 | 2026-11-03 | Nome provisório: **MAT** (Multi Agent Tenant) | Escolha do autor |
| D02 | 2026-11-03 | Pipeline fixo (3 fases), engine genérica depois | Elimina sobre-engenharia no dia zero |
| D03 | 2026-11-04 | **Python 100%** (sem TypeScript) | Zero overhead de IPC entre linguagens; Python controla SQLite, Anthropic SDK, Docker SDK |
| D04 | 2026-11-04 | SO host: Windows 11 + WSL2 (Ubuntu 22.04). Repo e worktrees dentro do filesystem WSL2, nunca em NTFS | Bind mount NTFS→container é lento, problemas de path e permissão |
| D05 | 2026-11-04 | Alvo MVP: repositório Python simples (pytest + ruff) | Simplifica sandbox Docker (imagem Python leve) |
| D06 | 2026-11-04 | Loop próprio com SDK `anthropic` + tool use, sem Agent SDK | Agent SDK roda ferramentas built-in no host e fura o sandbox; teto de tokens mais fácil turno a turno |
| D07 | 2026-11-04 | Modelos: `claude-sonnet-5-5`, `claude-haiku-5-5`, `claude-opus-5-5`. Sempre vindos de `agents.yaml`, nunca hardcoded | Modelos da geração atual; desacoplamento de modelo e agente |
| D08 | 2026-11-04 | Provider único (Anthropic), contextos e temperaturas diferentes por agente | Simplifica infra; diversidade vem do prompt e da temperatura |
| D09 | 2026-11-04 | Gate determinístico PRIMEIRO (pytest exit code, ruff, cobertura vs baseline). Verificador LLM só entra se gate determinístico passar | Confiança verbalizada por LLM não é calibrada (AUROC ~62.7% GPT-4, Xiong et al.) |
| D10 | 2026-11-04 | Sem threshold numérico de confiança (sem 0.92). Verificador faz pass/fail por critério citando evidência, sem score | Modelos são superconfiantes; scores entre 0.8–1.0 não discriminam |
| D11 | 2026-11-04 | Limites de retry: 3 attempts/plano × (1 plano inicial + 2 replans) = até 9 attempts totais. + cap de tokens e timeout por attempt | Previne loops infinitos e controla custo |
| D12 | 2026-11-09 | Corretor: agente separado no Passo 2, após Programador. Só aponta problemas; Programador corrige. Loop controlado por teto de tokens, não por número de voltas | Separação de responsabilidades; evita auto-confirmação |
| D13 | 2026-11-09 | Corretor considera pronto quando: pytest passa, ruff sem erros, diff atende objetivo original da task | Critérios determinísticos + semântico |
| D14 | 2026-11-09 | MVP só com tester convencional (Happy Path). Bypass e Breaker adicionados depois | Fatia fina primeiro |
| D15 | 2026-11-09 | Programador escreve testes da feature (vão no commit). Testers só validam executando. Testes bypass/breaker: entram no PR apenas se determinísticos e passarem, selecionados pelo Consolidador | Testes persistentes no PR; efêmeros descartados |
| D16 | 2026-11-09 | Edição de arquivos no host (handlers view/str_replace/create/insert com validação de path). Execução (bash/pytest/ruff) no container via docker exec, --network none, rootfs RO, /tmp gravável, limites CPU/mem/pids, --user com uid/gid do host | Isolamento onde o risco está (execução), sem custo de tudo no container |
| D17 | 2026-11-09 | Host nunca executa código do worktree. Git (commit/diff/PR) roda no host, mas nada do projeto-alvo | Mitigação de prompt injection indireta via conftest.py, hooks, scripts |
| D18 | 2026-11-09 | Dependências entram antes do loop. Etapa de preparação com rede liberada só nela. Depois, loop do agente roda offline | Pré-instala deps na imagem/volume em cache |
| D19 | 2026-11-09 | Gate final roda em container NOVO com testes e config originais | Impede Programador de influenciar a verificação |
| D20 | 2026-11-09 | Checkpoint `awaiting_human` após plano do Analista (opcional por flag) | Modo supervisionado: humano aprova antes de gastar tokens |
| D21 | 2026-11-09 | Schemas de editor/bash da Anthropic (`text_editor_20250728`, `bash_20250124`) como interface dos tools | Schemas prontos, validação de path, match único no str_replace |
| D22 | 2026-11-09 | Mutation gate (mutmut) na Fase 2, não no MVP | Custo alto; MVP foca na fatia fina funcional |
| D23 | 2026-11-09 | Dividir documentação: README.md + docs/architecture.md + docs/decisions.md + docs/roadmap.md | README ≠ documento de arquitetura |
| D24 | 2026-11-09 | Modo inicial: supervisionado (TUI). Semi-autônomo futuramente | Segurança e confiança incremental |
| D25 | 2026-11-09 | Objetivo: ferramenta pessoal primeiro, produto para outros depois | Foco pragmático |
| D26 | 2026-11-09 | Stack futura: Python (agentes), Java (daemon/supervisor), TypeScript/React (frontend) | Java como servidor principal na máquina |
| D27 | 2026-11-09 | Plano ambíguo do Analista → pausa `awaiting_human` e pede ajuda do humano | Evita gastar tokens com plano ruim |
| D28 | 2026-11-09 | Se plan falha 3x, escala de volta para o Analista (replan). Se analista falha, escala para humano | Escalação reversa com limites |
| D29 | 2026-11-09 | Módulo de análise quantitativa: funcionalidade separada (heatmaps, Monte Carlo, superfícies, correlações) | Dashboards de custo, tentativas e performance |
| D30 | 2026-11-09 | Verificador precisa comparar diff com objetivo original (não só resultados de testes). Referência: MAST NeurIPS 2025 | Checagem superficial é modo de falha comum em multi-agentes |
| D31 | 2026-11-09 | Handoff entre agentes por modelos Pydantic, não texto livre | Contratos tipados previnem desalinhamento |
| D32 | 2026-11-09 | Onde o Verificador suspeitar, rodar N vezes e votar (majority voting) em vez de score de confiança | Amostrar e medir consistência > confiança verbalizada (92.7% vs 54.8% em GSM8K) |
