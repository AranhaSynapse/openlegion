"""Tests for extended template system — template application, permissions, workflows."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from src.cli.config import (
    AGENTS_FILE,
    PERMISSIONS_FILE,
    PROJECT_ROOT,
    _add_agent_permissions,
    _add_agent_to_config,
    _apply_template,
    _validate_agent_name,
)


class _TempConfigMixin:
    """Mixin that redirects config files to a temp directory."""

    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_agents = AGENTS_FILE
        self._orig_perms = PERMISSIONS_FILE
        self._orig_root = PROJECT_ROOT
        # Monkey-patch module-level paths
        import src.cli.config as cfg_mod
        self._agents_path = Path(self._tmpdir) / "config" / "agents.yaml"
        self._perms_path = Path(self._tmpdir) / "config" / "permissions.json"
        self._agents_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_mod.AGENTS_FILE = self._agents_path
        cfg_mod.PERMISSIONS_FILE = self._perms_path
        cfg_mod.PROJECT_ROOT = Path(self._tmpdir)
        # Initialize empty permissions
        self._perms_path.write_text(json.dumps({"permissions": {}}, indent=2))

    def teardown_method(self):
        import src.cli.config as cfg_mod
        cfg_mod.AGENTS_FILE = self._orig_agents
        cfg_mod.PERMISSIONS_FILE = self._orig_perms
        cfg_mod.PROJECT_ROOT = self._orig_root
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _mock_config(self, *, collab=True):
        return patch("src.cli.config._load_config", return_value={
            "llm": {"default_model": "openai/gpt-4o-mini"},
            "agents": {},
            "collaboration": collab,
        })


class TestAddAgentToConfig(_TempConfigMixin):
    def test_basic_fields(self):
        _add_agent_to_config("alice", "researcher", "openai/gpt-4o")
        with open(self._agents_path) as f:
            cfg = yaml.safe_load(f)
        assert cfg["agents"]["alice"]["role"] == "researcher"
        assert cfg["agents"]["alice"]["model"] == "openai/gpt-4o"

    def test_initial_instructions(self):
        _add_agent_to_config("bob", "engineer", "openai/gpt-4o", initial_instructions="Build things")
        with open(self._agents_path) as f:
            cfg = yaml.safe_load(f)
        assert cfg["agents"]["bob"]["initial_instructions"] == "Build things"

    def test_initial_soul(self):
        _add_agent_to_config("carol", "writer", "openai/gpt-4o", initial_soul="You are creative.")
        with open(self._agents_path) as f:
            cfg = yaml.safe_load(f)
        assert cfg["agents"]["carol"]["initial_soul"] == "You are creative."

    def test_initial_heartbeat(self):
        _add_agent_to_config("dave", "monitor", "openai/gpt-4o", initial_heartbeat="Check alerts.")
        with open(self._agents_path) as f:
            cfg = yaml.safe_load(f)
        assert cfg["agents"]["dave"]["initial_heartbeat"] == "Check alerts."

    def test_thinking(self):
        _add_agent_to_config("eve", "analyst", "openai/gpt-4o", thinking="medium")
        with open(self._agents_path) as f:
            cfg = yaml.safe_load(f)
        assert cfg["agents"]["eve"]["thinking"] == "medium"

    def test_budget(self):
        _add_agent_to_config("frank", "scout", "openai/gpt-4o", budget={"daily_usd": 5.0})
        with open(self._agents_path) as f:
            cfg = yaml.safe_load(f)
        assert cfg["agents"]["frank"]["budget"]["daily_usd"] == 5.0

    def test_resources(self):
        _add_agent_to_config("greg", "helper", "openai/gpt-4o", resources={"memory_limit": "1g", "cpu_limit": 1.0})
        with open(self._agents_path) as f:
            cfg = yaml.safe_load(f)
        assert cfg["agents"]["greg"]["resources"]["memory_limit"] == "1g"
        assert cfg["agents"]["greg"]["resources"]["cpu_limit"] == 1.0

    def test_empty_fields_not_written(self):
        """Empty optional fields should not appear in agents.yaml."""
        _add_agent_to_config("hank", "helper", "openai/gpt-4o")
        with open(self._agents_path) as f:
            cfg = yaml.safe_load(f)
        agent = cfg["agents"]["hank"]
        assert "initial_soul" not in agent
        assert "initial_heartbeat" not in agent
        assert "initial_instructions" not in agent
        assert "thinking" not in agent
        assert "budget" not in agent
        assert "resources" not in agent

    def test_all_fields_together(self):
        """All optional fields can be set simultaneously."""
        _add_agent_to_config(
            "full", "full-agent", "openai/gpt-4o",
            initial_instructions="Do stuff.",
            initial_soul="Be nice.",
            initial_heartbeat="Check things.",
            thinking="high",
            budget={"daily_usd": 10.0},
            resources={"memory_limit": "512m"},
        )
        with open(self._agents_path) as f:
            cfg = yaml.safe_load(f)
        agent = cfg["agents"]["full"]
        assert agent["initial_instructions"] == "Do stuff."
        assert agent["initial_soul"] == "Be nice."
        assert agent["initial_heartbeat"] == "Check things."
        assert agent["thinking"] == "high"
        assert agent["budget"]["daily_usd"] == 10.0
        assert agent["resources"]["memory_limit"] == "512m"


class TestAddAgentPermissions(_TempConfigMixin):
    def test_default_permissions(self):
        """Without template permissions, defaults are used."""
        _add_agent_to_config("alice", "researcher", "openai/gpt-4o")
        _add_agent_permissions("alice")
        with open(self._perms_path) as f:
            perms = json.load(f)
        alice = perms["permissions"]["alice"]
        assert alice["allowed_apis"] == ["llm"]
        assert alice["blackboard_read"] == []
        assert alice["blackboard_write"] == []

    def test_template_permissions_merged(self):
        """Template permissions are merged into defaults."""
        _add_agent_to_config("bob", "engineer", "openai/gpt-4o")
        _add_agent_permissions("bob", permissions={
            "blackboard_read": ["tasks/*", "reviews/*"],
            "blackboard_write": ["tasks/*"],
            "can_publish": ["task_complete"],
            "can_subscribe": ["tasks_ready"],
        })
        with open(self._perms_path) as f:
            perms = json.load(f)
        bob = perms["permissions"]["bob"]
        assert "tasks/*" in bob["blackboard_read"]
        assert "reviews/*" in bob["blackboard_read"]
        assert "tasks/*" in bob["blackboard_write"]
        assert "task_complete" in bob["can_publish"]
        assert "tasks_ready" in bob["can_subscribe"]
        # Defaults still present
        assert "llm" in bob["allowed_apis"]

    def test_template_permissions_merge_with_collab_defaults(self):
        """Template permissions add to collaboration defaults, not replace."""
        _add_agent_to_config("carol", "writer", "openai/gpt-4o")
        _add_agent_permissions("carol", permissions={
            "can_publish": ["draft_ready"],
        })
        with open(self._perms_path) as f:
            perms = json.load(f)
        carol = perms["permissions"]["carol"]
        # Both collab default "*" and template "draft_ready" should be present
        assert "draft_ready" in carol["can_publish"]
        assert "*" in carol["can_publish"]

    def test_non_collab_mode_uses_template_permissions(self):
        """When collaboration is off, template permissions are the only source."""
        _add_agent_to_config("dave", "researcher", "openai/gpt-4o")
        with patch("src.cli.config._load_config", return_value={
            "llm": {"default_model": "openai/gpt-4o-mini"},
            "agents": {"dave": {"role": "researcher"}},
            "collaboration": False,
        }):
            _add_agent_permissions("dave", permissions={
                "blackboard_read": ["data/*"],
                "blackboard_write": ["results/*"],
                "can_publish": ["done"],
                "can_subscribe": ["start"],
            })
        with open(self._perms_path) as f:
            perms = json.load(f)
        dave = perms["permissions"]["dave"]
        # Non-collab default is restrictive
        assert dave["can_message"] == ["orchestrator"]
        # Template permissions ARE the only blackboard patterns
        assert "data/*" in dave["blackboard_read"]
        assert "results/*" in dave["blackboard_write"]
        # Template publish/subscribe merged with non-collab defaults
        assert "done" in dave["can_publish"]
        assert "start" in dave["can_subscribe"]
        # Non-collab default also includes the agent-specific complete event
        assert "dave_complete" in dave["can_publish"]

    def test_none_permissions_ignored(self):
        """Passing permissions=None should produce default permissions."""
        _add_agent_to_config("eve", "helper", "openai/gpt-4o")
        _add_agent_permissions("eve", permissions=None)
        with open(self._perms_path) as f:
            perms = json.load(f)
        eve = perms["permissions"]["eve"]
        assert eve["blackboard_read"] == []
        assert eve["blackboard_write"] == []

    def test_empty_template_permission_lists_ignored(self):
        """Empty lists in template permissions don't affect defaults."""
        _add_agent_to_config("fay", "helper", "openai/gpt-4o")
        _add_agent_permissions("fay", permissions={
            "blackboard_read": [],
            "can_publish": [],
        })
        with open(self._perms_path) as f:
            perms = json.load(f)
        fay = perms["permissions"]["fay"]
        assert fay["blackboard_read"] == []

    def test_invalid_permission_type_ignored(self):
        """Non-list values in template permissions are safely ignored."""
        _add_agent_to_config("gus", "helper", "openai/gpt-4o")
        _add_agent_permissions("gus", permissions={
            "blackboard_read": "not-a-list",
            "can_publish": 42,
        })
        with open(self._perms_path) as f:
            perms = json.load(f)
        gus = perms["permissions"]["gus"]
        # Invalid types are ignored, defaults preserved
        assert isinstance(gus["blackboard_read"], list)


