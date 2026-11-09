"""Final Verifier Agent: Evaluates diff, original objective, test results, and raw evidence."""

from __future__ import annotations

import json

from mat.agents.llm import LLMClient
from mat.models.contracts import (
    DeterministicGateResult,
    Implementation,
    Plan,
    TestResult,
    Verdict,
)
from mat.settings import AgentProfile

DEFAULT_VERIFIER_SYSTEM_PROMPT = """Você é a Autoridade Final de Release e Verificação do pipeline MAT.
Seu papel é decidir de forma rigorosa se a alteração de código está pronta para aprovação (Go) ou se deve ser reprovada (No-Go).

Regras de Avaliação (Baseadas no estudo MAST NeurIPS 2025):
1. Verificadores superficiais falham ao checar apenas se o código "compilou" ou se testes passaram. Você DEVE inspecionar o DIFF linha por linha contra o OBJETIVO ORIGINAL.
2. Critérios Obrigatórios:
   - `meets_objective`: O diff atende inteiramente ao que foi pedido pelo usuário na tarefa?
   - `test_coverage_valid`: Os testes adicionados pelo Programador testam assertivamente a nova lógica (sem testes falsos ou assertions vazias)?
   - `no_unintended_changes`: Não há remoções indevidas, código de debug ou regressões na arquitetura?
   - `gate_and_tests_passed`: Os testes determinísticos e os testes convencionais passaram?
3. Se TODOS os critérios forem atendidos, aprove. Se qualquer critério falhar, reprove e forneça a razão exata citando as linhas e evidências.
4. Responda ESTRITAMENTE em formato JSON:
```json
{
  "approved": true | false,
  "criteria": {
    "meets_objective": true | false,
    "test_coverage_valid": true | false,
    "no_unintended_changes": true | false,
    "gate_and_tests_passed": true | false
  },
  "evidence_cited": ["ev_diff", "ev_pytest"],
  "rationale": "Explicação detalhada da aprovação ou dos pontos que causaram a reprovação."
}
```
"""


class VerifierAgent:
    def __init__(
        self,
        llm: LLMClient,
        profile: AgentProfile,
        system_prompt: str = DEFAULT_VERIFIER_SYSTEM_PROMPT,
    ) -> None:
        self.llm = llm
        self.profile = profile
        self.system_prompt = system_prompt

    async def verify(
        self,
        plan: Plan,
        impl: Implementation,
        gate_res: DeterministicGateResult,
        test_res: TestResult,
        task_description: str,
    ) -> Verdict:
        """Avalia se a alteração atende aos critérios formais de aprovação."""
        # Se os testes determinísticos ou o tester convencional falharam, reprova imediatamente
        if not gate_res.passed or not test_res.passed:
            return Verdict(
                approved=False,
                criteria={
                    "meets_objective": True,
                    "test_coverage_valid": False,
                    "no_unintended_changes": True,
                    "gate_and_tests_passed": False,
                },
                evidence_cited=["gate_failure", "test_failure"],
                rationale=f"Reprovação automática por falha em testes: {gate_res.error_message or test_res.raw_output}",
            )

        user_prompt = f"""# Tarefa Original:
{task_description}

# Plano de Arquitetura:
Abordagem: {plan.approach}
Arquivos previstos: {plan.impacted_files}

# Diff Real das Mudanças:
{impl.diff}

# Resultados dos Testes:
- Gate determinístico: {gate_res.pytest_passed} testes passados, cobertura {gate_res.coverage_pct}%
- Tester convencional: {test_res.tests_passed}/{test_res.tests_run} testes passados

Avalie com rigor e retorne sua decisão em JSON.
"""

        response = await self.llm.call(
            model=self.profile.model,
            messages=[{"role": "user", "content": user_prompt}],
            system=self.system_prompt,
            temperature=self.profile.temperature,
            max_tokens=self.profile.max_tokens,
        )

        text = "".join(getattr(b, "text", "") for b in response.content)

        clean_json = text
        if "```json" in text:
            clean_json = text.split("```json")[1].split("```")[0].strip()
        elif "{" in text and "}" in text:
            start = text.find("{")
            end = text.rfind("}") + 1
            clean_json = text[start:end]

        approved = False
        criteria = {}
        evidence_cited = []
        rationale = text

        try:
            data = json.loads(clean_json)
            approved = bool(data.get("approved", False))
            criteria = dict(data.get("criteria", {}))
            evidence_cited = list(data.get("evidence_cited", []))
            rationale = str(data.get("rationale", rationale))
        except (json.JSONDecodeError, ValueError):
            pass

        return Verdict(
            approved=approved,
            criteria=criteria,
            evidence_cited=evidence_cited,
            rationale=rationale,
        )
