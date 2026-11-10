# MAT — Multi-Agent Tenant

Orquestrador multiagente para transformar tasks em alterações de código e testes,
com supervisão humana antes da aprovação final e da publicação manual de um PR.

> **Status:** em desenvolvimento. Este documento descreve tanto o estado atual
> quanto as decisões para a arquitetura desejada; itens futuros não devem ser
> confundidos com funcionalidades já implementadas.

## Visão do produto

O MAT recebe uma task em texto livre, com backlog, imagens e arquivos opcionais,
e coordena agentes com responsabilidades separadas. A pessoa usuária aprova o
resultado antes que ele seja publicado como PR.

```text
Task → Analista → Programador ↔ Corretor
     → Testers (convencional, burlar e quebrar; em paralelo)
     → Consolidador → Verificador → Aprovação humana → PR
```

1. **Analista:** interpreta a task e os anexos e produz um plano com tarefas e
   dependências.
2. **Programador e Corretor:** o Programador implementa o plano; o Corretor
   revisa e aponta problemas, sem editar o código. O ciclo se repete até atender
   aos critérios de qualidade e ao objetivo original.
3. **Testers:** três agentes — convencional, burlar e quebrar — produzem e
   executam testes em paralelo. Um worktree de teste parte de uma fotografia da
   implementação; cada tester cria seu próprio worktree derivado e trabalha
   isoladamente, de acordo com seu papel e com os requisitos da task. O
   Consolidador avalia e integra os testes aceitos no worktree de entrega. Os
   testes aprovados fazem parte da entrega; uma falha pode devolver o trabalho
   ao ciclo de programação e revisão.
4. **Consolidador e Verificador:** o Consolidador reúne os resultados dos
   testers. O Verificador avalia as evidências e compara o diff com o objetivo
   original. Sua decisão deve usar um destino estruturado entre rotas
   predefinidas, com justificativa e evidências; não pode inventar etapas nem
   controlar o fluxo por texto livre:
   - **Programação/revisão:** corrigir o diff mantendo o plano atual;
   - **Análise:** revisar o plano quando o escopo ou os requisitos estiverem
     incorretos ou incompletos;
   - **Intervenção humana:** pausar quando houver ambiguidade ou decisão que
     exija a pessoa responsável;
   - **Encerrar como reprovado:** parar quando não houver caminho seguro ou
     válido para continuar.
5. **Aprovação humana:** a pessoa responsável inspeciona o diff, os testes e os
   alertas. O MAT prepara a entrega, mas não cria nem publica o PR; essa etapa
   continua manual. O MAT também não cria cards no Teams. Pode preparar
   sugestões para ambos, mas a criação é sempre feita pela pessoa responsável.
   A aprovação também não cria um commit: o worktree fica com as alterações
   disponíveis para revisão e commit manual.

O MAT deve pausar e pedir orientação quando houver ambiguidade relevante. Não
deve publicar alterações sem aprovação humana.

## Princípios e decisões de arquitetura

- **Papéis separados:** quem implementa não revisa nem escreve os testes.
- **Execução isolada:** a edição fica restrita ao worktree; comandos e testes
  rodam isolados e sem rede.
- **Workspace por task:** cada execução usa uma branch e um Git worktree
  exclusivos, baseados no commit escolhido. O checkout original não é editado e
  mudanças locais não são incorporadas automaticamente.
- **Retenção do workspace:** manter branches e worktrees após a execução,
  inclusive em falhas, até a pessoa responsável solicitar explicitamente a
  limpeza.
- **Retomada:** execuções interrompidas ficam pausadas no último checkpoint
  válido e só continuam mediante comando explícito da pessoa responsável.
- **Baseline com falhas:** falhas já existentes devem gerar trabalho de
  remediação em runs e worktrees separados, baseados na falha identificada. Esse
  trabalho percorre o mesmo pipeline e não mistura seu diff com a task original.
  A task original permanece independente. O MAT pode preparar sugestões de
  card no Teams e de PR para as correções, mas não cria nenhum deles.
- **Runner substituível:** usar Docker com gVisor (`runsc`) como isolamento
  padrão. Manter a interface do runner independente da implementação para
  permitir avaliar microVMs ou outra tecnologia no futuro.
- **Rede por etapa:** permitir rede apenas na preparação de dependências.
  Execução do código e testes ocorre sem rede.
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
arquitetura e implementação:

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
usando os repositórios apenas como referência. As pastas locais `Orca/` e
`REA/` estão dentro do diretório do MAT e não são repositórios Git independentes
neste checkout; mantenha-as fora do conteúdo versionado e evite adicioná-las
acidentalmente.

## Qualidade de código

Como o MAT é um projeto Python, a estratégia padrão deve usar ferramentas
adequadas ao ecossistema, em vez de adicionar Husky/Node.js apenas para hooks.
As verificações devem incluir Ruff, pytest e cobertura, integradas ao fluxo de
desenvolvimento por hooks Python — por exemplo, `pre-commit` — e também ao CI.
Os gates do pipeline devem validar o projeto-alvo com as ferramentas apropriadas
àquele repositório, que pode usar outra linguagem.

## Acompanhamento e análise quantitativa

O MAT deve oferecer painéis de acompanhamento de custos, tentativas e resultados
de testes. Também deve poder disponibilizar um módulo separado de análise
quantitativa. Os dois usos devem compartilhar a mesma implementação e as mesmas
fontes de dados, evitando lógica duplicada.

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

Ainda precisam ser implementados e integrados a execução real de agentes, o
runner gVisor, os três testers em paralelo, a persistência do dispatch de
análise, os painéis, o módulo quantitativo e o fluxo de aprovação final.
Criação e publicação de PR permanecem manuais por decisão de escopo.
