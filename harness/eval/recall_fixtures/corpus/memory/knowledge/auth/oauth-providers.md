---
trust: medium
source: agent-generated
created: 2026-03-10
type: knowledge
domain: auth
tags: [oauth, sso, google, github]
---

# OAuth provider integration

Supported third-party OAuth providers: Google, GitHub, and Microsoft
(Entra ID). Each provider is configured under `config/oauth/<provider>.yaml`
with the client id, client secret reference, and redirect URI.

The OAuth callback handler at `/auth/oauth/callback` validates the state
parameter, exchanges the authorization code for an access token, fetches
the user profile, and either creates a new user or links to the existing
account on an email match.

OAuth-authenticated users still receive a local session token after
sign-in; we do not pass third-party access tokens through to downstream
services.
