"""Pure, dependency-free helpers shared across layers.

ADR-0016 keeps this layer deliberately thin: modules here hold pure functions
with no side effects and no imports from any business layer. Anything that
needs configuration, I/O, or a client belongs in `services/` or `clients/`.
"""
