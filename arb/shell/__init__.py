"""The imperative shell.

Owns venue clients, persistence, and scheduling. Translates the world into
`Event`s and executes `Action`s. It contains no decisions - anything that
chooses is in the core, behind the reducer.
"""

__all__: list[str] = []
