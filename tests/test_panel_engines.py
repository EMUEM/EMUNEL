"""The SERVED panel (panel.py) must expose the Engine Settings page.

Regression for the fix where the Engine Settings UI was built only inside
the unserved modular frontend (console/frontend/assets/js/views/engines.js)
while the console actually serves the self-contained panel.py page at "/"
— so none of the engine sections ever appeared for users.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for rel in ("console/api",):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / rel))

from emunel_console import panel, version  # noqa: E402


def test_panel_has_engine_settings_page():
    page = panel.PAGE
    assert "viewEngines" in page, "viewEngines function missing from served panel"
    assert "Engine Settings" in page, "page title missing from served panel"
    assert "/api/engines" in page, "engines API wiring missing from served panel"
    assert 'data-nav="engines"' in page, "engines nav item missing from served panel"
    assert 'name==="engines")viewEngines()' in page, "nav_ routing to engines missing"


def test_panel_engines_nav_is_admin_gated():
    # the Engines nav button must only render for admins
    idx = panel.PAGE.find('data-nav="engines"')
    assert idx > 0
    assert "USER.is_admin" in panel.PAGE[max(0, idx - 200):idx]


def test_panel_build_stamp_rendered():
    # the served page must carry the build stamp (never the raw placeholder)
    assert "__EMUNEL_BUILD__" not in panel.PAGE
    assert "build " in panel.PAGE  # sidebar footer line


def test_version_build_fallback():
    assert version.build()  # "dev" or the stamp file — never empty
    assert version.info()["build"] == version.build()


def test_health_includes_build():
    src = (Path(__file__).resolve().parents[1]
           / "console" / "api" / "emunel_console" / "main.py").read_text()
    assert '"build": version.build()' in src
