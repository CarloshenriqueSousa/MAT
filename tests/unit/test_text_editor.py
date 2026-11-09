"""Unit tests for the host-side TextEditorHandler tool."""

from __future__ import annotations

import pytest

from mat.tools.text_editor import PathSecurityError, TextEditorHandler


def test_text_editor_create_and_view(tmp_path):
    editor = TextEditorHandler(tmp_path)

    # Cria arquivo
    res_create = editor.create("src/main.py", "x = 10\ny = 20\nprint(x + y)\n")
    assert "criado com sucesso" in res_create

    # Visualiza arquivo completo
    res_view = editor.view("src/main.py")
    assert "1: x = 10" in res_view
    assert "2: y = 20" in res_view
    assert "3: print(x + y)" in res_view

    # Visualiza intervalo
    res_range = editor.view("src/main.py", view_range=[2, 2])
    assert "2: y = 20" in res_range
    assert "1: x = 10" not in res_range


def test_text_editor_str_replace_exact_match(tmp_path):
    editor = TextEditorHandler(tmp_path)
    editor.create("config.py", "DEBUG = False\nPORT = 8080\n")

    res = editor.str_replace("config.py", old_str="PORT = 8080", new_str="PORT = 9000")
    assert "atualizado com sucesso" in res

    content = editor.view("config.py")
    assert "PORT = 9000" in content


def test_text_editor_str_replace_fails_on_duplicate(tmp_path):
    editor = TextEditorHandler(tmp_path)
    editor.create("dups.py", "value = 1\nvalue = 1\n")

    res = editor.str_replace("dups.py", old_str="value = 1", new_str="value = 2")
    assert "aparece 2 vezes" in res


def test_text_editor_path_traversal_blocked(tmp_path):
    editor = TextEditorHandler(tmp_path)

    # Tenta criar arquivo fora da raiz do worktree
    with pytest.raises(PathSecurityError):
        editor._resolve_safe_path("../escaped.py")

    with pytest.raises(PathSecurityError):
        editor._resolve_safe_path("/etc/passwd")

    # Via dispatcher execute() deve retornar mensagem de erro amigável
    res = editor.execute(command="create", path="../../escape.py", file_text="hack")
    assert "Violação de segurança" in res
