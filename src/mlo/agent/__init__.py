"""The agent layer: bounded LLM tasks over a deterministic fallback chain.

Design contract (docs/agent-design.md): the model cannot damage data (the
kernel guarantees that), so this package's whole job is making its JUDGMENT
reliable — schema-validated outputs, bounded repair, abstention, escalation,
and measured evals. Everything degrades to deterministic behavior when
[llm] enabled = false.
"""
