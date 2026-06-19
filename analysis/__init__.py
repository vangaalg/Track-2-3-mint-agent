"""Analysis layer — turns a market snapshot into a vetted trade proposal.

Machine A (the read) + Machine B (level/strike selection + the discipline gate),
expressed per the journal's Three-Bucket System. Phase 1 implements Trade 1
(directional). See ``analysis.proposal`` for the output contract.
"""
