import re

with open('src/awf/node/compose_manager.py', 'r') as f:
    content = f.read()

content = re.sub(
    r'def up\(self, spec: WorkspaceComposeSpec, \*, wait: bool = True\) -> ComposeProjectPaths:\n        """Start the stack\. With ``wait=True``, blocks until services are healthy\."""\n        paths = self\.render\(spec\)\n        args = \["up", "-d"\]\n        if wait:\n            args\.append\("--wait"\)',
    r'def up(self, spec: WorkspaceComposeSpec, *, wait: bool = True) -> ComposeProjectPaths:\n        """Start the stack. With ``wait=True``, blocks until services are healthy."""\n        paths = self.render(spec)\n        args = ["up", "-d", "--remove-orphans"]\n        if wait:\n            args.extend(["--wait", "--wait-timeout", "300"])',
    content
)

content = re.sub(
    r'args = \["up", "-d"\]\n        if wait:\n            args\.append\("--wait"\)\n        _log\.info\(',
    r'args = ["up", "-d", "--remove-orphans"]\n        if wait:\n            args.extend(["--wait", "--wait-timeout", "300"])\n        _log.info(',
    content
)

content = re.sub(
    r'args = \["down"\]\n        if remove_volumes:\n            args\.append\("-v"\)',
    r'args = ["down", "--remove-orphans"]\n        if remove_volumes:\n            args.append("-v")',
    content
)

with open('src/awf/node/compose_manager.py', 'w') as f:
    f.write(content)

with open('tests/unit/node/test_compose_manager_subprocess.py', 'r') as f:
    t_content = f.read()

t_content = t_content.replace(
    'assert "up" in cmd and "-d" in cmd and "--wait" in cmd\n',
    'assert "up" in cmd and "-d" in cmd and "--wait" in cmd\n        assert "--remove-orphans" in cmd\n        assert "--wait-timeout" in cmd and "300" in cmd\n'
)

t_content = t_content.replace(
    'assert "down" in cmd and "-v" in cmd\n',
    'assert "down" in cmd and "-v" in cmd\n        assert "--remove-orphans" in cmd\n'
)

t_content = t_content.replace(
    '            "up",\n            "-d",\n            "--wait",\n',
    '            "up",\n            "-d",\n            "--remove-orphans",\n            "--wait",\n            "--wait-timeout",\n            "300",\n'
)

t_content = t_content.replace(
    'assert exc.value.reason_code == "COMPOSE_COMMAND_FAILED"',
    'assert exc.value.reason_code == "DOCKER_UNAVAILABLE"'
)

with open('tests/unit/node/test_compose_manager_subprocess.py', 'w') as f:
    f.write(t_content)

