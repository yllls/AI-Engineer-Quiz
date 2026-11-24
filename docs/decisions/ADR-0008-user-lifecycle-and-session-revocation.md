# ADR-0008: User lifecycle and session revocation

**Status:** accepted

## Context

The backend already had a partial `PATCH /api/users/{uid}` route, but the UI
only offered user creation through prompts. There was no UI for editing or
removing users, no role validation, no protection for the last administrator,
and no way to permanently delete an account. Tokens are stateless and
role changes already take effect through the database row, but a password reset
would leave old tokens valid until expiry.

## Decision

Replace the prompt flow with a shared create/edit form. Username remains
read-only. Admins can change email, full name, role, active state, and an
optional new password. Server-side validation enforces known roles and fields,
self-protection, and a remaining active administrator.

Normal removal is a reversible deactivation. Re-enabling the account does not
revive tokens issued before deactivation. Permanent deletion is available only
for inactive users, requires a typed username confirmation in the UI, and clears
uploader, version creator, collection creator, share creator, and saved-search
references. Audit history is retained with its username snapshot.

Add `users.token_version`. Issue it in new tokens, compare it during request
authentication, and increment it on password reset and deactivation. This gives
a stateless token system immediate revocation without adding a session store.

## Consequences

**Good.** Admins can correct account data, reset passwords, and remove accounts
without breaking asset ownership or audit history. Deactivation and password
reset log out stale sessions. Self and last-admin safeguards prevent lockout.

**The cost.** A schema migration and token payload change are required. Existing
tokens without a version claim are treated as version 0 so the first upgrade
does not force an immediate login, but a password reset or disable does. Hard
deletion is intentionally a two-step operation (disable, then delete) and is
restricted to inactive users. Username changes, SSO, two-factor authentication,
password complexity policy, and delegated user administration remain out of
scope.
