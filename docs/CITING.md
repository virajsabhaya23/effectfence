# Citing EffectFence

If EffectFence, its benchmark corpus, or a generated counterexample contributes
to a paper, engineering report, standard, or product evaluation, cite the exact
released version and preserve the report's `certificateSha256`.

Copy-ready metadata is available in three formats:

```bash
effectfence citation --format bibtex
effectfence citation --format cff
effectfence citation --format json
```

GitHub also reads the repository's [`CITATION.cff`](../CITATION.cff) and exposes
a **Cite this repository** control.

No DOI is listed until an archival release has actually been deposited. After a
public GitHub release is connected to Zenodo, replace the repository-only
citation with the DOI returned by that archive; never reuse a draft or invented
identifier.

For reproducibility, a citation should include:

- EffectFence version and release commit;
- the conformance manifest without secrets;
- the JSON report and certificate hash;
- MCP server name/version and relevant dependency versions;
- the observer boundary and known limitations.
