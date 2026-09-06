# Quality Guardian

Independent AI-assisted quality, security, and release-gate agent for software projects.

## Purpose

Quality Guardian audits a project using objective CLI evidence, validates scanner coverage, classifies findings, and produces a release decision.

## Core Principle

AI analysis is not evidence.

The Guardian follows:

AI → analysis
CLI → evidence
AI → fix recommendation
CLI → verification

## Scanner Stack

- Semgrep CE
- Gitleaks
- Trivy
- OSV-Scanner
- npm audit when applicable

## Status Model

- PASS
- FAIL
- ERROR
- NOT_APPLICABLE
- NOT_SCANNED

## Release Decisions

- PASS
- BLOCK
- NEEDS_REVIEW

## Safety Model

Quality Guardian is read-only during auditing.

It does not:

- modify project files
- install dependencies
- change dependencies
- commit changes
- push changes
- deploy applications

## Agent

The OpenCode agent definition is located at:

`agent/quality-guardian.md`
