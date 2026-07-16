"""Unit tests for organization-based access resolution.

These use a lightweight fake session so they run without a database. The
full integration (migration seed/backfill + real ARRAY columns) is exercised
separately against Postgres; here we lock in the pure resolution logic:
email-domain parsing, role precedence, and creator auto-assignment.
"""

import asyncio

import pytest
from fastapi import HTTPException

from app.dependencies import (
    email_domain,
    get_user_role_for_production,
    resolve_org_for_creator,
)


class FakeUser:
    def __init__(self, uid, email):
        self.id = uid
        self.email = email


class FakeOrg:
    def __init__(self, oid, member_role, member_domains, creator_emails):
        self.id = oid
        self.member_role = member_role
        self.member_domains = member_domains
        self.creator_emails = creator_emails


class FakeProduction:
    def __init__(self, pid, owner_id, organization_id):
        self.id = pid
        self.owner_id = owner_id
        self.organization_id = organization_id


class FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._value


class FakeSession:
    """Minimal AsyncSession stand-in.

    - `get(Model, key)` resolves from `objects[(Model.__name__, key)]`.
    - `execute(...)` returns queued results in FIFO order (for the
      ProductionAccess role lookup and the Organization list query).
    """

    def __init__(self, objects, execute_results):
        self._objects = objects
        self._execute_results = list(execute_results)

    async def get(self, model, key):
        return self._objects.get((model.__name__, key))

    async def execute(self, *_args, **_kwargs):
        return self._execute_results.pop(0)


THIRULAW = FakeOrg(1, "manager", ["thirulaw.com"], ["wcedmonds28@gmail.com"])


def test_email_domain():
    assert email_domain("Alice@Thirulaw.com") == "thirulaw.com"
    assert email_domain("wcedmonds28@gmail.com") == "gmail.com"
    assert email_domain("noemail") == ""
    assert email_domain("") == ""
    assert email_domain(None) == ""


def test_owner_is_admin_regardless_of_org():
    user = FakeUser("u1", "alice@thirulaw.com")
    prod = FakeProduction(5, owner_id="u1", organization_id=1)
    session = FakeSession({("Production", 5): prod}, [])
    role = asyncio.run(get_user_role_for_production(session, user, 5))
    assert role == "admin"


def test_org_member_gets_member_role():
    user = FakeUser("u2", "alice@thirulaw.com")
    prod = FakeProduction(5, owner_id="someone_else", organization_id=1)
    session = FakeSession(
        {("Production", 5): prod, ("Organization", 1): THIRULAW},
        [FakeScalarResult(None)],  # no explicit ProductionAccess row
    )
    role = asyncio.run(get_user_role_for_production(session, user, 5))
    assert role == "manager"


def test_role_is_additive_max_of_explicit_and_org():
    user = FakeUser("u3", "alice@thirulaw.com")
    prod = FakeProduction(5, owner_id="someone_else", organization_id=1)
    # explicit grant is readonly, org grants manager -> effective manager
    session = FakeSession(
        {("Production", 5): prod, ("Organization", 1): THIRULAW},
        [FakeScalarResult("readonly")],
    )
    role = asyncio.run(get_user_role_for_production(session, user, 5))
    assert role == "manager"


def test_non_member_with_no_grant_is_denied():
    user = FakeUser("u4", "bob@other.com")
    prod = FakeProduction(5, owner_id="someone_else", organization_id=1)
    session = FakeSession(
        {("Production", 5): prod, ("Organization", 1): THIRULAW},
        [FakeScalarResult(None)],
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_user_role_for_production(session, user, 5))
    assert exc.value.status_code == 403


def test_resolve_org_by_member_domain():
    user = FakeUser("u5", "alice@thirulaw.com")
    session = FakeSession({}, [FakeScalarResult([THIRULAW])])
    assert asyncio.run(resolve_org_for_creator(session, user)) == 1


def test_resolve_org_by_creator_email():
    user = FakeUser("u6", "wcedmonds28@gmail.com")  # gmail, not a member domain
    session = FakeSession({}, [FakeScalarResult([THIRULAW])])
    assert asyncio.run(resolve_org_for_creator(session, user)) == 1


def test_resolve_org_none_for_outsider():
    user = FakeUser("u7", "bob@other.com")
    session = FakeSession({}, [FakeScalarResult([THIRULAW])])
    assert asyncio.run(resolve_org_for_creator(session, user)) is None
