# MAT — Multi-Agent Tenant

Orquestrador de agentes CLI para transformar solicitações em alterações de código
e testes em projetos de diferentes linguagens. O MAT prepara uma branch/worktree;
commit, push e PR continuam sob controle manual da pessoa responsável.

> **Status:** em desenvolvimento. Este documento descreve tanto o estado atual
> quanto as decisões para a arquitetura desejada; itens futuros não devem ser
> confundidos com funcionalidades já implementadas.

## Visão do produto

O fluxo desejado começa pela abertura de um projeto e escolha humana da
branch-base. A pessoa descreve a solicitação em conversa e pode fornecer
backlog, arquivos e imagens. Claude Code é a primeira CLI de agente planejada;
outras CLIs poderão ser adicionadas por adaptadores. Links privados são
fornecidos manualmente no MVP.

```text
Projeto/branch → Conversa + contexto → Plano aprovado → Agente CLI
        → Testes do projeto → Revisão humana → Worktree sem commit
```

1. **Preparação:** abrir o projeto, escolher branch/revisão-base e criar uma
  branch/worktree por solicitação. Checkout de origem com alterações locais
  pausa para orientação; o MAT não copia nem descarta essas alterações.
2. **Planejamento:** interpretar conversa e anexos, medir a baseline e apresentar
  o plano. A pessoa aprova o plano antes de qualquer edição.
3. **Agente e validação:** Claude Code é a primeira integração planejada. O MAT
  usa os scripts de teste conhecidos do projeto; comandos novos/arriscados e
  alterações em testes existentes pedem aprovação. O agente pode criar testes.
4. **Falhas:** baseline já quebrada pausa antes de editar. O fluxo permite no
  máximo uma rodada de correção automática antes de pedir orientação. Erros
  fora do escopo viram proposta de remediação separada, baseada na branch onde
  foram observados e sujeita a aceite humano.
5. **Revisão final:** mostrar diff, testes e alertas. A entrega fica sem commit;
  `git add`, commit e push são manuais. O MAT não cria nem publica PR ou cards
  no Teams.

O MAT deve pausar e pedir orientação quando houver ambiguidade relevante. Não
deve publicar alterações sem aprovação humana.

## Princípios e decisões de arquitetura

- **Agente externo:** suportar projetos de diferentes linguagens por adaptadores
  de CLI; Claude Code é a primeira. Isso não implica comandos universais de teste.
- **Workspace por solicitação:** uma branch e um Git worktree baseados na
  branch/revisão escolhida pela pessoa. O checkout original não é editado.
- **Checkout sujo:** pausar e recomendar ações; não copiar nem apagar mudanças
  locais automaticamente.
- **Isolamento a comprovar:** um protótipo descartável deve medir os limites da
  CLI no WSL. Diretório de trabalho no worktree não prova confinamento do processo.
- **Retenção do workspace:** manter branches e worktrees após a execução,
  inclusive em falhas, até a pessoa responsável solicitar explicitamente a
  limpeza.
- **Retomada:** execuções interrompidas ficam pausadas no último checkpoint
  válido e só continuam mediante comando explícito da pessoa responsável.
- **Baseline com falhas:** pausar antes da edição e pedir decisão. Falha fora do
  escopo pode gerar proposta de execução separada, baseada na branch observada;
  criar worktree somente após aceite humano.
- **Runner substituível:** usar Docker com gVisor (`runsc`) como isolamento
  padrão. Manter a interface do runner independente da implementação para
  permitir avaliar microVMs ou outra tecnologia no futuro.
- **Comandos de validação:** executar scripts conhecidos do projeto; pedir
  confirmação para comandos novos/arriscados. A política de rede do agente,
  testes e preparação de dependências depende do protótipo de isolamento.
- **Controles de recursos:** limitar tempo, memória e CPU das execuções.
- **Integridade dos testes:** sinalizar alterações em testes existentes,
  configuração de cobertura e uso de `skip`/`xfail`. Os alertas devem ser
  apresentados na aprovação humana; não substituem a avaliação do diff.
- **Rastreabilidade:** registrar estados, tentativas, custos, testes, evidências
  e checkpoints para permitir auditoria e acompanhamento.
- **Fluxo controlado:** validar cada destino do Verificador contra as rotas
  permitidas, o estado atual e os limites de retry/replanejamento; toda decisão
  deve ficar registrada com sua justificativa.
- **Publicação manual:** o MAT não cria nem publica PRs. Após a aprovação, deixa
  o trabalho sem commit, pronto para a pessoa responsável revisar e commitar.
  Cards no Teams e PRs são sempre criados manualmente; o MAT pode apenas preparar
  sugestões para esses itens.

## Repositórios de referência

Os projetos abaixo são referências iniciais para estudar e orientar decisões de
arquitetura e implementação. A experiência de projeto/worktree e agentes do
Orca inspira o fluxo, mas não implica copiar a plataforma inteira:

