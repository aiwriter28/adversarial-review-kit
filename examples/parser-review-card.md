# Review input card: example parser

## Intended behavior

`parse(value)` returns a nonempty string unchanged. Empty strings and `None` raise `ValueError`.

## Acceptance evidence

| Scenario | Input or action | Expected result | Check to run |
| --- | --- | --- | --- |
| Main path | `parse("abc")` | Returns `"abc"` | `python3 -m unittest -v` |
| Failure path | `parse(None)` | Raises `ValueError` | `python3 -m unittest -v` |
| Empty string | `parse("")` | Raises `ValueError` | `python3 -m unittest -v` |

## Constraints and boundaries

Standard library only. No file or network access.

## Test weakness to challenge

A test that computes its expected value by calling `parse` again will pass when `parse(None)` incorrectly returns `None`.

## Test commands

`python3 -m unittest -v`
