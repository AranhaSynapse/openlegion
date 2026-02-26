"""Tests for multi-project support: CRUD, config loading, permissions wiring."""

import json
from unittest.mock import patch

import pytest
import yaml

from src.cli.config import (
    _add_agent_to_project,
    _add_project_blackboard_permissions,
    _create_project,
    _delete_project,
    _get_agent_project,
    _load_config,
    _load_projects,
    _remove_agent,
    _remove_agent_from_project,
    _remove_project_blackboard_permissions,
    _validate_project_name,
)


class TestValidateProjectName:
    def test_valid_names(self):
        assert _validate_project_name("my-project") == "my-project"
        assert _validate_project_name("project1") == "project1"
        assert _validate_project_name("A_B-C") == "A_B-C"

    def test_invalid_empty(self):
        with pytest.raises(ValueError, match="Invalid project name"):
            _validate_project_name("")

    def test_invalid_special_chars(self):
        with pytest.raises(ValueError, match="Invalid project name"):
            _validate_project_name("my project")

    def test_invalid_start_char(self):
        with pytest.raises(ValueError, match="Invalid project name"):
            _validate_project_name("-start")


class TestLoadProjects:
    def test_no_projects_dir(self, tmp_path):
        with patch("src.cli.config.PROJECTS_DIR", tmp_path / "nonexistent"):
            result = _load_projects()
        assert result == {}

    def test_load_single_project(self, tmp_path):
        projects_dir = tmp_path / "projects"
        proj_dir = projects_dir / "my-proj"
        proj_dir.mkdir(parents=True)
        meta = {"name": "my-proj", "description": "Test", "members": ["agent1"]}
        (proj_dir / "metadata.yaml").write_text(yaml.dump(meta))

        with patch("src.cli.config.PROJECTS_DIR", projects_dir):
            result = _load_projects()

        assert "my-proj" in result
        assert result["my-proj"]["description"] == "Test"
        assert result["my-proj"]["members"] == ["agent1"]

    def test_load_multiple_projects(self, tmp_path):
        projects_dir = tmp_path / "projects"
        for name in ["alpha", "beta"]:
            d = projects_dir / name
            d.mkdir(parents=True)
            (d / "metadata.yaml").write_text(yaml.dump({"name": name, "members": []}))

        with patch("src.cli.config.PROJECTS_DIR", projects_dir):
            result = _load_projects()

        assert len(result) == 2
        assert "alpha" in result
        assert "beta" in result

    def test_corrupted_metadata_skipped(self, tmp_path):
        projects_dir = tmp_path / "projects"
        d = projects_dir / "bad"
        d.mkdir(parents=True)
        (d / "metadata.yaml").write_text("{{invalid yaml")

        with patch("src.cli.config.PROJECTS_DIR", projects_dir):
            result = _load_projects()

        assert result == {}


