# Closed Linux Host Composition

This opt-in composition observes Linux, Docker and a dedicated Core PostgreSQL
database. It starts no executor, worker, recovery task or credential broker.
Local Core defaults remain unchanged. `runtime_ready` remains false, including
after recovery acknowledgement. Hosted worker startup refuses execution until
the separate runtime integration is implemented and qualified.

## Release Boundary

Build the complete reviewed Core revision, including migration `ca97d3e4f506`,
through the normal Core CI/CD producer. Consume its immutable image digest and
verified provenance in the infrastructure release. Never replace this with a
locally built image, partial source overlay or direct production deployment.
Migration and runtime credentials must be distinct. Run the normal migration
stage once, then start one API process with its restricted runtime credential.
This contract neither authorizes a production migration nor grants cloud IAM.

## Required Host Inputs

The infrastructure composition must provide all of the following:

- The entire retained filesystem at its real absolute path, its block-device
  identity, filesystem UUID, mount metadata and actual Linux boot ID.
- Docker's actual data-root inside that filesystem. An image cache alone is
  not a retained Docker data-root. Core does not move data or restart Docker.
- A dedicated cgroup-v2 execution subtree, covering every task, monitor,
  maintenance, checkpoint, upgrade and recovery process. API and Docker control
  processes stay outside it. Idle requires `populated 0` and `frozen 0`.
- A trusted read-only Docker observation transport. Unknown, stopped, paused or
  restarting containers, active exec sessions and non-allowlisted containers
  deny idle. Labels alone are not an exemption.
- The API container's full container ID and actual inspected image ID, plus
  `awf.node.role=control-plane` and its exact logical-owner label. A trusted
  create/configure/start sequence binds these before settings load.
- A node-specific database and non-owner runtime role with necessary DML only.
  The runtime role cannot create objects, bypass triggers, truncate tables,
  assume other roles or execute privileged security-definer routines.
- Separate controller authentication and private authenticated TLS transport.
  Credentials are never caller-supplied URLs, command-line arguments or logs.

The deployment must prevent launchers outside the observed execution subtree.
An empty cgroup alone cannot prove that deployment invariant.

## Settings And Durable Metadata

`HostedSettings.host` requires `HostedSettings.lifecycle`. The trusted bootstrap
provides exact owner, numeric physical incarnation, positive recovery generation,
retained disk/database references, absolute host paths and database identity.
It also supplies these root-owned metadata files:

| File | Required fields |
| --- | --- |
| `state_root/.awf-node.json` | `owner`, `disk_ref`, `database_ref`, `filesystem_uuid` |
| Per-boot identity file | `owner`, `incarnation`, `recovery_generation`, current `boot_id` |
| First-node approval | `kind=empty-node-v1`, owner/incarnation/generation, disk/database/filesystem identity, database name and opaque receipt reference |

Files must be regular, single-link, at most 8192 bytes and root-owned without
write bits. Parent paths must be root-owned, without group/other writes or
symlinks. Core holds a nonblocking flock on a pre-created root-owned,
non-writable `state_root/.awf-host.lock` for the entire API lifespan. A second
API process fails before advancing the boot epoch.

Core validates the database endpoint supplied by trusted configuration; it does
not discover endpoints or attest a database clone from its name. PUBLIC ACL
isolation and migration-role provisioning belong to reviewed infrastructure
setup. Core never changes unrelated databases or provider-owned catalogs.

## Recovery And Cloud Integration

Startup closes admission and advances the persisted boot epoch. The controller
must acknowledge recovery using exact identity headers and an idempotency key.
An opaque receipt reference is not evidence by itself:

- A first empty node needs matching protected approval and actual empty durable
  work, filesystem, Docker and cgroup observations.
- Same-VM recovery requires a newer epoch and fresh host/database quiescence.
  Expired or unknown claims still block it.
- Replacement requires trusted evidence that previous execution and restart
  authority are fenced. There is no default physical-fence verifier. Heartbeat
  expiry, a stopped status or a local approval file cannot substitute for one.

`POST /v1/hosted-node/lifecycle/boot/evidence` binds a canonical UUID challenge
to current durable identity, prior identity, Linux boot ID, storage references
and fresh observations with a 30-second expiry. It never opens admission.
Cloud must compare this with provider identity and durable wake history before
acknowledging recovery. No database transaction may span that HTTP exchange.
Unknown outcomes stay reserved and require same-key reconciliation.

## Acceptance Limits

The tests exercise actual Core handlers and PostgreSQL with synthetic external
boundaries. They do not certify a live VM, provider fencing, automatic stop/wake
or paid task execution. Qualification must use normally released images and
real host/provider observations. Keep execution disabled and leave an idle
qualification VM stopped. A passing health endpoint is not runtime readiness.
