"""The Claude Code adapter: hook entry points + settings wiring.

These modules are deliberately dual-homed: they run as package modules
(`engram claude hook <event>` — the pip install path) and as standalone
scripts (the marketplace plugin's hooks/ directory ships byte-identical
copies). A parity test keeps the two homes in sync.
"""
