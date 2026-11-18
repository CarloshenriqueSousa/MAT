# MAT — Roadmap

## Estado Real

O esqueleto, configuração, persistência SQLite, contratos, state machine, runner
Docker, gerenciador de worktree, gate determinístico, CLI e testes existem. A CLI
continua ligada a stubs (incluindo `GateStub`) e grava `execution_mode=simulation`.

Analista, Programador, Corretor, Tester e Verificador têm implementações reais e o
orquestrador já aceita o gate real (`DeterministicGate.check`). A fábrica
`mat.factory` monta o conjunto real a partir de `agents.yaml` e de um worktree
(D52), mas a CLI ainda não cria worktrees e por isso só usa o conjunto simulado. O laço de
correção é limitado (D51) e o consumo real de tokens é persistido por tentativa.

O modo supervisionado pausa após o plano e é retomado com `mat resume <run_id>`
(ou `--reject`); cada checkpoint guarda o estado completo do laço (D53).

O Analista é registrado como dispatch, com retry e evidências como os demais agentes (D54).

Os limites seguem D43 por padrão (1 plano × 1 tentativa, uma rodada de correção);
tentativas extras e replan até o teto de D11/D28 são opt-in em `mat.yaml` (D55).

## Próximo Marco: Fatia Vertical Real

Ordem proposta; cada etapa deve ter teste e evidência antes da seguinte.

| Passo | Entrega | Critério de conclusão |
|---|---|---|
| 1 | Validar o sandbox no host Linux (D49) | Testes de integração Docker passando; `--network none`, limites e máscara de `.git` comprovados (CI `integration`); fábrica que monta agentes reais a partir de `agents.yaml` (D52) |
| 2 | TUI Textual para abrir projeto e selecionar branch-base | Listar branches locais/remotas, mostrar a revisão exata e exigir escolha/confirmação humana |
| 3 | Criar branch/worktree por solicitação | Registrar branch e commit-base; checkout original fica intacto; checkout sujo pausa sem copiar/apagar alterações |
| 4 | Conversa Textual com contexto e acompanhamento ao vivo | Receber pedido, backlog, arquivos e imagens; mostrar fase, agente, tentativas, erros e correções; links privados exigem conteúdo fornecido manualmente |
| 5 | Analista e aprovação do plano | Produzir plano revisável; nenhuma edição começa antes da aprovação humana |
| 6 | Baseline e execução do agente | Medir testes existentes antes de editar; se falharem, pausar; ligar Programador/Corretor reais ao orquestrador com o gate determinístico |
| 7 | Validação e correção limitada | Executar scripts conhecidos; pedir aprovação para comandos novos/arriscados e alterações em testes existentes; permitir no máximo uma rodada de correção automática |
| 8 | Revisão final e entrega | Mostrar diff, testes, alertas, erros e métricas; manter worktree sem commit, push ou PR |
| 9 | Retomada, cancelamento e remediações separadas | Retomar explicitamente também as pausas por falha (a aprovação de plano já é retomável, D53); erros fora do escopo viram proposta de worktree baseado na branch observada, após aceite humano |

Não habilitar modo `real` antes de fechar os passos 1–8 e revisar o threat model.
Outras linguagens dependem de imagens de sandbox por linguagem (D47); descoberta e
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
