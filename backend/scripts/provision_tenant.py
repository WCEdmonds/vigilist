"""Provision a new tenant organization (ops CLI).

Usage (from backend/, with the app's environment configured):

    venv/Scripts/python.exe -m scripts.provision_tenant \
        --slug acme --name "Acme LLP" \
        --domains acme.com acmellp.com \
        --admins managing.partner@acme.com \
        [--role reviewer] [--sso saml.acme] [--enforce]

Prints the follow-up onboarding steps (see docs/runbooks/tenant-onboarding.md).
"""

import argparse
import asyncio
import sys

from app.database import async_session
from app.services.provisioning import ProvisioningError, provision_tenant


async def main() -> int:
    p = argparse.ArgumentParser(description="Provision a tenant organization")
    p.add_argument("--slug", required=True, help="subdomain-safe identifier, e.g. acme")
    p.add_argument("--name", required=True, help='display name, e.g. "Acme LLP"')
    p.add_argument("--domains", required=True, nargs="+", help="member email domains")
    p.add_argument("--admins", nargs="*", default=[], help="creator/admin emails")
    p.add_argument("--role", default="reviewer", help="member role (default reviewer)")
    p.add_argument("--sso", default=None, help="Identity Platform provider id (saml.x / oidc.x)")
    p.add_argument("--enforce", action="store_true", help="enforce SSO for member domains")
    args = p.parse_args()

    async with async_session() as db:
        try:
            org = await provision_tenant(
                db, slug=args.slug, name=args.name,
                member_domains=args.domains, member_role=args.role,
                creator_emails=args.admins,
                sso_provider_id=args.sso, sso_enforced=args.enforce,
            )
        except ProvisioningError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1

    print(f"Provisioned organization #{org.id}: {org.name} ({org.slug})")
    print(f"  member domains : {', '.join(org.member_domains)}")
    print(f"  member role    : {org.member_role}")
    print(f"  admins         : {', '.join(org.creator_emails) or '(none)'}")
    print(f"  sso            : {org.sso_provider_id or '(none)'}"
          f"{' ENFORCED' if org.sso_enforced else ''}")
    print()
    print("Next steps (docs/runbooks/tenant-onboarding.md):")
    print("  1. Users at the member domains can sign in at app.vigilist.co now.")
    print("  2. Optional vanity subdomain: add the custom domain in Firebase Hosting.")
    print("  3. Optional SSO: create the provider in Identity Platform, then bind/enforce.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
