"""Node-local subsystems: git mirror/worktree, compose provisioner, cleanup.

Everything under ``awf.node`` talks to the local host (filesystem, git CLI,
Docker daemon). The control plane (``awf.api`` + ``awf.control``) must not
import this package directly — it hands work off via the Provisioner interface.
"""
