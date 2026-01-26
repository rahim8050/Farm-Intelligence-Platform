from __future__ import annotations

import builtins
import os
import sys

import pytest

import manage


def test_manage_main_sets_settings_and_calls_execute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: dict[str, object] = {}

    def fake_execute(argv: list[str]) -> None:
        called["argv"] = list(argv)

    monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)
    monkeypatch.setattr(
        "django.core.management.execute_from_command_line", fake_execute
    )
    monkeypatch.setattr(sys, "argv", ["manage.py", "check"])

    manage.main()

    assert os.environ["DJANGO_SETTINGS_MODULE"] == "config.settings"
    assert called["argv"] == ["manage.py", "check"]


def test_manage_main_import_error_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = builtins.__import__

    def fake_import(
        name: str,
        globals: dict[str, object] | None = None,
        locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name == "django.core.management":
            raise ImportError("missing django")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)

    with pytest.raises(ImportError, match="Couldn't import Django"):
        manage.main()
