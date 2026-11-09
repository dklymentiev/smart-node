"""Repository layer.

Each module corresponds to one logical entity (actors, projects, objectives,
tasks, ...). Repos accept an ``asyncpg.Pool`` and expose async CRUD methods.
They do not call other repos -- coordination across entities happens in the
core service layer.
"""
