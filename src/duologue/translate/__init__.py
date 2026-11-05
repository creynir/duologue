"""Translation between Claude Code transcript records and Codex rollout records.

Both directions derive every id they write from the pair and the source records, so translating
the same records again gives the same ids and a retry after a crash can recognise what it already
wrote. Timestamps come from the source records too. A record without one takes its neighbour's,
and only when no record in a batch has one does the translator fall back to ``now_ms`` (or the
clock, when the caller passes none). The only side effect is that
:class:`~duologue.translate.outputs.OutputStore` writes the full text of long tool outputs to
files.
"""
