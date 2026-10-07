# Operations Guide for Self-Hosted Deployments

This guide describes general operational practices for an independently managed
installation. It is **not** a runbook for Crowntime's production environment:
company-specific hosts, credentials, service overrides, and incident procedures
are not distributed. Use the versioned assets as references and adapt them to
your own infrastructure.

## Least-privilege deployment

Start with the repository's [deployment guide](DEPLOYMENT.md) and
[environment-variable example](../.env.example). Keep secrets in a protected
configuration store, not in source control, shell history, issue reports, or
logs. Use a dedicated service identity and grant only the host operations your
deployment actually needs. Do not copy another operator's sudoers rules,
service overrides, or filesystem paths without reviewing them for your host.

The versioned [deployment script](../scripts/deploy_server.sh) and its comments
are implementation references, not authorization to deploy this project or to
reuse another environment's configuration.

## Scheduled workloads

[`WORKLOAD_MANIFEST`](../deploy/systemd/WORKLOAD_MANIFEST) is the repository's
classification of scheduled and manually operated units;
[`MANAGED_UNITS`](../deploy/systemd/MANAGED_UNITS) lists units managed by the
versioned deploy flow. Review the classification before enabling a unit—do not
bulk-enable every service or timer.

[`assert_scheduled_workloads.sh`](../scripts/assert_scheduled_workloads.sh)
checks repository consistency by default. Its `--server` mode inspects the
systemd state of the host on which it is run; use it only on infrastructure you
administer and review its output locally.

## Backups and recovery

The repository includes a [backup helper](../scripts/backup_once.sh) and
versioned backup units. Configure a destination, retention policy, encryption,
and access controls appropriate to your deployment. Keep recovery material
separate from the application host, restrict access to it, and periodically
verify restoration in an isolated environment. A successful backup job alone
does not prove that recovery works.

Do not place backup passphrases, private keys, database URLs, or customer data
in Git, CI logs, or support requests. This repository does not provide or
configure an off-host destination for your installation.

## Alerts and uptime

The [disk-usage check](../scripts/disk_usage_check.sh) and the
[uptime workflow](../.github/workflows/uptime-check.yml) are examples of
separate host-local and external checks. The workflow is manually dispatched;
it requires an explicit HTTPS `UPTIME_BASE_URL` repository variable and has no
Crowntime production URL default. Configure any webhook as a protected secret
and use an endpoint you control.

Alerts are diagnostic signals, not proof that every worker completed
successfully. Monitor worker exit status and recent application/database health
as appropriate for your own service-level requirements.

## Background workers

The versioned systemd units and worker entry points are listed in the
[deployment guide](DEPLOYMENT.md#repo-owned-deployment-assets) and workload
manifest. On your own host, inspect the unit configuration, service status, and
journal using your platform's normal tools. Confirm that the service account,
working directory, environment, database access, and persistent storage match
your installation before enabling a scheduled trigger.

Detailed production runbooks are not included in this public snapshot. The
unit files, scripts, tests, and this generic guide are the available
references; they do not imply that the project's production settings are safe
or suitable for another operator.

## Worker diagnostics

For a worker that is not progressing, first inspect its exit status and recent
journal entries on your own host; then compare its effective unit configuration
with the versioned unit and check the relevant application health endpoint.
Avoid copying logs that contain identifiers, message content, credentials, or
customer data into public reports.

## WeCom authorization and callbacks

For a self-hosted installation, configure the enterprise application and
callback values for your own WeCom tenant using the
[customer integration guide](kb/customer/wecom-integration.md). Keep callback
tokens and encryption keys secret, validate callback ownership, and use HTTPS.
Cloud provider authorization and its production callback procedures are not
included in this public operations reference.

## Non-production isolation

Validate changes in a separate non-production environment with synthetic or
approved test data. Keep its database, credentials, storage, webhook endpoints,
and service identity separate from production. The hosted staging workflow and
its host-specific resource names are not included in this snapshot.

## Static site

The public marketing pages and static demo are under
[`static_site/company_homepage/`](../static_site/company_homepage/index.html).
Configure your own webroot and reverse-proxy document root consistently; the
company's deployment directory and Nginx configuration are not included.

## Object storage

Choose a storage backend supported by the application and configure only the
credentials for your own account. The [environment example](../.env.example)
identifies the relevant variables. Provider-specific research and the
company's bucket configuration are not included; do not reuse another
operator's keys, bucket, or domain.

## TLS and provider-specific tooling

TLS termination and certificate renewal are deployment choices. Use a
certificate workflow appropriate to your domain, provider, and host. The
repository's [`ssl-renew/` tooling](../ssl-renew/README.md) is provider- and
configuration-specific; review its scripts and example files before adapting
it. Never reuse another operator's DNS, certificate, or storage credentials.

## Email and cloud-only operations

Configure email integrations through the variables documented in
[`.env.example`](../.env.example), using credentials issued for your own
service. Cloud payment and billing workers are separate from the self-hosted
archive path; consult the relevant public [billing architecture decision](adr/0005-saas-billing-lifecycle-refunds-and-service-gates.md)
before changing their behavior. Do not enable cloud payment operations as a
substitute for configuring a self-hosted installation.
