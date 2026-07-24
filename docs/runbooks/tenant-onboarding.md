# Runbook: Onboarding a New Tenant (Law Firm)

Default home for all tenants is **app.vigilist.co**; vanity subdomains
(`firm.vigilist.co`) are an optional white-glove step. thirulaw.vigilist.co
is grandfathered and stays.

## 1. Provision the organization (required, ~1 minute)

From `backend/` with the production environment configured:

```
venv/Scripts/python.exe -m scripts.provision_tenant \
    --slug acme --name "Acme LLP" \
    --domains acme.com \
    --admins managing.partner@acme.com
```

- `--slug` must be lowercase/hyphens and not reserved (`app`, `www`, `api`, …).
- `--domains`: every user whose email is at these domains is automatically an
  org member with `--role` (default `reviewer`) on the org's matters.
- `--admins` (creator_emails): may configure SSO, are exempt from SSO
  enforcement (lockout escape hatch), and new matters they create file under
  this org.
- The action is audit-logged (`org_provisioned`, system provisioning user).

Users at the member domains can now sign in at **app.vigilist.co**
(Google or email/password) and see the org's matters per their role.

## 2. Optional: vanity subdomain

1. Firebase console → Hosting → Add custom domain → `acme.vigilist.co`.
2. Add the DNS records it prints (Cloudflare DNS, DNS-only/grey cloud).
3. Certificate provisions automatically. The login page auto-detects the
   slug for SSO discovery.

Skip unless the firm asks; app.vigilist.co works for everyone (SSO is
discovered from the typed email domain).

## 3. Optional: enterprise SSO

1. GCP console → Identity Platform → Providers → Add SAML (or OIDC);
   name it `saml.acme`. Exchange metadata with the firm's IdP team
   (send them the ACS URL + Entity ID from the console; load their
   metadata XML).
2. Bind it (as an org admin, or re-run provisioning with `--sso`):
   `PUT /api/organizations/acme/sso {"provider_id": "saml.acme"}`
3. Have a firm user test "Sign in with Acme LLP SSO".
4. Enforce: `PUT /api/organizations/acme/sso
   {"provider_id": "saml.acme", "enforced": true}` — member-domain users
   must now use the IdP; `--admins` keep password/Google as the escape hatch.

## 4. First matter

The firm's admins/managers create matters in-app (ingest wizard → new
matter, choosing document source designations per load). Matters created by
org members file under the org automatically; invite external co-counsel
per-matter via the share dialog.

## 5. Offboarding (sketch)

Remove the vanity domain (if any), set `sso_enforced=false`, export their
matters (production packages + CSVs), then delete matters (storage
`delete_prefix` per production) and the org row. A formal certified-deletion
flow is Phase 4 backlog (P4-3).