class TestGetAgentProject:
    def test_agent_in_project(self, tmp_path):
        projects_dir = tmp_path / "projects"
        d = projects_dir / "proj1"
        d.mkdir(parents=True)
        (d / "metadata.yaml").write_text(yaml.dump({
            "name": "proj1", "members": ["agent1", "agent2"],
        }))

        with patch("src.cli.config.PROJECTS_DIR", projects_dir):
            assert _get_agent_project("agent1") == "proj1"
            assert _get_agent_project("agent2") == "proj1"

    def test_standalone_agent(self, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir(parents=True)

        with patch("src.cli.config.PROJECTS_DIR", projects_dir):
            assert _get_agent_project("standalone") is None


class TestCreateProject:
    def test_create_basic(self, tmp_path):
        projects_dir = tmp_path / "projects"
        perms_file = tmp_path / "permissions.json"
        perms_file.write_text(json.dumps({"permissions": {}}))

        with (
            patch("src.cli.config.PROJECTS_DIR", projects_dir),
            patch("src.cli.config.PERMISSIONS_FILE", perms_file),
        ):
            _create_project("test-proj", description="A test project")

        meta_file = projects_dir / "test-proj" / "metadata.yaml"
        assert meta_file.exists()
        data = yaml.safe_load(meta_file.read_text())
        assert data["name"] == "test-proj"
        assert data["description"] == "A test project"
        assert data["members"] == []

        project_md = projects_dir / "test-proj" / "project.md"
        assert project_md.exists()
        assert "test-proj" in project_md.read_text()

        workflows_dir = projects_dir / "test-proj" / "workflows"
        assert workflows_dir.is_dir()

    def test_create_with_members(self, tmp_path):
        projects_dir = tmp_path / "projects"
        perms_file = tmp_path / "permissions.json"
        perms_file.write_text(json.dumps({
            "permissions": {
                "agent1": {"blackboard_read": ["context/*"], "blackboard_write": ["context/*"]},
                "agent2": {"blackboard_read": ["context/*"], "blackboard_write": ["context/*"]},
            }
        }))

        with (
            patch("src.cli.config.PROJECTS_DIR", projects_dir),
            patch("src.cli.config.PERMISSIONS_FILE", perms_file),
        ):
            _create_project("my-proj", members=["agent1", "agent2"])

        meta = yaml.safe_load((projects_dir / "my-proj" / "metadata.yaml").read_text())
        assert meta["members"] == ["agent1", "agent2"]

        # Check permissions were updated
        perms = json.loads(perms_file.read_text())
        assert "projects/my-proj/*" in perms["permissions"]["agent1"]["blackboard_read"]
        assert "projects/my-proj/*" in perms["permissions"]["agent1"]["blackboard_write"]
        assert "projects/my-proj/*" in perms["permissions"]["agent2"]["blackboard_read"]

    def test_create_duplicate_raises(self, tmp_path):
        projects_dir = tmp_path / "projects"
        (projects_dir / "existing").mkdir(parents=True)
        perms_file = tmp_path / "permissions.json"
        perms_file.write_text(json.dumps({"permissions": {}}))

        with (
            patch("src.cli.config.PROJECTS_DIR", projects_dir),
            patch("src.cli.config.PERMISSIONS_FILE", perms_file),
        ):
            with pytest.raises(ValueError, match="already exists"):
                _create_project("existing")


class TestDeleteProject:
    def test_delete_project(self, tmp_path):
        projects_dir = tmp_path / "projects"
        proj_dir = projects_dir / "doomed"
        proj_dir.mkdir(parents=True)
        (proj_dir / "metadata.yaml").write_text(yaml.dump({
            "name": "doomed", "members": ["agent1"],
        }))
        (proj_dir / "project.md").write_text("# doomed")

        perms_file = tmp_path / "permissions.json"
        perms_file.write_text(json.dumps({
            "permissions": {
                "agent1": {
                    "blackboard_read": ["context/*", "projects/doomed/*"],
                    "blackboard_write": ["context/*", "projects/doomed/*"],
                },
            }
        }))

        with (
            patch("src.cli.config.PROJECTS_DIR", projects_dir),
            patch("src.cli.config.PERMISSIONS_FILE", perms_file),
        ):
            _delete_project("doomed")

        assert not proj_dir.exists()

        # Permissions cleaned up
        perms = json.loads(perms_file.read_text())
        assert "projects/doomed/*" not in perms["permissions"]["agent1"]["blackboard_read"]
        assert "projects/doomed/*" not in perms["permissions"]["agent1"]["blackboard_write"]

    def test_delete_nonexistent_raises(self, tmp_path):
        with patch("src.cli.config.PROJECTS_DIR", tmp_path / "nope"):
            with pytest.raises(ValueError, match="not found"):
                _delete_project("ghost")


class TestAddRemoveAgentProject:
    def _setup(self, tmp_path):
        projects_dir = tmp_path / "projects"
        proj_dir = projects_dir / "proj1"
        proj_dir.mkdir(parents=True)
        (proj_dir / "metadata.yaml").write_text(yaml.dump({
            "name": "proj1", "members": [],
        }))

        perms_file = tmp_path / "permissions.json"
        perms_file.write_text(json.dumps({
            "permissions": {
                "agent1": {
                    "blackboard_read": ["context/*"],
                    "blackboard_write": ["context/*"],
                },
            }
        }))
        return projects_dir, perms_file

    def test_add_agent(self, tmp_path):
        projects_dir, perms_file = self._setup(tmp_path)

        with (
            patch("src.cli.config.PROJECTS_DIR", projects_dir),
            patch("src.cli.config.PERMISSIONS_FILE", perms_file),
        ):
            _add_agent_to_project("proj1", "agent1")

        meta = yaml.safe_load((projects_dir / "proj1" / "metadata.yaml").read_text())
        assert "agent1" in meta["members"]

        perms = json.loads(perms_file.read_text())
        assert "projects/proj1/*" in perms["permissions"]["agent1"]["blackboard_read"]

    def test_add_agent_idempotent(self, tmp_path):
        projects_dir, perms_file = self._setup(tmp_path)

        with (
            patch("src.cli.config.PROJECTS_DIR", projects_dir),
            patch("src.cli.config.PERMISSIONS_FILE", perms_file),
        ):
            _add_agent_to_project("proj1", "agent1")
            _add_agent_to_project("proj1", "agent1")

        meta = yaml.safe_load((projects_dir / "proj1" / "metadata.yaml").read_text())
        assert meta["members"].count("agent1") == 1

    def test_remove_agent(self, tmp_path):
        projects_dir = tmp_path / "projects"
        proj_dir = projects_dir / "proj1"
        proj_dir.mkdir(parents=True)
        (proj_dir / "metadata.yaml").write_text(yaml.dump({
            "name": "proj1", "members": ["agent1"],
        }))

        perms_file = tmp_path / "permissions.json"
        perms_file.write_text(json.dumps({
            "permissions": {
                "agent1": {
                    "blackboard_read": ["context/*", "projects/proj1/*"],
                    "blackboard_write": ["context/*", "projects/proj1/*"],
                },
            }
        }))

        with (
            patch("src.cli.config.PROJECTS_DIR", projects_dir),
            patch("src.cli.config.PERMISSIONS_FILE", perms_file),
        ):
            _remove_agent_from_project("proj1", "agent1")

        meta = yaml.safe_load((proj_dir / "metadata.yaml").read_text())
        assert "agent1" not in meta["members"]

        perms = json.loads(perms_file.read_text())
        assert "projects/proj1/*" not in perms["permissions"]["agent1"]["blackboard_read"]

    def test_move_agent_between_projects(self, tmp_path):
        projects_dir = tmp_path / "projects"
        for pname in ("proj1", "proj2"):
            d = projects_dir / pname
            d.mkdir(parents=True)
            members = ["agent1"] if pname == "proj1" else []
            (d / "metadata.yaml").write_text(yaml.dump({
                "name": pname, "members": members,
            }))

        perms_file = tmp_path / "permissions.json"
        perms_file.write_text(json.dumps({
            "permissions": {
                "agent1": {
                    "blackboard_read": ["context/*", "projects/proj1/*"],
                    "blackboard_write": ["context/*", "projects/proj1/*"],
                },
            }
        }))

        with (
            patch("src.cli.config.PROJECTS_DIR", projects_dir),
            patch("src.cli.config.PERMISSIONS_FILE", perms_file),
        ):
            _add_agent_to_project("proj2", "agent1")

        # Removed from proj1
        meta1 = yaml.safe_load((projects_dir / "proj1" / "metadata.yaml").read_text())
        assert "agent1" not in meta1["members"]

        # Added to proj2
        meta2 = yaml.safe_load((projects_dir / "proj2" / "metadata.yaml").read_text())
        assert "agent1" in meta2["members"]

        # Permissions updated
        perms = json.loads(perms_file.read_text())
        assert "projects/proj1/*" not in perms["permissions"]["agent1"]["blackboard_read"]
        assert "projects/proj2/*" in perms["permissions"]["agent1"]["blackboard_read"]


class TestBlackboardPermissions:
    def test_add_permissions(self, tmp_path):
        perms_file = tmp_path / "permissions.json"
        perms_file.write_text(json.dumps({
            "permissions": {
                "bot": {"blackboard_read": ["context/*"], "blackboard_write": ["context/*"]},
            }
        }))

        with patch("src.cli.config.PERMISSIONS_FILE", perms_file):
            _add_project_blackboard_permissions("bot", "marketing")

        perms = json.loads(perms_file.read_text())
        assert "projects/marketing/*" in perms["permissions"]["bot"]["blackboard_read"]
        assert "projects/marketing/*" in perms["permissions"]["bot"]["blackboard_write"]

    def test_remove_permissions(self, tmp_path):
        perms_file = tmp_path / "permissions.json"
        perms_file.write_text(json.dumps({
            "permissions": {
                "bot": {
                    "blackboard_read": ["context/*", "projects/marketing/*"],
                    "blackboard_write": ["context/*", "projects/marketing/*"],
                },
            }
        }))

        with patch("src.cli.config.PERMISSIONS_FILE", perms_file):
            _remove_project_blackboard_permissions("bot", "marketing")

        perms = json.loads(perms_file.read_text())
        assert "projects/marketing/*" not in perms["permissions"]["bot"]["blackboard_read"]
        assert "projects/marketing/*" not in perms["permissions"]["bot"]["blackboard_write"]


class TestLoadConfigWithProjects:
    def test_config_includes_projects(self, tmp_path):
        config_file = tmp_path / "mesh.yaml"
        agents_file = tmp_path / "agents.yaml"
        projects_dir = tmp_path / "config" / "projects"

        config_file.write_text(yaml.dump({"mesh": {"port": 8420}}))
        agents_file.write_text(yaml.dump({"agents": {"bot1": {"role": "test"}}}))

        proj_dir = projects_dir / "myproject"
        proj_dir.mkdir(parents=True)
        (proj_dir / "metadata.yaml").write_text(yaml.dump({
            "name": "myproject", "members": ["bot1"],
        }))

        with (
            patch("src.cli.config.CONFIG_FILE", config_file),
            patch("src.cli.config.AGENTS_FILE", agents_file),
            patch("src.cli.config.PROJECTS_DIR", projects_dir),
        ):
            cfg = _load_config(config_file)

        assert "projects" in cfg
        assert "myproject" in cfg["projects"]
        assert cfg["_agent_projects"]["bot1"] == "myproject"

    def test_config_no_projects(self, tmp_path):
        config_file = tmp_path / "mesh.yaml"
        agents_file = tmp_path / "agents.yaml"
        projects_dir = tmp_path / "nonexistent"

        config_file.write_text(yaml.dump({"mesh": {"port": 8420}}))
        agents_file.write_text(yaml.dump({"agents": {}}))

        with (
            patch("src.cli.config.CONFIG_FILE", config_file),
            patch("src.cli.config.AGENTS_FILE", agents_file),
            patch("src.cli.config.PROJECTS_DIR", projects_dir),
        ):
            cfg = _load_config(config_file)

        assert cfg["projects"] == {}
        assert cfg["_agent_projects"] == {}


class TestRemoveAgentCleansProject:
    def test_remove_agent_also_removes_from_project(self, tmp_path):
        projects_dir = tmp_path / "projects"
        proj_dir = projects_dir / "proj1"
        proj_dir.mkdir(parents=True)
        (proj_dir / "metadata.yaml").write_text(yaml.dump({
            "name": "proj1", "members": ["agent1", "agent2"],
        }))

        agents_file = tmp_path / "agents.yaml"
        agents_file.write_text(yaml.dump({
            "agents": {"agent1": {"role": "test"}, "agent2": {"role": "test2"}},
        }))

        perms_file = tmp_path / "permissions.json"
        perms_file.write_text(json.dumps({
            "permissions": {
                "agent1": {
                    "blackboard_read": ["context/*", "projects/proj1/*"],
                    "blackboard_write": ["context/*", "projects/proj1/*"],
                },
                "agent2": {
                    "blackboard_read": ["context/*", "projects/proj1/*"],
                    "blackboard_write": ["context/*", "projects/proj1/*"],
                },
            }
        }))

        with (
            patch("src.cli.config.PROJECTS_DIR", projects_dir),
            patch("src.cli.config.AGENTS_FILE", agents_file),
            patch("src.cli.config.PERMISSIONS_FILE", perms_file),
        ):
            _remove_agent("agent1")

        # agent1 removed from project
        meta = yaml.safe_load((proj_dir / "metadata.yaml").read_text())
        assert "agent1" not in meta["members"]
        assert "agent2" in meta["members"]

        # agent1 removed from agents.yaml
        agents = yaml.safe_load(agents_file.read_text())
        assert "agent1" not in agents["agents"]

        # agent1 removed from permissions
        perms = json.loads(perms_file.read_text())
        assert "agent1" not in perms["permissions"]
