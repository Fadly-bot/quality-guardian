# Quality Guardian Project Profiles
## Purpose

Project Profiles define which checks are relevant to a detected project.

A profile does not replace scanner evidence.

The profile determines applicability; scanner output determines the result.

## Detection Principle

Project detection must be evidence-based.

The Guardian may only select a profile when the required detection evidence exists.

If no specific profile can be confidently selected:

`generic`

must be used.

The Guardian must not infer a framework or ecosystem from weak evidence.

## Profile Schema

Each profile uses the following structure:

```json
{
  "name": "Example",
  "version": "1.0",
  "detect": [],
  "package_managers": [],
  "required_scanners": [],
  "optional_scanners": [],
  "quality_commands": []
}
```
## Fields

### name

Human-readable profile name.

### version

Profile schema/version identifier.

### detect

Evidence required to identify the profile.

Detection entries must identify concrete files or project markers.

Examples:

- `package.json`
- `next.config.js`
- `pyproject.toml`
- `composer.json`

### package_managers

Package managers relevant to the profile.

Examples:

- npm
- pnpm
- yarn
- pip
- composer

### required_scanners

Security scanners expected for the profile.

The initial scanner set is:

- semgrep
- gitleaks
- trivy
- osv

A scanner being required does not mean its result is automatically PASS.

Coverage must still be validated.

### optional_scanners

Additional ecosystem-specific scanners.

Examples:

- npm audit
- pip-audit
- cargo audit
- govulncheck

An optional scanner should only run when its ecosystem and required tooling are available.

### quality_commands

Commands that may be used to assess project quality.

Commands must only be executed when they are actually defined by the project.

Examples:

- `npm test`
- `npm run lint`
- `npm run build`
- `npm run typecheck`

The existence of a profile command does not prove that the project supports it.
## Applicability

A check has three separate concepts:

1. Applicable
2. Executed
3. Passed

These must never be conflated.

Example:

A Node.js profile may define `npm test`.

If `package.json` has no test script:

`npm test` must not be treated as a successful test.

The result should reflect the actual project evidence.

## Detection Priority

More specific profiles take precedence over generic profiles.

Example:

Next.js
  ↓
Node.js
  ↓
Generic

If Next.js evidence exists, select Next.js.

If only Node.js evidence exists, select Node.js.

If neither exists, select generic.

## Current Profiles

Planned profiles:

- generic
- node
- nextjs
- python
- php

## Security Boundary

Profiles must never:

- invent vulnerabilities
- assign security severity
- override scanner results
- convert NOT_SCANNED to PASS
- suppress scanner findings
- modify project files
- install dependencies

Profiles control applicability and command selection only.

## Versioning

Profile schema changes must be versioned independently from the Guardian application version.

A profile change must be tested before being considered release-ready.

## Detection Rules

Profile detection must inspect the project root and relevant project markers.

Detection must be deterministic for the same project state.

Profiles are evaluated from most specific to least specific.

The generic profile is the fallback when no specific profile matches.

### Detection Requirements

A detection rule must use concrete filesystem evidence.

Examples:

- Next.js: `next.config.js`, `next.config.mjs`, `next.config.ts`, or a `next` dependency in `package.json`.
- Node.js: `package.json`.
- Python: `pyproject.toml`, `requirements.txt`, or `setup.py`.
- PHP: `composer.json`.

### Detection Precedence

The current precedence is:

1. Next.js
2. Node.js
3. Python
4. PHP
5. Generic

A higher-priority profile must only be selected when its detection evidence is satisfied.

### Detection Failure

If profile detection encounters an unexpected filesystem or parsing error, the Guardian must report the error.

An unexpected detection error must not silently become a specific profile.
