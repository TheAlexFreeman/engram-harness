---
trust: high
source: user-stated
created: 2026-01-15
type: knowledge
domain: auth
tags: [session, tokens, jwt, validation]
---

# Session token storage and validation

Session tokens are JWTs signed with HS256. They live in an httpOnly secure
cookie named `sid`. Validation runs on every authenticated request:

1. Extract the `sid` cookie value.
2. Verify the HMAC signature against the rotating signing key in
   `secrets/jwt-signing-key.txt`.
3. Decode the payload and reject if `exp` is in the past or `iat` is more
   than 24 hours old.
4. Look up the `sub` (user id) in the `sessions` table; reject if the
   session has been revoked.

Tokens are reissued on every successful request with a sliding expiry of
two hours from the last activity timestamp.
