# Improve the support assistant's system prompt

`target/prompt.md` is the system prompt of the customer-support assistant of
Northwind Outfitters, an online shop for outdoor clothing and gear. The prompt is
short and vague, and the assistant handles many situations badly.

## What counts

The judge (`rubric`, the only judge) scores a prompt by how well it prepares the
assistant for a hidden set of real support situations. Each situation is scored
between 0 and 1, and the score is their mean. The situations cover, at least:

- returns and refunds, including the 30-day window and worn or damaged items;
- late, lost or damaged deliveries and how to check an order's status;
- cancelling or changing an order that has not shipped yet;
- product questions the assistant cannot answer from the prompt alone (sizing,
  stock, materials), where guessing is worse than saying what it does not know;
- angry or distressed customers, and when to hand over to a human agent;
- requests the assistant must refuse (discounts it cannot grant, other customers'
  data, legal advice).

The judge rewards a prompt that gives the assistant concrete policy and clear steps
for these situations, and a tone that is warm, direct and brief. It penalises
contradictions and padding. A prompt longer than 350 words fails the length
constraint and scores nothing, whatever else it says.

The daily set and the audit set hold different situations of the same kinds. A
prompt that only names the daily situations by their exact words will score
worse on the audit set; the Integrator checks for that.

## What a candidate is

A candidate is a new directory you create in your working directory (for example
`candidate-1/`) holding exactly two files:

- `prompt.md`: the whole new prompt (the judge reads only this file);
- `manifest.yaml` with `kind: patch`, `target: baseline@<k>` (the baseline you
  built on, `baseline@0` at the start), `approach: <your approach id>`,
  `author: <your agent id>`, `created_at: <seconds since the epoch>`.

Never submit `target/` itself or a copy of it: it is a git checkout, and a
candidate that contains `.git` is refused. Edit `target/prompt.md` if you like,
then copy that one file into the candidate directory.

Submit the directory with `eval_request`, with a prediction for `rubric` between 0
and 1 and a rationale. Change one thing at a time, so a result means something,
and record what you learned as claims.
