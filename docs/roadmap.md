# MAT — Roadmap

## Estado Real

O esqueleto, configuração, persistência SQLite, contratos iniciais, state
machine, runner Docker, gerenciador de worktree, gate determinístico, CLI e
testes unitários existem. Isso não equivale a uma execução de código real: a
CLI continua ligada a stubs, o gate chamado pelo orquestrador é simulado e não
há seleção de repositório-alvo nem retomada pela CLI.

Os agentes Programador e Corretor têm implementações LLM, mas não estão ligados
à CLI operacional. O Analista, Tester, Verificador e aprovação final ainda
precisam ser integrados ao fluxo real. O campo `execution_mode` diferencia
simulação, execução real e registros antigos de origem desconhecida.

## Próximo Marco: Fatia Vertical Real

Ordem proposta; cada etapa deve ter teste e evidência antes da seguinte.

| Passo | Entrega | Critério de conclusão |
|---|---|---|
| 1 | Protótipo descartável do Claude Code no WSL | Confirmar `-p --output-format json/stream-json` e métricas; começar sem hooks/MCP do repo; testar `--restricted`/`--bare`, acesso fora do worktree, sandbox Bash com falha fechada e sem retry não isolado, allowlist de egress, credenciais e encerramento SIGTERM/timeout |
| 2 | TUI Textual para abrir projeto e selecionar branch-base | Listar branches locais/remotas, mostrar a revisão exata e exigir escolha/confirmação humana |
| 3 | Criar branch/worktree por solicitação | Registrar branch e commit-base; checkout original fica intacto; checkout sujo pausa sem copiar/apagar alterações |
| 4 | Conversa Textual com contexto e acompanhamento ao vivo | Receber pedido, backlog, arquivos e imagens; mostrar fase, agente, tentativas, erros e correções; links privados exigem conteúdo fornecido manualmente |
| 5 | Analista e aprovação do plano | Produzir plano revisável; nenhuma edição começa antes da aprovação humana |
| 6 | Baseline e execução do agente | Medir testes existentes antes de editar; se falharem, pausar; integrar Claude Code sob a fronteira validada no passo 1 |
| 7 | Validação e correção limitada | Executar scripts conhecidos; pedir aprovação para comandos novos/arriscados e alterações em testes existentes; permitir no máximo uma rodada de correção automática |
| 8 | Revisão final e entrega | Mostrar diff, testes, alertas, erros e métricas; manter worktree sem commit, push ou PR |
| 9 | Retomada, cancelamento e remediações separadas | Retomar checkpoints explicitamente; erros fora do escopo viram proposta de worktree baseado na branch observada, após aceite humano |

Não habilitar modo `real` antes de fechar os passos 1–8 e revisar o threat model.
Suporte a diferentes linguagens será delegado ao agente CLI; descoberta e
autorização de comandos de validação continuam específicas por projeto. Se uma
etapa falhar, permanecer em `awaiting_human`; não simular sucesso.

## Depois da Fatia Vertical

| Área | Entrega posterior |
|---|---|
| Testes | Bypass e Breaker em worktrees independentes; consolidação determinística dos testes aceitos |
| Segurança | gVisor como runner padrão, snapshot imutável para gate final e testes de fronteira do host/container |
| Qualidade | Mutation gate e avaliação em conjunto fixo de tasks reais |
| Observabilidade avançada | Histórico comparativo de custo/tokens, duração, tentativas, erros, correções e resultados dos gates; somente após métricas reais estarem disponíveis |
| Análise quantitativa | Módulo separado sobre histórico de runs: distribuições e correlações de custo/duração/sucesso, heatmaps, superfícies e Monte Carlo com premissas e intervalos de incerteza; não mostrar projeções sem amostra histórica suficiente |
| Pesquisa | RAG de PRs e majority voting, condicionados a avaliações que provem benefício |
| Expansão | Visualização gráfica complementar, daemon, multi-repo e outras extensões após validar o uso pessoal |

Black–Scholes não é requisito do módulo: a referência visual não implica um
domínio financeiro. A análise usa dados observados das execuções do MAT.

## Fora do MVP

Commit, push, publicação/criação de PR, cards no Teams, múltiplos repositórios
por execução e execução paralela de agentes permanecem fora do primeiro marco.
