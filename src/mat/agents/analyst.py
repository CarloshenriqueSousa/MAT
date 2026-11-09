"""Analyst Agent: inspects project, measures baseline, and generates technical execution plan."""

from __future__ import annotations

import json
from pathlib import Path

from mat.agents.llm import LLMClient
from mat.decision import DecisionEngine
from mat.gate.baseline import BaselineMeasurer
from mat.models.contracts import Baseline, Plan, TestScenario
from mat.settings import AgentProfile
from mat.tools.text_editor import TextEditorHandler

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


class AmbiguousTaskError(ValueError):
    """Raised when task cannot be reliably planned without human clarification."""


class AnalystAgent:
    def __init__(
        self,
        llm: LLMClient,
        profile: AgentProfile,
        worktree_path: Path,
        baseline_measurer: BaselineMeasurer,
        decision_engine: DecisionEngine | None = None,
        system_prompt: str = DEFAULT_ANALYST_SYSTEM_PROMPT,
    ) -> None:
        self.llm = llm
        self.profile = profile
        self.worktree_path = worktree_path
        self.measurer = baseline_measurer
        self.decision = decision_engine or DecisionEngine(llm, profile.model)
        self.system_prompt = system_prompt
        self.editor = TextEditorHandler(worktree_path)

    async def analyze(
        self, run_id: str, plan_index: int, task_description: str
    ) -> tuple[Plan, Baseline]:
        """Executa medição do baseline e geração do plano estruturado."""
        # 1. Mede baseline antes de qualquer alteração
        baseline = await self.measurer.measure(run_id, self.worktree_path)

        # 2. Coleta lista de arquivos existentes no projeto
        existing_files = [
            str(p.relative_to(self.worktree_path))
            for p in self.worktree_path.glob("**/*")
            if p.is_file() and not any(part.startswith(".") for part in p.parts)
        ][:50]

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

        # Parse do JSON do plano
        clean_json = text
        if "```json" in text:
            clean_json = text.split("```json")[1].split("```")[0].strip()
        elif "{" in text and "}" in text:
            start = text.find("{")
            end = text.rfind("}") + 1
            clean_json = text[start:end]

        try:
            data = json.loads(clean_json)
        except (json.JSONDecodeError, ValueError):
            data = {
                "approach": text,
                "risk_level": "medium",
                "impacted_files": [],
                "test_scenarios": [{"name": "basic_verification", "type": "positive"}],
            }

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

        plan = Plan(
            id=f"plan_{run_id}_v{plan_index}",
            run_id=run_id,
            plan_index=plan_index,
            approach=data.get("approach", "Implementação padrão"),
            risk_level=data.get("risk_level", "low"),
            impacted_files=data.get("impacted_files", []),
            test_scenarios=scenarios,
        )

        return plan, baseline
