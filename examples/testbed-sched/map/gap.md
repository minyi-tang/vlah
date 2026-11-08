# Opportunity map: unmeasured

These are the public site observations included in the worker pack. They are not
measured gaps, a ranking of opportunities or a list of known mechanisms.

| Site | Public observation |
| --- | --- |
| 1 | Quick jobs wait behind other people's work |
| 2 | Complaints arrive in time-of-day waves; jobs and their neighbors take longer |
| 3 | The same job's running time varies with where it lands |
| 4 | Many users run tiny jobs; the first job after a pause is slow to get going |
| 5 | No special symptom reported; jobs occupy whole machines and have duration estimates |
| 6 | No special symptom reported; jobs occupy whole machines and have duration estimates |

Run `tb fast target/policy --seed 1 --out fast.json`, then
`python problem/map/gap.py fast.json --out gap.md` in the worker sandbox.
Use any allowed public seed from 1 through 5; compare against its matching
baseline. The generated report splits waits by site, job size, estimated duration
and user.

Preserve measurements and their candidate/baseline identities in the append-only
record. No site has a claimed gain yet. Local simulation is only a smoke check;
formal conclusions require the official slow result and passing constraints.
