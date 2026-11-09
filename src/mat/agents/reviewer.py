"""Reviewer Agent (Corretor): inspects code, runs validation, and provides feedback without editing."""

from __future__ import annotations

import json
from pathlib import Path

from mat.agents.llm import LLMClient
from mat.agents.loop import AgentLoop
from mat.engine.budget import AttemptBudget
from mat.models.contracts import Implementation, Plan, ReviewResult
from mat.sandbox.docker_runner import DockerRunner
from mat.settings import AgentProfile
from mat.tools.bash_executor import BashHandler
from mat.tools.definitions import BASH_TOOL_SCHEMA, TEXT_EDITOR_TOOL_SCHEMA
from mat.tools.text_editor import TextEditorHandler

DEFAULT_REVIEWER_SYSTEM_PROMPT = """Você é o Agente Corretor (Reviewer) do pipeline MAT.
Seu papel é verificar com rigor o trabalho do Programador antes que o código prossiga para a fase de testes.

Regras Estritas:
1. Você NÃO deve alterar o código. Seu papel é inspecionar, executar testes e apontar problemas detalhados.
2. Você considera o código APROVADO se, e somente se:
   - `pytest` passa com 100% de sucesso.
   - `ruff check .` não encontra nenhum erro de linting ou formatação.
   - O diff e a implementação atendem fielmente ao objetivo original da tarefa solicitada.
   - Foram incluídos testes adequados cobrindo a nova funcionalidade.
3. Se houver falhas de teste, lint ou desvios do objetivo, aponte cada issue de forma clara e acionável.
4. Na sua mensagem final, retorne um bloco JSON no formato:
```json
{
  "approved": true | false,
  "issues": ["problema 1", "problema 2"],
  "meets_task_objective": true | false,
  "feedback": "Resumo das instruções para o Programador"
}
```
"""


class ReviewerAgent:
    def __init__(
        self,
        llm: LLMClient,
        profile: AgentProfile,
        worktree_path: Path,
        docker_runner: DockerRunner,
        system_prompt: str = DEFAULT_REVIEWER_SYSTEM_PROMPT,
    ) -> None:
        self.llm = llm
        self.profile = profile
        self.worktree_path = worktree_path
        self.runner = docker_runner
        self.system_prompt = system_prompt

        self.editor = TextEditorHandler(worktree_path)
        self.bash = BashHandler(worktree_path, docker_runner)

    async def review(
        self,
        plan: Plan,
        impl: Implementation,
        task_description: str,
        budget: AttemptBudget | None = None,
    ) -> ReviewResult:
        """Inspeciona o código, executa validações e decide aprovação."""
        # Corretor tem acesso a ferramentas de leitura e execução de testes
        tools = [TEXT_EDITOR_TOOL_SCHEMA, BASH_TOOL_SCHEMA]
        dispatchers = {
            "text_editor": self.editor.execute,
            "bash": self.bash.execute,
        }

        agent_loop = AgentLoop(
            llm=self.llm,
            model=self.profile.model,
            system_prompt=self.system_prompt,
            tools=tools,
            tool_dispatchers=dispatchers,
            temperature=self.profile.temperature,
            max_tokens_per_turn=min(self.profile.max_tokens, 4000),
        )

        user_prompt = f"""# Tarefa Original:
{task_description}

# Plano do Analista:
{plan.approach}

# Diff Implementado:
{impl.diff}

Execute `pytest` e `ruff` via ferramenta `bash`, inspecione os arquivos alterados e retorne seu veredito em JSON.
"""
        result = await agent_loop.run(user_prompt=user_prompt, budget=budget, max_turns=10)

        # Parse do resultado JSON na mensagem final
        approved = False
        issues = []
        meets_objective = False
        feedback = result.final_text

        # Tenta extrair JSON delimitado
        text = result.final_text
        if "```json" in text:
            raw_json = text.split("```json")[1].split("```")[0].strip()
        elif "{" in text and "}" in text:
            start = text.find("{")
            end = text.rfind("}") + 1
            raw_json = text[start:end]
        else:
            raw_json = ""

        if raw_json:
            try:
                data = json.loads(raw_json)
                approved = bool(data.get("approved", False))
                issues = list(data.get("issues", []))
                meets_objective = bool(data.get("meets_task_objective", False))
                feedback = str(data.get("feedback", feedback))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                pass

        return ReviewResult(
            approved=approved,
            issues=issues,
            meets_task_objective=meets_objective,
            feedback=feedback,
        )
