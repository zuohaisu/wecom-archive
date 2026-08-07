# Security Policy

Crowntime WeCom Archive is operated as a hosted service. It processes WeCom
conversation data on behalf of customer companies and holds their archive
decryption keys, so security reports are handled with priority over all other
work.

## Reporting a vulnerability

Do not disclose a suspected vulnerability in a public channel. Report it
privately to the maintainers of this repository and wait for acknowledgement
before discussing it elsewhere.

Include a clear description, the affected environment or commit, reproduction
steps, and the potential impact. **Do not include production credentials,
private keys, customer conversation content, or personal data in a report** —
describe the access path instead of demonstrating it with real data.

## Severity guidance

The following are treated as critical and page immediately:

- Any exposure of archive private key material or `app_secret` values
- Any cross-tenant data access — one customer able to read another's
  conversations, media, contacts, or configuration
- Authentication or session bypass on the administrator console
- Unauthenticated access to media objects or export endpoints

## Supported versions

The hosted service runs from the current default branch. There are no
supported self-hosted releases: the product is not distributed to customers,
so fixes are delivered by deploying, not by publishing an advisory.
