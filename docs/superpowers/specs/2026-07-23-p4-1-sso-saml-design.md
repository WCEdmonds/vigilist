# P4-1 — Enterprise SSO (SAML/OIDC) via Identity Platform

**Date:** 2026-07-23
**Phase:** 4 (Enterprise Trust), sub-project 1
**Depends on:** existing Firebase auth + Organization tenancy (slug
subdomains, member_domains)
**Consumed by:** firm deployments ("we only allow Okta/Entra login").

## Decision context (approved 2026-07-23)

Vigilist's auth is Firebase; upgrading the project to GCP **Identity
Platform** provides SAML and OIDC identity providers as configuration
(providers like `saml.acme` / `oidc.acme` created in the console per
customer IdP). Engineering scope is therefore the per-organization wiring:

- **Per-org provider binding**: `organizations.sso_provider_id`
  (`saml.*`/`oidc.*`) + `sso_enforced`. Enforcement means members of the
  org's email domains MUST authenticate through that provider — checked
  server-side on every request (client hiding is UX only, never security).
- **Login routing**: the login page discovers SSO from the subdomain slug
  on mount, and from the typed email domain on demand, via a public
  (unauthenticated) config endpoint — it must be public because the user
  isn't logged in yet; it discloses only provider id + org display name.
- **Enforcement location**: `get_current_user`, using the ID token's
  `firebase.sign_in_provider` claim vs the org's provider. Google/password
  tokens for an enforced-org domain → 403 with a clear message. Logic
  lives in a testable helper (`enforce_org_sso`), not inline.
- **Management**: `PUT /api/organizations/{slug}/sso` gated to users whose
  email is in the org's `creator_emails` (the existing org-admin-ish
  concept), audited. Enforcing requires a provider id; provider ids must
  match `^(saml|oidc)\.[A-Za-z0-9_-]+$`.
- Escape hatch: `creator_emails` members are EXEMPT from enforcement (the
  admin who misconfigures SSO must still be able to log in and fix it).

## 1. Data model (migration `e7f8a9b0c1d2`, down_revision `d6e7f8a9b0c1`)

`organizations.sso_provider_id` String(100) nullable;
`organizations.sso_enforced` Boolean NOT NULL server_default false.

## 2. Backend

- `GET /api/auth/sso-config?slug=&email=` (public): org by exact slug else
  by email domain in member_domains → `{provider_id, enforced, org_name}`;
  `{provider_id: null}` when none.
- `app/services/sso.py`: `enforce_org_sso(db, email, sign_in_provider)` —
  loads member orgs by domain (reuses `get_member_organizations` shape);
  for the first org with `sso_enforced` and a provider: raise 403 unless
  `sign_in_provider == provider` or email ∈ `creator_emails`. Called from
  `get_current_user` after decode (claim:
  `decoded["firebase"]["sign_in_provider"]`).
- `PUT /api/organizations/{slug}/sso` `{provider_id, enforced}` per
  decision context; audited `org_sso_updated`.

## 3. Frontend

- `useAuth` gains `loginWithSSO(providerId)`: `SAMLAuthProvider` for
  `saml.*`, `OAuthProvider` for `oidc.*`, popup flow + backend sync (same
  shape as Google).
- `AuthPage`: on mount, derive slug from hostname (`x.vigilist.co` → `x`)
  and fetch sso-config; "Sign in with {org} SSO" button when configured;
  when `enforced`, the password/Google forms are hidden for that tenant
  page. A "Use company SSO" affordance fetches config by typed email for
  users on the apex domain. Enforcement errors from the backend surface
  the 403 message.

## 4. Testing

Backend: sso-config resolution (slug hit, email-domain hit, miss),
`enforce_org_sso` (enforced+wrong provider → 403; matching provider ok;
unenforced ok; creator exempt; no-org ok), PUT gating (creator ok,
member 403, validation 422s, enforce-without-provider 422). Frontend:
build gate. Ops runbook note in the spec: creating the actual
SAML/OIDC providers happens in the GCP console per customer (documented
step list), not in code.

## Ops runbook (per customer onboarding)

1. GCP console → Identity Platform → Providers → add SAML (or OIDC),
   name it `saml.<orgslug>`; exchange metadata with the firm's IdP
   (ACS URL + entity id from the console; their metadata XML in).
2. `PUT /api/organizations/{slug}/sso {"provider_id": "saml.<orgslug>"}`.
3. Test login from the firm subdomain, then set `"enforced": true`.

## Out of scope

- SCIM provisioning, granular RBAC, IP allowlisting, session policies
  (P4-2); Identity Platform multi-tenancy (single-tenant pool with
  per-org providers is sufficient at this scale).
