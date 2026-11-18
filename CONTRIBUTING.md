# Contribuindo com o MAT

Normas de branches, commits, pull requests e releases. Valem para pessoas e para
agentes (Claude Code) que trabalham no repositório.

## Branches

| Branch | Papel | Quem escreve |
|---|---|---|
| `main` | Somente versões publicadas (releases). Cada commit em `main` corresponde a uma tag `vX.Y.Z` | PR de release vindo da `dev` |
| `dev` | Integração contínua do trabalho em andamento | PRs de feature/fix |
| `<tipo>/<descricao>` | Uma mudança lógica (fase, correção, documento) | Autor do PR |

- Toda mudança nasce de uma branch criada a partir da `dev` atualizada.
- Nome: `<tipo>/<descricao-curta-com-hifens>`, usando os mesmos tipos dos commits.
  Exemplos: `feat/fase-1-fabrica-agentes`, `fix/run-presa-em-running`, `docs/threat-model`.
- Nada é commitado diretamente em `main` ou `dev`.

## Commits — Conventional Commits 1.0.0

```text
<tipo>(<escopo opcional>)<! se quebra compatibilidade>: <descrição>

<corpo opcional: o porquê da mudança>

<rodapé opcional: BREAKING CHANGE:, Refs #n, Co-Authored-By:>
```

**Tipos**

| Tipo | Uso | Efeito na versão |
|---|---|---|
| `feat` | Nova funcionalidade | minor |
| `fix` | Correção de bug | patch |
| `perf` | Melhoria de desempenho | patch |
| `refactor` | Mudança interna sem alterar comportamento | — |
| `test` | Testes novos ou corrigidos | — |
| `docs` | Documentação e ADRs | — |
| `build` | Dependências, empacotamento, imagem Docker | — |
| `ci` | Pipelines (GitHub Actions, pre-commit) | — |
| `chore` | Manutenção que não se encaixa acima (inclui releases) | — |
| `revert` | Reverte um commit anterior | depende |

**Escopos sugeridos:** `engine`, `agents`, `sandbox`, `gate`, `db`, `cli`, `config`, `tools`, `docs`.

**Regras**

- Descrição em português, no imperativo, minúscula, sem ponto final, até ~72 caracteres:
  `fix(engine): passa feedback do corretor como argumento nomeado`.
- O corpo explica **por quê**, não repete o diff.
- Mudança incompatível: `!` após o tipo/escopo **e** rodapé `BREAKING CHANGE: <o que muda e como migrar>`.
- Referencie issues/PRs no rodapé (`Refs #3`).
- Commits feitos com ajuda do Claude Code levam o rodapé `Co-Authored-By:` correspondente.

## Pull requests

- **Destino:** sempre `dev` (exceto o PR de release, que vai para `main`).
- **Título:** segue Conventional Commits — é validado pela CI (`pr-title.yml`) e vira o
  commit final, porque os PRs para `dev` são integrados com **squash merge**.
- **Descrição:** usa o template (`.github/pull_request_template.md`): resumo, decisões
  (com ADRs de `docs/decisions.md`), mudanças, por quê, como usar, testes e pendências.
  Diga explicitamente o que **não** foi validado (ex.: testes Docker fora do host Linux).
- **Tamanho:** uma mudança lógica por PR. Fases grandes viram vários PRs.
- **Antes de pedir revisão:** Ruff e pytest passando localmente e CI verde.
- **Decisões de arquitetura** entram como nova ADR em `docs/decisions.md` no mesmo PR.
- **Merge:** feito por uma pessoa responsável. Agentes abrem PRs, mas não fazem merge
  nem force-push.

```bash
uv run --python 3.12 --extra dev ruff check .
uv run --python 3.12 --extra dev pytest -q
```

## Releases — SemVer

A `main` só avança por releases. Versão `MAJOR.MINOR.PATCH`; enquanto estiver em `0.x`,
mudanças incompatíveis sobem o **minor** e correções sobem o **patch**.

1. Na `dev` atualizada, crie `release/vX.Y.Z`.
2. Atualize a versão em `pyproject.toml` e `src/mat/__init__.py` (`__version__`).
3. Abra o PR `chore(release): vX.Y.Z` de `release/vX.Y.Z` para **`main`**, com as notas
   da versão agrupadas por tipo (Funcionalidades, Correções, Mudanças incompatíveis, Outros).
4. Integre com **merge commit** (não squash), preservando o histórico da `dev`.
5. Crie a tag e a release no commit de merge da `main`:

   ```bash
   git tag -a vX.Y.Z -m "vX.Y.Z" <commit-de-merge> && git push origin vX.Y.Z
   gh release create vX.Y.Z --title "vX.Y.Z" --notes-file notas.md
   ```

6. Traga a `main` de volta para a `dev` (PR `chore(release): sincroniza main em dev`)
   para que o bump de versão também esteja na `dev`.

Correção urgente em produção: branch `fix/...` a partir da `main`, release de patch
`vX.Y.Z+1` seguindo os passos 2–6.
