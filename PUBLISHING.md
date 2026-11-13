# Publication notes

This project is a standalone demonstration with fictional inputs. It contains no imported repository history. Its input contract and generated metadata are vendor-neutral rather than a replacement for any particular reporting service.

## Prepare the files

Run from this standalone project directory:

```sh
python3 -B -m unittest discover -s tests -v
python3 -B -m report_workbench audit --root .
```

Review the source, examples and any later changes. Check that you have not added real customer records, database dumps, account identifiers, internal schemas, credentials, private paths or confidential mappings. Pattern checks cannot recognize every type of private information.

The supplied export manifest lists the distribution's files and hashes. Generated bundles under `.artifacts` are intentionally excluded from the distribution. Real input files belong outside the published tree or under an ignored private input directory; the tool never copies them into bundles automatically. Generated SQL/metadata can still contain information from your inputs, so review outputs before sharing them separately.

## Create fresh history

Create a new, empty repository from this folder instead of changing the remote on an unrelated repository. This distribution includes no `.git` directory, saved remote URL, author name or email.

Review your chosen Git author identity before the first commit. To initialize locally without inheriting Git templates:

```sh
git init --template= -b main
git add .gitignore README.md PUBLISHING.md requirements.txt report_workbench examples tests verification.json EXPORT_MANIFEST.json
git status --short
```

Inspect the staged file list before making a commit or adding a remote. Nothing in these instructions creates an online repository, sends messages or uploads files.

## License and scope

No license has been assigned on anyone's behalf. Choose one you are authorized to apply before presenting the code as licensed open source. Technical sanitization does not establish publication rights; use the publication permission applicable to your situation.

This is a review-draft generator. It does not automatically infer semantic mappings, prove every report's correctness, validate deployed schemas or establish production readiness. SQL Server execution and business-result comparisons are separate tasks.
