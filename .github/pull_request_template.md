<!--
Título do PR: Conventional Commits, descrição em português e minúscula.
  ex.: feat(agents): adiciona fábrica de agentes a partir do agents.yaml
O título vira o commit final na dev (squash). Normas completas: CONTRIBUTING.md
-->

## Resumo

<!-- O que este PR entrega, em 2–3 linhas. -->

## Decisões

<!-- Escolhas feitas e alternativas descartadas. Cite as ADRs (docs/decisions.md) criadas ou alteradas. -->

## Mudanças

<!-- O que mudou, agrupado por área (engine, agents, sandbox, gate, db, cli, config, docs, ci). -->

## Por quê

<!-- Problema, bug ou requisito que motivou cada mudança relevante. -->

## Como usar

<!-- Comandos, configuração nova (mat.yaml / agents.yaml / variáveis de ambiente) e exemplos. Escreva "Sem mudança de uso" se for o caso. -->

## Testes

<!-- Testes adicionados/alterados, como rodar e resultado. -->

```bash
uv run --python 3.12 --extra dev ruff check .
uv run --python 3.12 --extra dev pytest -q
```

**Não validado:** <!-- o que ficou sem verificação e por quê (ex.: depende do host Linux com Docker). -->

## Pendências / próximos passos

<!-- O que fica para depois; links para issues/PRs. -->

## Checklist

- [ ] Branch criada a partir da `dev` atualizada, com nome `<tipo>/<descricao>`
- [ ] Título no padrão Conventional Commits
- [ ] Ruff e pytest passando localmente; CI verde
- [ ] Testes novos cobrem o comportamento alterado
- [ ] Nova decisão de arquitetura registrada como ADR em `docs/decisions.md`
- [ ] README/docs atualizados quando o uso ou o estado do projeto mudou
- [ ] Mudança incompatível sinalizada com `!` e explicada em "Como usar"
