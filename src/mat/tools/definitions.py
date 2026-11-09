"""Anthropic tool schemas for text_editor and bash tools."""

from __future__ import annotations

from typing import Any

TEXT_EDITOR_TOOL_SCHEMA: dict[str, Any] = {
    "name": "text_editor",
    "description": (
        "Ferramenta segura de edição de arquivos no repositório. "
        "Suporta: view (com numeração de linhas), create (criação de novo arquivo), "
        "str_replace (substituição exata com verificação de ocorrência única) e insert (inserção em linha específica)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "enum": ["view", "create", "str_replace", "insert"],
                "description": "Comando a ser executado.",
            },
            "path": {
                "type": "string",
                "description": "Caminho relativo do arquivo a partir da raiz do repositório.",
            },
            "file_text": {
                "type": "string",
                "description": "Conteúdo para criação (obrigatório se command == 'create').",
            },
            "old_str": {
                "type": "string",
                "description": "String exata a ser substituída (obrigatório se command == 'str_replace').",
            },
            "new_str": {
                "type": "string",
                "description": "Nova string de substituição ou inserção.",
            },
            "view_range": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Intervalo opcional de linhas [início, fim] para visualização.",
            },
            "insert_line": {
                "type": "integer",
                "description": "Número da linha após a qual inserir o novo texto (se command == 'insert').",
            },
        },
        "required": ["command", "path"],
    },
}

BASH_TOOL_SCHEMA: dict[str, Any] = {
    "name": "bash",
    "description": (
        "Executa comandos bash (como pytest, ruff, etc.) dentro do container sandbox isolado. "
        "Acesso à rede é totalmente bloqueado (--network none). "
        "Use para rodar testes, verificar linting ou inspecionar arquivos."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Comando bash a ser executado no container sandbox.",
            }
        },
        "required": ["command"],
    },
}
