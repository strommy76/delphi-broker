from __future__ import annotations

import importlib
import json
import sys
import types
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@dataclass
class BrokerStack:
    config: object
    database: object
    client: TestClient

    def sign(self, agent_id: str, *fields: str) -> str:
        return self.database.compute_signature(self.config.AGENT_SECRETS[agent_id], *fields)


def _reload_broker_modules() -> tuple[object, object, object]:
    for name in sorted(sys.modules, reverse=True):
        if name == "delphi_broker" or name.startswith("delphi_broker."):
            sys.modules.pop(name, None)
    fastmcp_module = types.ModuleType("mcp.server.fastmcp")

    class _FakeFastMCP:
        def __init__(self, name: str):
            self.name = name

        def tool(self):
            def decorator(func):
                return func

            return decorator

        def streamable_http_app(self):
            return FastAPI()

    fastmcp_module.FastMCP = _FakeFastMCP
    sys.modules["mcp"] = types.ModuleType("mcp")
    sys.modules["mcp.server"] = types.ModuleType("mcp.server")
    sys.modules["mcp.server.fastmcp"] = fastmcp_module
    markdown_module = types.ModuleType("markdown")

    class _FakeMarkdown:
        def __init__(self, extensions=None):
            self.extensions = extensions or []

        def reset(self):
            return None

        def convert(self, text: str) -> str:
            return text

    markdown_module.Markdown = _FakeMarkdown
    sys.modules["markdown"] = markdown_module
    nh3_module = types.ModuleType("nh3")
    nh3_module.clean = lambda html: html
    sys.modules["nh3"] = nh3_module
    python_multipart_module = types.ModuleType("python_multipart")
    python_multipart_module.__version__ = "0.0.20"
    sys.modules["python_multipart"] = python_multipart_module
    multipart_module = types.ModuleType("multipart")
    multipart_module.__version__ = "0.0.20"
    sys.modules["multipart"] = multipart_module
    multipart_submodule = types.ModuleType("multipart.multipart")
    multipart_submodule.parse_options_header = lambda value: ("multipart/form-data", {})
    sys.modules["multipart.multipart"] = multipart_submodule
    config = importlib.import_module("delphi_broker.config")
    database = importlib.import_module("delphi_broker.database")
    main = importlib.import_module("delphi_broker.main")
    return config, database, main


@pytest.fixture
def broker_stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    agents_path = tmp_path / "agents.json"
    agents_path.write_text(
        json.dumps(
            {
                "agents": [
                    {
                        "agent_id": "worker-a",
                        "host": "host-a",
                        "roles": "worker",
                        "secret": "a" * 64,
                    },
                    {
                        "agent_id": "worker-b",
                        "host": "host-b",
                        "roles": "worker",
                        "secret": "b" * 64,
                    },
                    {
                        "agent_id": "orch",
                        "host": "host-o",
                        "roles": "worker,orchestrator",
                        "secret": "c" * 64,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "broker.sqlite"
    monkeypatch.setenv("DELPHI_AGENTS_PATH", str(agents_path))
    monkeypatch.setenv("DELPHI_DB_PATH", str(db_path))
    monkeypatch.setenv("DELPHI_WEB_UI_PASSWORD", "web-secret")
    monkeypatch.setenv("DELPHI_WEB_UI_AGENT_ID", "web-ui")
    monkeypatch.setenv("DELPHI_WEB_UI_ROLES", "orchestrator")

    config, database, main = _reload_broker_modules()
    with TestClient(main.app) as client:
        yield BrokerStack(config=config, database=database, client=client)
