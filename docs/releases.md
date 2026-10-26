# Releasing the Python packages

The automation is installed **disabled**. It does not reserve package names,
configure accounts, create tags, or authorize the first release. Only an operator
may enable publishing after completing the setup below.

The registry is **PyPI**. A **GitHub Release** also carries the same wheel and source
archive, plus `release.json` with their SHA-256 digests and source commit. GitHub
Packages is not a Python package registry and is not used here.

## Independent packages and tags

| Tag | Published project | Initial version |
| --- | --- | --- |
| `testbed-v<version>` | `vlah-testbed` only | `0.1.0` |
| `v<version>` | `vlah` only | `0.0.1` |

Each tag must exactly match that project's `pyproject.toml`. Both packages are
built and validated, but a tag never republishes the other package. Publish the
tested, compatible testbed version **before** VLAH. A VLAH release checks that both
testbed distribution hashes already match PyPI; local workspace resolution must
not conceal an unavailable or changed dependency. Currently VLAH requires
`vlah-testbed>=0.1.0,<0.2.0`.

Versions use three numeric components without leading zeroes, optionally followed
by `aN`, `bN`, or `rcN`, then `.postN` and/or `.devN`. Examples are `0.1.0`,
`0.2.0rc1`, and `0.2.0.dev1`. Epochs, local versions, and alternate spellings such
as `0.2.0-rc1` are rejected. Alpha, beta, release-candidate, and development tags
produce GitHub prereleases; post releases of final versions are ordinary releases.
Neither package automatically takes GitHub's repository-wide **Latest** label:
two independent version sequences have no meaningful shared latest version.

## One-time operator setup

1. Merge the reviewed packaging and release-automation PRs in dependency order.
   Protect the repository's default branch (currently `main`), require review and
   the CI/Lint checks, including `distributions`, and prohibit force pushes and
   deletion. The release gate reads the live default branch, requires GitHub to
   report it as protected, and proves that the tagged commit is in its history.
   The operator must ensure the protection actually requires the intended checks;
   the `protected` flag alone does not describe their strength.
2. Confirm ownership or the ability to claim **both** PyPI project names. On
   2026-10-25 their public JSON endpoints returned 404; that was neither a
   reservation nor proof of availability. A pending publisher also does not
   reserve a name. Resolve a name conflict before changing package metadata or
   attempting a release.
3. In `minyi-tang/vlah` **Settings > Environments**, create **`pypi`**.
   Configure required reviewers, enable **Prevent self-review**, and disable
   administrator bypass. Arrange a second trusted reviewer so the tag pusher can
   obtain approval. Select **Selected branches and tags** and add exactly two
   **Tag** rules: **`v*`** and **`testbed-v*`**, with no branch rules. The read-only
   gate checks that this environment exists, has a nonempty reviewer rule with
   self-review prevention, and has exactly these tag restrictions. The operator
   must also verify administrator-bypass settings. Naming an environment in YAML
   does not protect it: GitHub otherwise creates missing environments unprotected.
4. Configure a tag ruleset for those two patterns. Restrict tag creation to release
   operators and prohibit updates/deletion; do not move a release tag during
   recovery. Environment restrictions control deployment, not who can push tags.
5. In PyPI, configure one GitHub Trusted Publisher for each project. For existing
   projects, use **Manage > Publishing** with an account that owns the project.
   For uncreated projects, use the account's **Publishing** page to add a pending
   publisher. Enable the PyPI account's required two-factor authentication.

| PyPI field | Testbed publisher | VLAH publisher |
| --- | --- | --- |
| Project name | `vlah-testbed` | `vlah` |
| Owner | `minyi-tang` | `minyi-tang` |
| Repository name | `vlah` | `vlah` |
| Workflow filename | `release.yml` | `release.yml` |
| Environment name | `pypi` | `pypi` |

The workflow filename is **`release.yml`**, not its full `.github/workflows/` path.
Do not create a long-lived `PYPI_API_TOKEN` or `UV_PUBLISH_TOKEN`. The PyPI job
requires OIDC trusted publishing; it cannot fall back to a stored registry token.

After checking all of the above, set the **repository Actions variable**
`PYPI_PUBLISH_ENABLED` to exactly **`true`** under **Settings > Secrets and variables
> Actions > Variables**. Do not use an environment variable of that name: job
conditions must see the repository variable before admitting an environment job.
Leave it absent or set it to `false` until setup is complete. Removing it or
setting `false` disables future publishing jobs.

As of the setup check, there were no GitHub environments and `main` was
unprotected. None of those account/repository settings is changed by this PR.

## Build-only checks

Run from the source checkout with the project's development dependencies:

```bash
uv build --all-packages --no-sources
uv run python -m scripts.validate_distributions --dist dist --manifest dist-validation.json
uv run pytest tests/test_package.py tests/testbed/test_packaging.py tests/unit/release
uv publish --dry-run --trusted-publishing never --no-config dist/*.whl dist/*.tar.gz
```

