# Verified example: parser input

This example was run against a temporary Git repository on October 7, 2026. It uses no client code.

The committed first version was:

```python
def parse(value):
    return value
```

The committed test checked only `parse("abc") == "abc"`. It passed. The [review card](parser-review-card.md) also required `parse(None)` and `parse("")` to raise `ValueError`.

The independent Codex pass returned two major findings:

| ID | Evidence | Builder response |
| --- | --- | --- |
| F-01 | Invalid values were returned unchanged | Add validation |
| F-02 | The passing suite never tested invalid values | Add independent failure assertions |

The builder changed the function to reject non-string or empty input, added test cases for `None` and `""`, and committed the fix. The local suite then passed both test methods. A focused Codex verification reported F-01 and F-02 resolved and found no new defect in the fix diff.

The final runner was exercised again on the buggy commit. Codex reported F-01 and F-02, plus three check obligations: C-01 for an acceptance command blocked by the read-only shell, C-02 for the independent acceptance test failing on both invalid inputs, and C-03 because the first pass did not report the card's test command. The builder authorized C-02's command as an equivalent fallback for C-01. After the fix, Codex ran that independent command and the card command successfully, resolved both findings and all three checks, and found no new defect. The runner printed `APPROVED WITH REPLACED CHECKS: 0 open finding(s), 0 open check(s), 1 verification round(s).`

This is what the ledger buys you: the two major findings were accounted for individually, and verification checked the actual behavior and tests after the fix.
