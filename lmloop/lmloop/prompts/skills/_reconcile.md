# Skill: reconcile — resolve contradicting learnings

You will be given `contradicts` pairs from the project knowledge graph. Review
them. Do not continue unrelated work.

For each pair:
1. Call `recall_memory` if you need more context.
2. Decide: keep both (they are not really in conflict), supersede one learning
   by `remember` with the same key and a corrected insight, or log a decision
   that settles the conflict (`log_decision`).
3. If you keep a relationship, you may `graph_add_edge` with `related_to` or
   leave the `contradicts` edge (it is an audit trail).

Report what you resolved and what you left. Stop.
