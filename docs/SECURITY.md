# Security Model

Quality Guardian separates objective scanner evidence from AI reasoning.

## Security Scanners

### Semgrep

Static analysis for source-code security and correctness patterns.

### Gitleaks

Secret detection in the working tree and Git history.

A zero-commit repository cannot provide Git-history coverage.

Therefore:

0 commits scanned → NOT_SCANNED

### Trivy

Trivy coverage is interpreted separately for:

- vulnerabilities
- secrets
- misconfigurations

A `-` result means the corresponding target was not scanned.

It must not be interpreted as zero findings or PASS.

### OSV-Scanner

Dependency vulnerability scanning based on supported package manifests and lockfiles.

### Package Auditing

When applicable, ecosystem-specific auditing such as `npm audit` may provide additional dependency evidence.

## Evidence Requirements

A security finding must identify:

- scanner/tool
- exact command
- exit code
- intended target
- actual target
- coverage assessment
- file/path
- line when applicable
- evidence
- explanation
- remediation

The Guardian must not invent:

- CVEs
- vulnerabilities
- package facts
- scanner results
- coverage
- project characteristics

## Severity

Severity must be contextual.

A missing test suite is not automatically a security vulnerability.

An unusual dependency version is not automatically a supply-chain attack.

A missing `.gitignore` is not automatically a security vulnerability.

Security severity requires objective evidence.