class TestApplyTemplate(_TempConfigMixin):
    def test_creates_agents_with_new_fields(self):
        tpl = {
            "agents": {
                "scout": {
                    "role": "research_scout",
                    "model": "{default_model}",
                    "instructions": "Find sources.",
                    "soul": "You are curious.",
                    "heartbeat": "Check news.",
                    "thinking": "medium",
                    "budget": {"daily_usd": 5.0, "monthly_usd": 100.0},
                },
            },
        }
        with self._mock_config():
            created = _apply_template("test-tpl", tpl)

        assert "scout" in created
        with open(self._agents_path) as f:
            cfg = yaml.safe_load(f)
        scout = cfg["agents"]["scout"]
        assert scout["initial_instructions"] == "Find sources."
        assert scout["initial_soul"] == "You are curious."
        assert scout["initial_heartbeat"] == "Check news."
        assert scout["thinking"] == "medium"
        assert scout["budget"]["daily_usd"] == 5.0

    def test_resources_written_in_single_pass(self):
        """Resources are included in the same agents.yaml write, not a separate re-read."""
        tpl = {
            "agents": {
                "worker": {
                    "role": "worker",
                    "model": "{default_model}",
                    "resources": {"memory_limit": "1g", "cpu_limit": 1.0},
                },
            },
        }
        with self._mock_config():
            _apply_template("test-tpl", tpl)

        with open(self._agents_path) as f:
            cfg = yaml.safe_load(f)
        assert cfg["agents"]["worker"]["resources"]["memory_limit"] == "1g"
        assert cfg["agents"]["worker"]["resources"]["cpu_limit"] == 1.0

    def test_sets_permissions_from_template(self):
        tpl = {
            "agents": {
                "analyst": {
                    "role": "analyst",
                    "model": "{default_model}",
                    "permissions": {
                        "blackboard_read": ["data/*"],
                        "blackboard_write": ["analysis/*"],
                        "can_publish": ["analysis_ready"],
                        "can_subscribe": ["data_ready"],
                    },
                },
            },
        }
        with self._mock_config():
            _apply_template("test-tpl", tpl)

        with open(self._perms_path) as f:
            perms = json.load(f)
        analyst = perms["permissions"]["analyst"]
        assert "data/*" in analyst["blackboard_read"]
        assert "analysis/*" in analyst["blackboard_write"]
        assert "analysis_ready" in analyst["can_publish"]
        assert "data_ready" in analyst["can_subscribe"]

    def test_creates_workflow_file(self):
        tpl = {
            "agents": {
                "worker": {
                    "role": "worker",
                    "model": "{default_model}",
                },
            },
            "workflow": {
                "name": "test-pipeline",
                "trigger": "manual",
                "timeout": 300,
                "steps": [
                    {"id": "step1", "agent": "worker", "task_type": "work", "timeout": 120},
                ],
            },
        }
        with self._mock_config():
            _apply_template("test-tpl", tpl)

        wf_path = Path(self._tmpdir) / "config" / "workflows" / "test-pipeline.yaml"
        assert wf_path.exists()
        with open(wf_path) as f:
            wf = yaml.safe_load(f)
        assert wf["name"] == "test-pipeline"
        assert wf["steps"][0]["agent"] == "worker"

    def test_workflow_parses_as_valid_definition(self):
        """Written workflow YAML roundtrips through WorkflowDefinition."""
        from src.shared.types import WorkflowDefinition
        tpl = {
            "agents": {
                "a": {"role": "a", "model": "{default_model}"},
                "b": {"role": "b", "model": "{default_model}"},
            },
            "workflow": {
                "name": "roundtrip-test",
                "trigger": "manual",
                "timeout": 600,
                "steps": [
                    {"id": "s1", "agent": "a", "task_type": "work", "timeout": 120},
                    {"id": "s2", "agent": "b", "task_type": "review", "timeout": 60, "depends_on": ["s1"]},
                ],
            },
        }
        with self._mock_config():
            _apply_template("roundtrip", tpl)

        wf_path = Path(self._tmpdir) / "config" / "workflows" / "roundtrip-test.yaml"
        with open(wf_path) as f:
            data = yaml.safe_load(f)
        wf = WorkflowDefinition(**data)
        assert wf.name == "roundtrip-test"
        assert len(wf.steps) == 2
        assert wf.steps[1].depends_on == ["s1"]

    def test_no_workflow_when_not_defined(self):
        tpl = {
            "agents": {
                "helper": {
                    "role": "helper",
                    "model": "{default_model}",
                },
            },
        }
        with self._mock_config():
            _apply_template("test-tpl", tpl)

        wf_dir = Path(self._tmpdir) / "config" / "workflows"
        if wf_dir.exists():
            assert list(wf_dir.glob("*.yaml")) == []

    def test_model_substitution(self):
        tpl = {
            "agents": {
                "agent1": {
                    "role": "helper",
                    "model": "{default_model}",
                },
            },
        }
        with patch("src.cli.config._load_config", return_value={
            "llm": {"default_model": "anthropic/claude-sonnet-4-6"},
            "agents": {},
            "collaboration": True,
        }):
            _apply_template("test-tpl", tpl)

        with open(self._agents_path) as f:
            cfg = yaml.safe_load(f)
        assert cfg["agents"]["agent1"]["model"] == "anthropic/claude-sonnet-4-6"

    def test_skills_dir_created(self):
        tpl = {
            "agents": {
                "agent1": {
                    "role": "helper",
                    "model": "{default_model}",
                },
            },
        }
        with self._mock_config():
            _apply_template("test-tpl", tpl)

        skills_dir = Path(self._tmpdir) / "skills" / "agent1"
        assert skills_dir.is_dir()

    def test_rejects_invalid_agent_name(self):
        """Agent names with path traversal are rejected."""
        tpl = {
            "agents": {
                "../evil": {
                    "role": "evil",
                    "model": "{default_model}",
                },
            },
        }
        with self._mock_config():
            with pytest.raises(ValueError, match="Invalid agent name"):
                _apply_template("test-tpl", tpl)

    def test_rejects_invalid_workflow_name(self):
        """Workflow names with path traversal are rejected."""
        tpl = {
            "agents": {
                "worker": {
                    "role": "worker",
                    "model": "{default_model}",
                },
            },
            "workflow": {
                "name": "../../etc/evil",
                "trigger": "manual",
                "steps": [{"id": "s1", "agent": "worker", "task_type": "work"}],
            },
        }
        with self._mock_config():
            with pytest.raises(ValueError, match="Invalid agent name"):
                _apply_template("test-tpl", tpl)

    def test_workflow_name_falls_back_to_template_name(self):
        """When workflow has no name, template_name is used (and validated)."""
        tpl = {
            "agents": {
                "worker": {"role": "worker", "model": "{default_model}"},
            },
            "workflow": {
                "trigger": "manual",
                "steps": [{"id": "s1", "agent": "worker", "task_type": "work"}],
            },
        }
        with self._mock_config():
            _apply_template("my-workflow", tpl)

        wf_path = Path(self._tmpdir) / "config" / "workflows" / "my-workflow.yaml"
        assert wf_path.exists()

    def test_system_prompt_fallback_key(self):
        """The legacy 'system_prompt' key is used if 'instructions' is absent."""
        tpl = {
            "agents": {
                "legacy": {
                    "role": "legacy",
                    "model": "{default_model}",
                    "system_prompt": "Legacy instructions here.",
                },
            },
        }
        with self._mock_config():
            _apply_template("test-tpl", tpl)

        with open(self._agents_path) as f:
            cfg = yaml.safe_load(f)
        assert cfg["agents"]["legacy"]["initial_instructions"] == "Legacy instructions here."

    def test_multi_agent_template(self):
        """A template with multiple agents creates all of them."""
        tpl = {
            "agents": {
                "alpha": {"role": "first", "model": "{default_model}"},
                "beta": {"role": "second", "model": "{default_model}"},
                "gamma": {"role": "third", "model": "{default_model}"},
            },
        }
        with self._mock_config():
            created = _apply_template("multi", tpl)

        assert created == ["alpha", "beta", "gamma"]
        with open(self._agents_path) as f:
            cfg = yaml.safe_load(f)
        assert len(cfg["agents"]) == 3
        with open(self._perms_path) as f:
            perms = json.load(f)
        assert len(perms["permissions"]) == 3


