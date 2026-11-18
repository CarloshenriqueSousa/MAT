# MAT — instruções para o Claude Code

Orquestrador de agentes em Python 3.12 (loop próprio com SDK `anthropic`, execução em
sandbox Docker). Visão e estado: `README.md`; decisões: `docs/decisions.md`;
ordem de entrega: `docs/roadmap.md`; segurança: `docs/threat-model.md`.

## Fluxo de trabalho (obrigatório)

Siga `CONTRIBUTING.md`. Em resumo:

- Toda mudança: branch `<tipo>/<descricao>` a partir de `origin/dev` atualizada.
- Commits e títulos de PR em Conventional Commits, descrição em português.
- Abra o PR para `dev` com o template preenchido (decisões, mudanças, por quê, como usar,
  testes e o que não foi validado). Acompanhe a CI e corrija falhas na mesma branch.
- Nunca faça merge, force-push nem commit direto em `main`/`dev`. A `main` só recebe releases.
- Decisão de arquitetura nova → ADR em `docs/decisions.md` no mesmo PR.

## Comandos

```bash
uv run --python 3.12 --extra dev ruff check .
uv run --python 3.12 --extra dev pytest -q
uv run --python 3.12 mat run "descrição da task"   # simulação com stubs
```

Testes de integração Docker são pulados sem Docker ou sem a imagem `mat-sandbox`
(`docker/build.sh`); o runtime real é um host Linux com Docker (D49).

## Regras do código

- Não simular sucesso: falha ou ambiguidade vai para `awaiting_human`, nunca para um
  resultado inventado.
- Código do repositório-alvo nunca executa no host; só no container (D16/D17).
- Contratos entre agentes são modelos Pydantic (`src/mat/models/contracts.py`).
- Modelos de LLM vêm de `configs/agents.yaml`, nunca fixos no código.
