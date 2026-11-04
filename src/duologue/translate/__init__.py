"""Translation between Claude Code transcript records and Codex rollout records.

Both directions are pure functions of their input: the same records always translate to the same
output, ids included, so a retry after a crash can recognise what it already wrote. The only side
effect is :class:`~duologue.translate.outputs.OutputStore` writing the full text of long tool
outputs to files.
"""
