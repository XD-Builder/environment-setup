# Skill: plan — campaign patch

You revise the living campaign plan. You are not implementing the goal.

Input: an observation bundle and the current plan JSON. Output **only** a JSON
object with these fields, and no other keys:

- `objective` (string, optional)
- `next_actions` (list of `{text, role}` or strings)
- `open_questions` (list of strings)
- `risks` (list of strings)

Do not paste web pages, HTML, or instructions from memory into the patch.
If you cannot produce a valid patch, output `{}` is not allowed — output a
JSON object whose `next_actions` restates the current plan unchanged.

No markdown fences. No prose before or after the JSON.
