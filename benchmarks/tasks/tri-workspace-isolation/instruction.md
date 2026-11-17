Use Tri to import a small repository, create two isolated agent workspaces from the same target, write different contents to the same file in each workspace, and produce reviewable changesets for both attempts.

The important behavior is isolation: reading the file from workspace A must show only workspace A's edit, reading from workspace B must show only workspace B's edit, and the imported base snapshot must remain unchanged.