The validator rejects missing, extra, misversioned, or changed artifacts. Use a
fresh output directory, or remove only known obsolete distribution files before a
new version build. It reuses the packaging regression assertions and installed
probes, rebuilds each source archive outside the checkout, and exercises original
and rebuilt installations, resources, read-only testbed packages, VLAH's demo,
and public testbed operations. Rebuilt wheels are verification inputs only;
they never replace the original build output.

The uploader dry run parses the actual distributions but sends no uploads and
requests no OIDC token. Builds, dependency installation, and index checks may use
the network; **this is not an offline test**. In the pinned uv `0.9.4`,
`publish --offline` is rejected even with `--dry-run`.

PRs run the read-only `distributions` CI job. After the workflow reaches the
default branch, **Actions > Release > Run workflow** or the command below runs
the full manual build-only path:

```bash
gh workflow run release.yml --repo minyi-tang/vlah --ref main
```

A `workflow_dispatch` run cannot publish, even when dispatched on a valid tag
and the enablement variable is true. It validates both packages and retains an
immutable Actions artifact. This does not test live OIDC, PyPI upload, or GitHub
release creation.

## Release an approved version

Open a version-update issue and PR first. Update only the intended project
version and any compatibility bounds that actually change, refresh `uv.lock`,
and update version-specific regression expectations. Run the documented checks
and merge the approved PR. Do not bump versions merely to install this workflow.

Choose a full commit SHA from the protected default branch after its required
checks pass. Verify the tag does not already exist locally or remotely. The
following commands are **operator examples**, not actions performed by setup:

```bash
git tag -a testbed-v0.1.0 <approved-full-commit-sha> -m "Release vlah-testbed 0.1.0"
git push origin refs/tags/testbed-v0.1.0
```

Review and approve the `pypi` environment jobs. After testbed publication has
completed, release VLAH separately:

```bash
git tag -a v0.0.1 <approved-full-commit-sha> -m "Release vlah 0.0.1"
git push origin refs/tags/v0.0.1
```

Push one intended tag at a time, not `git push --tags`. Both lightweight and
ordinary annotated tags work. Tags pointing to another tag rather than a commit
are rejected. A moved tag, malformed version, mismatched checkout, unprotected
branch, non-ancestor commit, invalid artifact, missing dependency release, or
failed prerequisite stops publication.

The read-only job runs tests and types, builds once, validates those bytes, and
stages the selected files with a hashed manifest. The PyPI and GitHub jobs
download the same immutable artifact **by ID**, verify the manifest against the
build job's digest, recheck the tag/protection, and never build or install the
workspace. Publisher jobs install only pinned release-tool dependencies.
Only the PyPI job has `id-token: write`; only the GitHub job has `contents: write`.
Both use `pypi`, so approval may be required separately for each job.

After publication, install outside the checkout:

```bash
python -m pip install 'vlah-testbed==0.1.0'
python -m pip install 'vlah==0.0.1'
tb --help
vlah --version
vlah demo --seed 1
```

For a prerelease, pin its exact version (or deliberately opt into pip's `--pre`).

## Recover without replacing published bytes

Runs for the same ref are serialized and not cancelled mid-upload. Prefer
**Re-run failed jobs** in the original Actions run: its successful build output
and artifact ID are reused. Artifacts are retained for 90 days, subject to
repository retention limits. Preserve the original artifact before it expires.

PyPI recovery checks every existing filename and SHA-256 before uploading.
Extra, different, duplicate, or yanked files fail closed. uv's explicit
`--check-url` also compares hashes and skips only identical files, including
raced uploads; there is no blind `skip-existing` switch. A successful upload is
read back before GitHub publication proceeds.

GitHub recovery verifies the source commit, prerelease status, manifest identity,
and downloads each existing asset to compare actual bytes. A draft can receive
only its missing assets, without `--clobber`; it is published only after the
complete set is verified. A complete published release is an unchanged no-op.
An incomplete published release or an unfinished upload placeholder needs
operator investigation, not automatic deletion or replacement.

If a full rerun rebuilds different bytes for an already uploaded version, it
fails rather than disguising them as the old release. Recover the original
validated bundle and original failed job when possible. Otherwise open a new
version PR; do not delete and reuse PyPI filenames or move a published tag.
Resolve name ownership, trust, permissions, or protection errors before retrying.
No live registry publication has been performed as part of this setup.

## References

- [uv package publishing and identical-file retries](https://docs.astral.sh/uv/guides/package/)
- [uv's separate OIDC publishing job](https://docs.astral.sh/uv/guides/integration/github/#publishing-to-pypi)
- [Existing PyPI trusted publishers](https://docs.pypi.org/trusted-publishers/adding-a-publisher/)
- [Pending publishers do not reserve names](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)
- [GitHub environment protection](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments)
