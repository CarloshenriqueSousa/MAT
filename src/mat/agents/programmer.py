"""Programmer Agent implementation with autonomous tool-use."""

from __future__ import annotations

from pathlib import Path

from mat.agents.llm import LLMClient
from mat.agents.loop import AgentLoop
from mat.engine.budget import AttemptBudget
from mat.models.contracts import Implementation, Plan
from mat.sandbox.docker_runner import DockerRunner
from mat.sandbox.worktree import WorktreeManager
from mat.settings import AgentProfile
from mat.tools.bash_executor import BashHandler
from mat.tools.definitions import BASH_TOOL_SCHEMA, TEXT_EDITOR_TOOL_SCHEMA
from mat.tools.text_editor import TextEditorHandler

DEFAULT_PROGRAMMER_SYSTEM_PROMPT = """Você é um Engenheiro de Software Sênior autônomo do pipeline MAT.
Seu papel é implementar o código de produção e escrever os testes automatizados da feature.

Regras Estritas:
1. Você recebe a especificação da task, o plano arquitetural do Analista e eventuais feedbacks de revisões anteriores.
2. Você possui duas ferramentas:
   - `text_editor`: para visualizar (view), criar (create) e editar (str_replace / insert) arquivos no repositório.
   - `bash`: para rodar comandos como `pytest` e `ruff` dentro do sandbox isolado.
3. Você DEVE escrever os testes da feature que comprovem que ela funciona, dentro do diretório de testes apropriado.
4. Antes de concluir, você DEVE rodar `pytest` e `ruff` via `bash` para garantir que todos os testes passem e não haja erros de linting.
5. Quando tudo estiver funcionando e verificado, explique brevemente as mudanças que você realizou e finalize seu turno.
"""


class ProgrammerAgent:
    def __init__(
        self,
        llm: LLMClient,
        profile: AgentProfile,
        worktree_path: Path,
        docker_runner: DockerRunner,
        worktree_manager: WorktreeManager,
        system_prompt: str = DEFAULT_PROGRAMMER_SYSTEM_PROMPT,
    ) -> None:
        self.llm = llm
        self.profile = profile
        self.worktree_path = worktree_path
        self.runner = docker_runner
        self.worktree_manager = worktree_manager
        self.system_prompt = system_prompt

        self.editor = TextEditorHandler(worktree_path)
        self.bash = BashHandler(worktree_path, docker_runner)

    async def implement(
        self,
        plan: Plan,
        task_description: str = "",
        feedback: str = "",
        budget: AttemptBudget | None = None,
    ) -> Implementation:
        """Executa o loop autônomo de implementação e testes."""
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

        user_prompt_parts = [
            f"# Tarefa a ser implementada:\n{task_description or plan.approach}\n",
            f"# Plano de Execução do Analista:\nAbordagem: {plan.approach}\nArquivos impactados: {plan.impacted_files}\n",
        ]
        if feedback:
            user_prompt_parts.append(
                f"# ATENÇÃO - Feedback da tentativa anterior a ser corrigido:\n{feedback}\n"
            )
        user_prompt_parts.append(
            "Inicie inspecionando os arquivos necessários, implemente o código e os testes, e valide com pytest e ruff."
        )

        user_prompt = "\n".join(user_prompt_parts)

        # Roda o loop ReAct
        await agent_loop.run(user_prompt=user_prompt, budget=budget)

        # Captura diff do worktree
        diff = await self.worktree_manager.get_diff(self.worktree_path)

        # Não deixe symlinks novos escaparem da evidência revisada/commitada.
        status = await self.worktree_manager.get_untracked_symlinks(self.worktree_path)
        if status:
            raise RuntimeError(
                "O worktree contém symlinks novos, que não são aceitos pelo fluxo seguro: "
                + ", ".join(status)
            )

        # Commita na branch do worktree
        commit_sha = await self.worktree_manager.commit_all(
            self.worktree_path, message=f"feat: implementa {plan.id}"
        )

        return Implementation(
            modified_files=plan.impacted_files,
            diff=diff,
            commit_sha=commit_sha,
        )
