"""Decision module (JEV-like) for the Analyst: structured Choice and Score without open-ended text."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from mat.agents.llm import LLMClient


@dataclass
class ChoiceResult:
    selected: str
    distribution: dict[str, float] = field(default_factory=dict)
    rationale: str = ""


@dataclass
class ScoreResult:
    score: float
    min_score: float = 0.0
    max_score: float = 10.0
    rationale: str = ""


class DecisionEngine:
    def __init__(self, llm: LLMClient, model: str) -> None:
        self.llm = llm
        self.model = model

    async def choose(
        self,
        question: str,
        options: list[str],
        context: str = "",
    ) -> ChoiceResult:
        """Evaluates state against predefined discrete options and returns structured decision."""
        system = """Você é um motor de decisão estatístico e estruturado (inspirado no modelo JEV).
Avalie as opções fornecidas e responda ESTRITAMENTE em formato JSON:
{
  "selected": "opcao_escolhida",
  "distribution": {"opcao1": 0.7, "opcao2": 0.3},
  "rationale": "Justificativa técnica objetiva"
}
"""
        user_prompt = f"Contexto:\n{context}\n\nPergunta: {question}\nOpções disponíveis: {options}"

        response = await self.llm.call(
            model=self.model,
            messages=[{"role": "user", "content": user_prompt}],
            system=system,
            temperature=0.05,
            max_tokens=1000,
        )

        text = "".join(getattr(b, "text", "") for b in response.content)
        # Parse JSON
        selected = options[0] if options else ""
        distribution = {opt: 1.0 / len(options) for opt in options} if options else {}
        rationale = text

        try:
            clean_json = text[text.find("{") : text.rfind("}") + 1]
            data = json.loads(clean_json)
            selected = data.get("selected", selected)
            distribution = data.get("distribution", distribution)
            rationale = data.get("rationale", rationale)
        except (json.JSONDecodeError, ValueError):
            pass

        return ChoiceResult(selected=selected, distribution=distribution, rationale=rationale)

    async def score(
        self,
        question: str,
        context: str = "",
        min_score: float = 0.0,
        max_score: float = 10.0,
    ) -> ScoreResult:
        """Evaluates quantitative risk or complexity score."""
        system = """Você é um motor de avaliação quantitativa estruturado.
Responda ESTRITAMENTE em JSON:
{
  "score": 4.5,
  "rationale": "Justificativa da nota"
}
"""
        user_prompt = (
            f"Contexto:\n{context}\n\nPergunta: {question}\nEscala: de {min_score} a {max_score}"
        )

        response = await self.llm.call(
            model=self.model,
            messages=[{"role": "user", "content": user_prompt}],
            system=system,
            temperature=0.05,
            max_tokens=1000,
        )

        text = "".join(getattr(b, "text", "") for b in response.content)
        score_val = (min_score + max_score) / 2.0
        rationale = text

        try:
            clean_json = text[text.find("{") : text.rfind("}") + 1]
            data = json.loads(clean_json)
            score_val = float(data.get("score", score_val))
            rationale = data.get("rationale", rationale)
        except (json.JSONDecodeError, ValueError):
            pass

        return ScoreResult(
            score=score_val, min_score=min_score, max_score=max_score, rationale=rationale
        )