- [REA](https://github.com/morluto/rea)
- [Orca](https://github.com/stablyai/orca)

São referências, não dependências incorporadas nem projetos-alvo do MAT. Padrões
ou componentes só devem ser adotados após avaliar adequação ao projeto e as
licenças aplicáveis.

Cada execução atua em um único repositório Git. Tasks que envolvam projetos
distintos devem ser divididas em execuções separadas.

### Aprendizados da análise

**Orca — referência principal para orquestração.** O modelo Run → Task →
Dispatch separa a execução maior, o trabalho planejado e cada tentativa de
agente. Identificadores e estados explícitos tornam mais fácil rastrear falhas,
retomar trabalho e limitar concorrência. Para o MAT, cada Dispatch deve guardar
papel do agente, tentativa, entrada, resultado estruturado, estado e evidências;
cancelamento e timeout também precisam ter estados/resultados verificáveis.

Worktrees independentes são uma boa fronteira para trabalho paralelo, como os
três testers, mas não eliminam conflitos ao integrar alterações nem isolam
recursos externos compartilhados. O MAT deve criar cada worktree a partir de
uma revisão-base explícita, registrar essa revisão e deixar a integração dos
resultados sob responsabilidade do Consolidador. Setup, retenção e limpeza
devem continuar sujeitos às regras de supervisão já definidas.

O Orca também demonstra a utilidade de persistir execuções e tentativas em
SQLite e oferecer resumos consultáveis por ID. O MAT já usa SQLite e possui
entidades para runs, tasks, tentativas, dispatches, evidências e checkpoints;
o próximo passo é conectar esses registros a cada operação real, em vez de
replicar a infraestrutura ampla de UI, sessões remotas e execução federada do
Orca. Uma interface pequena para provedores de agentes e outra para runners
mantêm essa evolução possível sem trazer essas complexidades para o núcleo.

**REA — referência para ferramentas e evidências.** O REA não fornece o ciclo
de planejamento e execução do MAT: ele expõe ferramentas e workflows para um
agente cliente coordenar. A lição mais aplicável é manter ferramentas com
contratos explícitos e separar workflows, domínio e adaptadores. Para o MAT,
evidências devem ser registros estruturados e rastreáveis — vinculados ao run,
task, tentativa e agente — e não apenas texto livre em logs. A decisão de
continuar, repetir, replanejar ou pedir intervenção humana permanece no
orquestrador do MAT.

**Escopo recomendado.** Adotar os conceitos de identidade de execução,
worktrees por agente, estados recuperáveis, resultados estruturados e
evidências auditáveis. Não portar a infraestrutura de desktop, múltiplos
ambientes de execução, integrações especializadas ou ferramentas de análise
específicas dos projetos de referência. Essas partes aumentariam o escopo sem
atender aos requisitos centrais do MAT.

**Reutilização e cópias locais.** Os arquivos de licença examinados nas cópias
locais declaram MIT para ambos os projetos. Isso não substitui a verificação de
licenças de dependências e componentes de terceiros nem a preservação dos
avisos exigidos caso trechos sejam incorporados. Até haver uma decisão de
reutilização, o MAT deve implementar seus próprios contratos e comportamentos,
usando os repositórios apenas como referência. Neste checkout, as referências
foram consultadas no GitHub; cópias locais não são necessárias para o fluxo
atual.

## Qualidade de código

Como o MAT é um projeto Python, a estratégia padrão deve usar ferramentas
adequadas ao ecossistema, em vez de adicionar Husky/Node.js apenas para hooks.
As verificações devem incluir Ruff, pytest e cobertura, integradas ao fluxo de
desenvolvimento por hooks Python — por exemplo, `pre-commit` — e também ao CI.
Os gates do pipeline devem validar o projeto-alvo com as ferramentas apropriadas
àquele repositório, que pode usar outra linguagem.

## Acompanhamento e análise quantitativa

O painel operacional deve acompanhar fase, agente, duração, custos/tokens,
tentativas, erros, correções e resultados dos gates. Em evolução posterior, um
módulo visual separado analisará o histórico real de runs: distribuições e
correlações de custo, duração, sucesso, tentativas e falhas; heatmaps,
superfícies e projeções Monte Carlo com premissas e incerteza explícitas.
Projeções só serão exibidas quando houver histórico suficiente. A TUI atende ao
acompanhamento operacional; visualizações exploratórias 3D pertencem ao módulo
separado. Black–Scholes não faz parte do escopo atual. Ambos os módulos devem
reutilizar as mesmas fontes de telemetria, sem duplicar a lógica de coleta.

## Estado atual

O projeto é uma base Python 3.12 com CLI, configuração YAML validada com
Pydantic, persistência SQLite, modelos de dados, orquestração de pipeline,
sandbox e testes unitários e de integração.

A CLI atual executa uma simulação com agentes stub e informa que não altera o
repositório nem executa os gates reais. Portanto, o fluxo descrito acima é a
direção do produto. Como primeiro passo de rastreabilidade, chamadas do
Programador, Corretor, Tester convencional e Verificador já criam tarefas e
dispatches vinculados à tentativa, registram entradas e saídas estruturadas e
geram evidências de conclusão ou falha. A chamada do Analista ainda é
registrada como evidência do run, pois ocorre antes da criação da tentativa.

Falhas de agentes podem recomendar retry por meio de um erro estruturado do
adaptador. O orquestrador impõe um limite global configurável de retries na
mesma tarefa/dispatch; cada falha recomendada é registrada como evidência. Erros
sem recomendação de retry ou que esgotem o limite deixam o dispatch e a
tentativa em estado explícito e pausam o run para intervenção humana.

O campo `execution_mode` registra se uma run é `simulation`, `real` ou
`unknown` (execuções anteriores à migração). A CLI atual grava `simulation`;
um status `passed` nessa modalidade significa apenas que os stubs percorreram o
fluxo, não que o código do projeto foi validado.

Ainda precisam ser conectados o seletor de projeto/branch, conversa com anexos,
worktree por solicitação, Claude Code, descoberta segura de validações e revisão
humana. O protótipo de isolamento da CLI no WSL é pré-requisito para execução
real. TUI Textual, retomada, gVisor, testers paralelos e módulo quantitativo
também estão pendentes. A ordem está em [docs/roadmap.md](docs/roadmap.md).
