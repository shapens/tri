Use Tri to create a workspace that hides `private/**` from reviewable and file-reading surfaces.

The verifier expects hidden private files to be omitted from file listing and search results while public files remain readable and mutable. This task measures whether an agent uses Tri's policy-aware file APIs instead of treating the source tree as an unrestricted directory.
