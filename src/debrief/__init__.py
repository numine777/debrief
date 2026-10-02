"""Debrief: review what coding agents built through the records they keep.

Debrief has two halves. Agents follow a short protocol and write records
(briefs, systems, tests, decisions, journals) into an archive outside the
repository. Debrief then checks those records against git and serves a
viewer that links every explanation to the code it explains.
"""

__version__ = "0.1.0"

#: Version of the record format agents write. Bumped only on breaking changes.
PROTOCOL = "ai-sessions/0.1"
