"""Public bridge for MCP first-run tools that share CLI helper behavior.

The setup/start/init command modules keep their implementation helpers private.
This module is the stable import surface for MCP tools that must reuse those
helpers without importing command-module internals directly.
"""

from __future__ import annotations

from awf.cli import init_ops as _init_ops
from awf.cli import setup_commands as _setup_commands
from awf.cli import start_commands as _start_commands

# NOTE: These aliases expose private CLI symbols as the stable MCP import surface.
# Keep this bridge in sync when refactoring setup/start/init command internals.
DEFAULT_START_TIMEOUT_SECONDS = _start_commands._DEFAULT_START_TIMEOUT_SECONDS
StartBootstrapInputs = _start_commands._StartBootstrapInputs
build_init_project_onboarding_payload = _init_ops._init_project_onboarding_payload
client_setup_environ = _setup_commands._client_env
client_setup_home = _setup_commands._client_home
client_setup_now = _setup_commands._client_now
client_setup_which = _setup_commands._client_which
client_source_checkout_blocked_payload = _setup_commands._client_source_checkout_blocked_payload
existing_project_profile_path = _init_ops._existing_project_profile_path
resolve_client_env_file = _setup_commands._resolve_client_env_file
resolve_start_bootstrap_inputs = _start_commands._resolve_start_bootstrap_inputs
resolve_start_source_checkout = _start_commands._resolve_start_source_checkout
run_setup_readiness = _setup_commands._run_setup
setup_config_error_details = _setup_commands._config_error_details
setup_reason_coded_payload = _setup_commands._reason_coded_payload
source_checkout_failure_payload = _start_commands._source_checkout_failure_payload
start_failure_payload = _start_commands._start_failure_payload
start_success_payload = _start_commands._start_success_payload

__all__ = (
    "DEFAULT_START_TIMEOUT_SECONDS",
    "StartBootstrapInputs",
    "build_init_project_onboarding_payload",
    "client_setup_environ",
    "client_setup_home",
    "client_setup_now",
    "client_setup_which",
    "client_source_checkout_blocked_payload",
    "existing_project_profile_path",
    "resolve_client_env_file",
    "resolve_start_bootstrap_inputs",
    "resolve_start_source_checkout",
    "run_setup_readiness",
    "setup_config_error_details",
    "setup_reason_coded_payload",
    "source_checkout_failure_payload",
    "start_failure_payload",
    "start_success_payload",
)
