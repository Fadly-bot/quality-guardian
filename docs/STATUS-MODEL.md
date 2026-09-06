# Status Model

Quality Guardian uses exactly five scanner/check statuses.

## PASS

The relevant target was actually covered and the check produced acceptable results.

## FAIL

The relevant target was covered and a confirmed issue was found.

## ERROR

The check was attempted but could not complete correctly.

## NOT_APPLICABLE

The check does not apply to the detected project.

## NOT_SCANNED

The intended target was not sufficiently covered.

Important:

0 findings does not automatically mean PASS.

If coverage is insufficient, the result must be NOT_SCANNED.

## Release Decisions

Quality Guardian uses:

- PASS
- BLOCK
- NEEDS_REVIEW

Release decisions are based on evidence and finding severity, not scanner exit codes alone.
