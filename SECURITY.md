# Security Policy

AWF Core is a local deterministic workspace fabric. It can run coding agents,
package installers, Docker containers, and Git/GitHub operations on the machine
where it is installed, so treat an AWF host like an engineering workstation with
automation privileges.

## Supported Scope

Security fixes are accepted for the current `development` branch and the latest
tagged local Core release once public releases begin. Future hosted or cloud
offerings will publish separate support boundaries.

## Local Core Trust Boundary

- AWF Core controls Docker workspace lifecycle, logs, validation, cleanup, and PR
  monitor orchestration.
- Workspace agents may interact with the internet when the active profile allows
  egress.
- Provider keys, GitHub credentials, and package-registry tokens are local
  operator credentials. Do not paste them into prompts, logs, issues, or PRs.
- AWF does not yet provide a cloud secret broker or hardened network sandbox for
  local Core. Use profile-level egress and secret declarations to document the
  intended posture.

See `docs/AWF_CORE_TRUST_MODEL.md` for the full local trust model.

## Reporting A Vulnerability

Please report security issues privately by emailing `security@aira.pro` with:

- affected commit or release;
- reproduction steps;
- expected impact;
- relevant logs with secrets redacted.

Do not open a public GitHub issue for suspected credential exposure, sandbox
escape, supply-chain compromise, or privilege escalation.

## BYOK Guidance

For bring-your-own-key usage, prefer narrowly scoped provider and GitHub tokens,
rotate keys used in demonstrations, and avoid mounting broad host-home secrets
into workspaces unless the profile explicitly requires it.
