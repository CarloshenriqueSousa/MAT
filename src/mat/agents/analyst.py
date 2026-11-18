"""Analyst Agent: inspects project, measures baseline, and generates technical execution plan."""

from __future__ import annotations

import json
import os
from pathlib import Path

from mat.agents.errors import AgentOutputError, AmbiguousTaskError
from mat.agents.llm import LLMClient
from mat.agents.parsing import extract_json_object
from mat.gate.baseline import BaselineMeasurer
from mat.models.contracts import Baseline, Plan, TestScenario
from mat.settings import AgentProfile

__all__ = ["AmbiguousTaskError", "AnalystAgent", "list_project_files"]

MAX_LISTED_FILES = 200
_SKIPPED_DIRS = {"node_modules", "__pycache__", "venv", "build", "dist"}

DEFAULT_ANALYST_SYSTEM_PROMPT = """Você é o Arquiteto de Software e Analista Sênior do pipeline MAT.
Seu papel é analisar a base de código, avaliar o impacto da tarefa solicitada e gerar um Plano de Execução estruturado e sem ambiguidades.

Instruções:
1. Examine os arquivos existentes no repositório.
2. Identifique quais arquivos precisarão ser criados ou modificados.
3. Desenhe cenários de teste necessários (positivos, adversariais e destrutivos).
4. Responda ESTRITAMENTE em formato JSON com o seguinte schema:
```json
{
  "approach": "Explicação técnica detalhada da abordagem recomendada",
  "risk_level": "low" | "medium" | "high",
  "impacted_files": ["src/arquivo.py", "tests/test_arquivo.py"],
  "test_scenarios": [
    {"name": "teste_happy_path", "type": "positive", "description": "validação da funcionalidade principal"}
  ],
  "is_ambiguous": false,
  "ambiguity_reason": null
}
```
Se a tarefa for contraditória ou excessivamente ambígua, marque "is_ambiguous": true com a razão.
"""


def list_project_files(root: Path, limit: int = MAX_LISTED_FILES) -> list[str]:
    """List project files relative to ``root``, skipping hidden and generated dirs.

    Only path components *below* ``root`` are filtered, so a worktree living under
    a hidden directory (e.g. ``~/.mat/worktrees``) is still listed.
    """
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if not d.startswith(".") and d not in _SKIPPED_DIRS
        )
        rel_dir = Path(dirpath).relative_to(root)
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            files.append((rel_dir / name).as_posix())
            if len(files) >= limit:
                return files
    return files


class AnalystAgent:
    def __init__(
        self,
        llm: LLMClient,
        profile: AgentProfile,
        worktree_path: Path,
        baseline_measurer: BaselineMeasurer,
        system_prompt: str = DEFAULT_ANALYST_SYSTEM_PROMPT,
    ) -> None:
        self.llm = llm
        self.profile = profile
        self.worktree_path = worktree_path
        self.measurer = baseline_measurer
        self.system_prompt = system_prompt

    async def analyze(
        self, run_id: str, plan_index: int, task_description: str
    ) -> tuple[Plan, Baseline]:
        """Executa medição do baseline e geração do plano estruturado."""
        # 1. Mede baseline antes de qualquer alteração
        baseline = await self.measurer.measure(run_id, self.worktree_path)

        # 2. Coleta lista de arquivos existentes no projeto
        existing_files = list_project_files(self.worktree_path)

        user_prompt = f"""# Tarefa Solicitada:
{task_description}

# Arquivos Existentes no Repositório:
{json.dumps(existing_files, indent=2)}

# Métricas Iniciais do Projeto (Baseline):
- Testes passando: {baseline.pytest_passed}/{baseline.pytest_total}
- Cobertura atual: {baseline.coverage_pct}%
- Lint limpo: {baseline.ruff_clean}

Gere o plano de execução técnico em JSON.
"""

        response = await self.llm.call(
            model=self.profile.model,
            messages=[{"role": "user", "content": user_prompt}],
            system=self.system_prompt,
            temperature=self.profile.temperature,
            max_tokens=self.profile.max_tokens,
        )

        text = "".join(getattr(b, "text", "") for b in response.content)

        data = extract_json_object(text)
        if data is None:
            # Nunca inventar um plano: sem JSON válido, a decisão volta para o humano.
            raise AgentOutputError("Analista não retornou um plano JSON válido.")

        if data.get("is_ambiguous", False):
            raise AmbiguousTaskError(data.get("ambiguity_reason", "Tarefa ambígua."))

        scenarios = [
            TestScenario(
                name=s.get("name", f"scenario_{i}"),
                type=s.get("type", "positive"),
                description=s.get("description", ""),
            )
            for i, s in enumerate(data.get("test_scenarios", []))
        ]

        approach = data.get("approach")
        if not isinstance(approach, str) or not approach.strip():
            raise AgentOutputError("Plano do Analista não contém 'approach'.")

        plan = Plan(
            id=f"plan_{run_id}_v{plan_index}",
            run_id=run_id,
            plan_index=plan_index,
            approach=approach,
            risk_level=data.get("risk_level", "low"),
            impacted_files=data.get("impacted_files", []),
            test_scenarios=scenarios,
        )

        return plan, baseline
