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

In each situation a customer writes in, and the assistant answers with your prompt
as its only instructions; it knows nothing else about the company. The reply is
scored against the policy handbook below: every fact it states must be right, it
must take the right next step, and its tone must be warm, direct and brief. The
judge rewards a prompt that gives the assistant the policy it needs and clear steps
for these situations. It penalises contradictions and padding. A prompt longer than
350 words fails the length constraint and scores nothing, whatever else it says.
The handbook is longer than that, so a prompt has to choose and compress.

Scores carry noise: the same prompt can score a few hundredths apart on two runs.

The daily set and the audit set hold different situations of the same kinds. A
prompt that only names the daily situations by their exact words will score
worse on the audit set; the Integrator checks for that.

## Policy handbook

Returns: unworn, unwashed items with tags can be returned within 30 days of delivery for a full refund to the original payment method. Refunds are issued 5 to 7 business days after the warehouse receives and inspects the return. Return shipping is free for defective, damaged or wrong items; otherwise a $6 return label is deducted from the refund. Worn, washed or tag-less items, and anything past 30 days, cannot be returned, but a manufacturing defect is covered by a 1-year warranty: the customer sends photos to support and gets a repair, replacement or refund. Final-sale items (marked in the order) cannot be returned unless defective.

Shipping: standard shipping takes 3 to 5 business days and is free over $75; express takes 1 to 2 business days for $15. Every order gets a tracking link by email when it ships. If tracking shows no movement for 5 business days, or the parcel is marked delivered but is missing after 48 hours, support opens a carrier claim and ships a free replacement once the claim is filed.

Order changes: orders can be cancelled or changed (size, color, address) until they ship, usually within 12 hours of ordering, by replying to the order confirmation email or through the account page. After shipping, the customer must wait and return it.

Products: the assistant cannot see live stock or each product's exact measurements and materials; it must point to the product page's size chart and details, or offer a specialist. Sizing runs true to size except Alpine shells, which run small: suggest one size up.

Money: the assistant cannot give discounts, coupons, price matches or refunds outside policy. The only promotions are those on the website. Price adjustments: if an item's price drops within 14 days of purchase, support refunds the difference once.

Privacy and safety: never share or change anything about another person's order or account; ask the account holder to contact us. Never ask for passwords or full card numbers. No legal or medical advice.

People: acknowledge feelings briefly and sincerely, then fix the problem. Hand over to a human agent when the customer asks for one, mentions a chargeback, a legal threat, an injury or a safety issue with a product, or when the request needs an exception to policy.

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
