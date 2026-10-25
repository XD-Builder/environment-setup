# Skill: graph mine — propose edges after retro extraction

You already mined learnings from the transcripts (or are about to). Now add
**knowledge-graph edges** between existing nodes. Do not invent nodes.

Iron law: only relate things the transcripts actually connect. Skip weak or
speculative links. Every edge needs a one-sentence `note`.

## Edges to consider
- `leads_to` — a decision produced a learning
- `contradicts` — two learnings cannot both be true
- `related_to` — same topic, not a contradiction or causal link

Use `recall_memory` to see current nodes, then `graph_add_edge` with
from_type/from_key, to_type/to_key, edge_type, and note.

If nothing is worth linking, say so and stop. Do not continue the user's work.
