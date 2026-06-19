"""Journal layer — append-only decision log (the training loop).

Every proposal + the human's approve/reject + (later) the outcome is appended as
one JSONL record, mirroring the live trade journal so the same process/outcome
grading can be applied to the agent's own proposals.
"""