class TestLoadTemplates:
    def test_all_templates_parse(self):
        """All template YAML files in src/templates/ parse without error."""
        from src.cli.config import _load_templates
        templates = _load_templates()
        assert len(templates) >= 6  # starter, devteam, content, sales, deep-research, monitor
        for name, tpl in templates.items():
            assert "agents" in tpl, f"Template '{name}' missing agents key"
            assert "description" in tpl, f"Template '{name}' missing description"

    def test_templates_have_valid_agent_defs(self):
        """Each agent in each template has at least role and model."""
        from src.cli.config import _load_templates
        templates = _load_templates()
        for tpl_name, tpl in templates.items():
            for agent_name, agent_def in tpl.get("agents", {}).items():
                assert "role" in agent_def, f"{tpl_name}/{agent_name} missing role"
                assert "model" in agent_def, f"{tpl_name}/{agent_name} missing model"

    def test_all_agent_names_are_valid(self):
        """Every agent name in every template passes validation."""
        from src.cli.config import _load_templates
        templates = _load_templates()
        for tpl_name, tpl in templates.items():
            for agent_name in tpl.get("agents", {}):
                _validate_agent_name(agent_name)  # raises on invalid

    def test_all_workflow_names_are_valid(self):
        """Every workflow name in templates passes validation."""
        from src.cli.config import _load_templates
        templates = _load_templates()
        for tpl_name, tpl in templates.items():
            wf = tpl.get("workflow")
            if wf:
                wf_name = wf.get("name", tpl_name)
                _validate_agent_name(wf_name)

    def test_workflow_templates_parse_as_definitions(self):
        """All template workflows parse as valid WorkflowDefinition."""
        from src.cli.config import _load_templates
        from src.shared.types import WorkflowDefinition
        templates = _load_templates()
        for tpl_name, tpl in templates.items():
            wf = tpl.get("workflow")
            if wf:
                wd = WorkflowDefinition(**wf)
                assert wd.name, f"{tpl_name} workflow has no name"
                assert len(wd.steps) > 0, f"{tpl_name} workflow has no steps"

    def test_workflow_agents_exist_in_template(self):
        """All agents referenced in workflow steps are defined in the template."""
        from src.cli.config import _load_templates
        templates = _load_templates()
        for tpl_name, tpl in templates.items():
            wf = tpl.get("workflow")
            if not wf:
                continue
            agent_names = set(tpl.get("agents", {}).keys())
            for step in wf.get("steps", []):
                step_agent = step.get("agent")
                if step_agent:
                    assert step_agent in agent_names, (
                        f"{tpl_name} workflow step '{step.get('id')}' references "
                        f"agent '{step_agent}' which is not defined in the template"
                    )
