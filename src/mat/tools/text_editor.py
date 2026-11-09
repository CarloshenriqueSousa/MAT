"""Host-side secure text editor tool.

Enforces:
- Path traversal prevention (rejects '..' or symlinks escaping worktree)
- Exact single-occurrence requirement for str_replace
- View with line numbers
- Insert and create operations
"""

from __future__ import annotations

from pathlib import Path


class PathSecurityError(ValueError):
    """Raised when a path attempts to escape the worktree."""


class TextEditorHandler:
    def __init__(self, worktree_root: Path) -> None:
        self.worktree_root = worktree_root.resolve()

    def _resolve_safe_path(self, rel_path: str) -> Path:
        """Resolve path and ensure it is strictly within worktree_root."""
        raw_path = Path(rel_path)
        if raw_path.is_absolute() or rel_path.startswith(("/", "\\")):
            raise PathSecurityError(
                f"Violação de segurança: caminhos absolutos não são permitidos: '{rel_path}'."
            )

        target = (self.worktree_root / raw_path).resolve()

        try:
            target.relative_to(self.worktree_root)
        except ValueError as exc:
            raise PathSecurityError(
                f"Violação de segurança: o caminho '{rel_path}' escapa do worktree."
            ) from exc

        return target

    def view(self, path: str, view_range: list[int] | None = None) -> str:
        """View file content, optionally within line range [start, end] (1-indexed)."""
        file_path = self._resolve_safe_path(path)
        if not file_path.exists():
            return f"Erro: o arquivo '{path}' não existe."
        if not file_path.is_file():
            return f"Erro: '{path}' é um diretório, não um arquivo."

        lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()

        if view_range and len(view_range) == 2:
            start, end = view_range
            start = max(1, start)
            end = min(len(lines), end)
            selected = lines[start - 1 : end]
            start_num = start
        else:
            selected = lines
            start_num = 1

        numbered = [f"{start_num + i:5d}: {line}" for i, line in enumerate(selected)]
        return "\n".join(numbered) if numbered else "(arquivo vazio)"

    def create(self, path: str, file_text: str) -> str:
        """Create a new file with text."""
        file_path = self._resolve_safe_path(path)
        if file_path.exists():
            return f"Erro: o arquivo '{path}' já existe. Use str_replace para editá-lo."

        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(file_text, encoding="utf-8")
        return f"Arquivo '{path}' criado com sucesso ({len(file_text)} caracteres)."

    def str_replace(self, path: str, old_str: str, new_str: str) -> str:
        """Replace old_str with new_str. old_str must appear EXACTLY once."""
        file_path = self._resolve_safe_path(path)
        if not file_path.exists():
            return f"Erro: o arquivo '{path}' não existe."

        content = file_path.read_text(encoding="utf-8")
        occurrences = content.count(old_str)

        if occurrences == 0:
            return f"Erro: 'old_str' não foi encontrado no arquivo '{path}'."
        if occurrences > 1:
            return (
                f"Erro: 'old_str' aparece {occurrences} vezes no arquivo '{path}'. "
                "Forneça mais contexto de linhas ao redor para torná-lo único."
            )

        updated = content.replace(old_str, new_str, 1)
        file_path.write_text(updated, encoding="utf-8")
        return f"Arquivo '{path}' atualizado com sucesso."

    def insert(self, path: str, insert_line: int, new_str: str) -> str:
        """Insert new_str after insert_line (0-indexed or 1-indexed; 0 = top of file)."""
        file_path = self._resolve_safe_path(path)
        if not file_path.exists():
            return f"Erro: o arquivo '{path}' não existe."

        lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)
        idx = max(0, min(insert_line, len(lines)))

        insert_content = new_str if new_str.endswith("\n") else new_str + "\n"
        lines.insert(idx, insert_content)

        file_path.write_text("".join(lines), encoding="utf-8")
        return f"Texto inserido na linha {idx} do arquivo '{path}' com sucesso."

    def execute(self, command: str, **kwargs) -> str:
        """Dispatch command."""
        path = kwargs.get("path", "")
        if not path:
            return "Erro: parâmetro 'path' é obrigatório."

        try:
            if command == "view":
                return self.view(path, kwargs.get("view_range"))
            elif command == "create":
                return self.create(path, kwargs.get("file_text", ""))
            elif command == "str_replace":
                old_str = kwargs.get("old_str", "")
                new_str = kwargs.get("new_str", "")
                if not old_str:
                    return "Erro: 'old_str' é obrigatório para str_replace."
                return self.str_replace(path, old_str, new_str)
            elif command == "insert":
                insert_line = kwargs.get("insert_line", 0)
                new_str = kwargs.get("new_str", "")
                return self.insert(path, insert_line, new_str)
            else:
                return f"Erro: comando desconhecido '{command}'."
        except PathSecurityError as e:
            return str(e)
        except (OSError, ValueError, TypeError) as e:
            return f"Erro ao executar '{command}' em '{path}': {e}"
