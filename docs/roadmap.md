# MAT — Roadmap

## Fatia Fina (atual)

Ordem estrita — cada item é pré-requisito do próximo.

| Passo | O quê | Status |
|---|---|---|
| 1 | Esqueleto: `pyproject.toml`, pacotes `src/mat/`, configs, `.env.example`, `.gitignore` | ✅ Concluído |
| 2 | `schema.py` + `connection.py` com reader/writer separados, lock de escrita, WAL | ✅ Concluído |
| 3 | `settings.py` (Pydantic) carregando `mat.yaml` + `agents.yaml` | ✅ Concluído |
| 4 | `conftest.py` + `tests/unit/test_db.py` — 12 testes, ruff limpo | ✅ Concluído |
| 5 | State machine de fases e attempts, com agentes stub | ✅ Concluído |
| 6 | Sandbox Docker: subir container, rodar `pytest` com `--network none` | ✅ Concluído |
| 7 | Gate determinístico: pytest exit code, ruff, cobertura contra baseline + anti-cheat | ✅ Concluído |
| 8 | CLI mínima (`mat run`, `mat report`) rodando task simples ponta a ponta com stubs | ✅ Concluído |
| 9 | Plugar LLM: Programador (loop tool-use com SDK anthropic, text_editor no host e bash no container) | ✅ Concluído |
| 10 | Plugar LLM: Corretor (review-only, aponta problemas, loop por tokens) | ✅ Concluído |
| 11 | Plugar LLM: Analista (scan de projeto + plano + baseline) | 🔨 Próximo |
| 12 | Plugar LLM: Tester Happy Path (valida feature em container) | ⬜ |
| 13 | Plugar LLM: Verificador Final (pass/fail por critério, evidência bruta) | ⬜ |
| 14 | TUI mínimo (Textual): supervisionar e aprovar cada fase | ⬜ |
| 15 | `mat report`: custo por run, tokens, duração, resultado | ⬜ |

## Fase 2 — Hardening

| Item | Detalhe |
|---|---|
| Tester Bypass | Agente adversário que tenta burlar caminhos |
| Tester Breaker | Agente de caos com fuzzing |
| Consolidador | Sintetiza resultados e seleciona testes para o commit |
| Mutation gate | `mutmut` nos arquivos alterados, threshold por criticidade |
| RAG de PRs | FastEmbed local + indexação incremental |
| Majority voting | Verificador rodando N vezes onde suspeitar |
| Worktree isolation | Git worktree por run para paralelismo |

## Fase 3 — Evolução

| Item | Detalhe |
|---|---|
| Daemon Java | Supervisor da máquina, banco centralizado |
| Web UI | React + WebSocket para dashboard |
| Módulo quantitativo | Heatmaps, Monte Carlo, superfícies, Black-Scholes style |
| Integração JEV real | Quando API disponível |
| Meta-teste | MAT testando a si mesmo |
| Multi-repo | Suporte a projetos Java, TypeScript |
