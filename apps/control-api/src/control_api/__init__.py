"""Squire control plane.

The "boring monolith" from PRD §4: it owns the tenant registry, the Telegram bot
pool, and the provisioning state machine that drives Railway's GraphQL API.

Deliberate non-responsibilities (they are what make the privacy claim structural):
  * it never sees a conversation,
  * it never stores a plaintext *user* credential,
  * it never holds a tenant DEK (it generates one, hands it to Railway, forgets it).
"""

__version__ = "0.1.0"
