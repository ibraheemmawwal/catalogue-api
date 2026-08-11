"""Query layer.

Every surface goes through here — HTTP routes and MCP tools alike. Two
independently written query paths over one database would drift, and the first
symptom would be an agent and a developer getting different answers to the same
question.
"""
