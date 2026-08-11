"""The MCP surface.

Mounted into the same application as the HTTP routes, over the same repository
layer. Two independently built query paths over one database would drift, and
the first symptom would be an agent and a developer getting different answers
to the same question.
"""
