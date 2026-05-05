---
trust: high
source: user-stated
created: 2026-02-01
type: knowledge
domain: auth
tags: [password, hashing, argon2, bcrypt]
---

# Password hashing

User passwords are hashed with Argon2id (memory: 64 MiB, parallelism: 2,
iterations: 3). The salt is per-user and stored alongside the hash in
the `users.password_hash` column.

Argon2id was chosen over bcrypt because the memory-hard property
materially raises the cost of GPU-based attacks. The work factor is
audited yearly to keep verification under 250 ms on the production
auth servers.

Legacy bcrypt hashes (cost factor 12) are still accepted on login and
silently upgraded to Argon2id after a successful verify.
