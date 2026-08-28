# Architecture Index — Crowntime WeCom Archive

> **Current architecture source:** [Current Architecture Map](architecture/current-state.md)
> and the [Runtime Architecture Debt Register](architecture/runtime-debt.md).
>
> This index supersedes the former internal-only/single-tenant architecture
> overview. It does not change runtime behavior.

## Start here

1. Read [Current Architecture Map](architecture/current-state.md) for the
   hosted multi-tenant product boundary, identity/tenant model, WeCom callback
   separation, archive runtime modes, authority map, contradiction inventory,
   and documentation hierarchy.
2. Read [Runtime Architecture Debt Register](architecture/runtime-debt.md)
   before changing a compatibility path. `TRANSITIONAL` paths require their
   listed retirement evidence and separate approval; they are not cleanup.
3. Use executable sources—models, services, routers, worker scripts,
   migrations, tests, `.env.example`, deployment scripts, and unit manifests—
   to resolve implementation questions. When prose conflicts with executable
   behavior, the executable source wins and the prose needs correction.

## Related authoritative documents

| Concern | Source |
| --- | --- |
| Contribution, safety, architecture-boundary, ticket, and validation rules | [`AGENTS.md`](../AGENTS.md) |
| Environment-variable contract | [`.env.example`](../.env.example) |
| Deployment asset ownership and CD behavior | [`DEPLOYMENT.md`](DEPLOYMENT.md) |
| Third-party provider authorization and isolated callbacks | [`operations/wecom-third-party-authorization.md`](operations/wecom-third-party-authorization.md) |
| Non-production isolation | [`operations/nonproduction-deployment.md`](operations/nonproduction-deployment.md) |
| Product strategy | [`adr/0003-product-strategy-hosted-only.md`](adr/0003-product-strategy-hosted-only.md) |
| Billing, refund, and service lifecycle decisions | [`adr/0004-annual-plan-wechat-pay-gates.md`](adr/0004-annual-plan-wechat-pay-gates.md), [`adr/0005-saas-billing-lifecycle-refunds-and-service-gates.md`](adr/0005-saas-billing-lifecycle-refunds-and-service-gates.md), and [`adr/0006-alipay-pc-page-pay.md`](adr/0006-alipay-pc-page-pay.md) |

GitHub Issues are the active issue-management system. Historical Linear links
or RND identifiers are provenance, not active workflow instructions.
