# %%
# Imports #

import json
import os

import yaml
from utils import statusboard_tools, wizard_tools

# %%
# Helpers #


def make_credentials_repo(root, context, panels=None, hosts=None):
    """Create a fake <context>_credentials repo with optional statusboard config and inventory."""
    repo = os.path.join(str(root), f"{context}_credentials")
    os.makedirs(repo, exist_ok=True)
    if panels is not None:
        with open(os.path.join(repo, f"{context}_statusboard.yaml"), "w", encoding="utf-8") as file_handle:
            yaml.safe_dump(panels, file_handle)
    if hosts is not None:
        with open(os.path.join(repo, f"{context}_hosts.json"), "w", encoding="utf-8") as file_handle:
            json.dump({"hosts": hosts}, file_handle)
    return repo


SSH_PANEL = {"name": "vm_jobs", "type": "ssh_command", "host": "sshvm", "command": "bash x.sh"}


# %%
# Target discovery #


def test_wizard_targets_lists_root_config_and_all_credentials_repos(tmp_path):
    repo_with = make_credentials_repo(tmp_path, "acme", panels=[])
    repo_without = make_credentials_repo(tmp_path, "beta")  # no config yet - still a valid target
    repo_root = os.path.join(str(tmp_path), "status_board")
    os.makedirs(repo_root)
    targets = wizard_tools.wizard_targets(str(tmp_path), repo_root)
    assert [target["base_dir"] for target in targets] == [repo_root, repo_with, repo_without]
    assert [target["exists"] for target in targets] == [False, True, False]
    # only the repo-root config is tracked/public; credentials-repo overlays are private
    assert [target["public"] for target in targets] == [True, False, False]
    assert targets[2]["path"].endswith("beta_statusboard.yaml")


def test_wizard_targets_without_repo_root(tmp_path):
    make_credentials_repo(tmp_path, "acme", panels=[])
    targets = wizard_tools.wizard_targets(str(tmp_path))
    assert len(targets) == 1
    assert targets[0]["context"] == "acme"


# %%
# Existing names #


def test_existing_panel_names_spans_all_configs(tmp_path):
    make_credentials_repo(tmp_path, "acme", panels=[SSH_PANEL])
    make_credentials_repo(tmp_path, "beta", panels=[{"name": "gh", "type": "github_prs", "token_env": "T"}])
    assert wizard_tools.existing_panel_names(str(tmp_path)) == {"vm_jobs", "gh"}


def test_existing_panel_names_tolerates_broken_configs(tmp_path):
    make_credentials_repo(tmp_path, "acme", panels=[{"name": "x", "type": "nope"}])
    assert wizard_tools.existing_panel_names(str(tmp_path)) == set()


# %%
# YAML formatting / appending #


def test_format_panel_yaml_orders_fields():
    panel = {"interval": 300, "command": "bash x.sh", "type": "ssh_command", "name": "vm", "host": "sshvm"}
    block = wizard_tools.format_panel_yaml(panel)
    keys = [line.split(":")[0].strip("- ") for line in block.strip().splitlines()]
    assert keys == ["name", "type", "host", "command", "interval"]


def test_format_panel_yaml_drops_private_keys_keeps_unknown():
    panel = {"name": "vm", "type": "ssh_command", "host": "h", "command": "c", "_base_dir": "/x", "custom": 1}
    block = wizard_tools.format_panel_yaml(panel)
    assert "_base_dir" not in block
    assert "custom: 1" in block


def test_append_panel_creates_file_then_appends_preserving_content(tmp_path):
    config_path = os.path.join(str(tmp_path), "acme_statusboard.yaml")
    wizard_tools.append_panel(config_path, SSH_PANEL)
    assert os.path.exists(config_path)
    with open(config_path, "r", encoding="utf-8") as file_handle:
        first = file_handle.read()
    assert first.startswith("#")  # header comment on the fresh file

    # hand-edit the file (a comment the wizard must not destroy), then append a second panel
    with open(config_path, "a", encoding="utf-8") as file_handle:
        file_handle.write("# keep me\n")
    second_panel = {"name": "gh", "type": "github_prs", "token_env": "T"}
    wizard_tools.append_panel(config_path, second_panel)
    with open(config_path, "r", encoding="utf-8") as file_handle:
        content = file_handle.read()
    assert "# keep me" in content
    loaded = yaml.safe_load(content)
    assert [panel["name"] for panel in loaded] == ["vm_jobs", "gh"]


def test_append_panel_output_loads_through_the_real_loader(tmp_path):
    """A wizard-written panel round-trips through load_panels with no schema complaints."""
    repo = make_credentials_repo(tmp_path, "acme")
    config_path = os.path.join(repo, "acme_statusboard.yaml")
    panel = dict(SSH_PANEL, host_stats=True, log_link={"pattern": r"^(\S+)", "command": "tail -F {job}.log"})
    wizard_tools.append_panel(config_path, panel)
    panels, config_paths = statusboard_tools.load_panels(str(tmp_path))
    assert config_paths == [config_path]
    assert panels[0]["name"] == "vm_jobs"
    assert panels[0]["host_stats"] is True
    assert panels[0]["log_link"]["command"] == "tail -F {job}.log"
    assert panels[0]["interval"] == statusboard_tools.DEFAULT_INTERVALS["ssh_command"]


def test_append_panel_handles_missing_trailing_newline(tmp_path):
    config_path = os.path.join(str(tmp_path), "statusboard.yaml")
    with open(config_path, "w", encoding="utf-8") as file_handle:
        file_handle.write("- name: old\n  type: github_prs\n  token_env: T")  # no trailing newline
    wizard_tools.append_panel(config_path, SSH_PANEL)
    with open(config_path, "r", encoding="utf-8") as file_handle:
        loaded = yaml.safe_load(file_handle)
    assert [panel["name"] for panel in loaded] == ["old", "vm_jobs"]


# %%


def test_format_panel_yaml_http_checks_round_trips():
    panel = {
        "name": "acme_sites", "type": "http_checks", "interval": 120, "host": "sshvm", "jump": "jump1",
        "sites": [{"url": "https://intranet.acme.internal/", "name": "intranet", "insecure": True},
                  {"url": "http://10.0.0.20:8000/api/health", "expect": [200, 401]}],
    }
    block = wizard_tools.format_panel_yaml(panel)
    lines = block.splitlines()
    assert lines[0] == "- name: acme_sites"
    assert lines[1] == "  type: http_checks"
    assert lines.index("  host: sshvm") < lines.index("  sites:") < lines.index("  interval: 120")
    assert yaml.safe_load(block) == [panel]
