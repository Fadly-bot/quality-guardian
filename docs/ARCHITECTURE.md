# Quality Guardian Architecture

## Role

Quality Guardian is an independent audit and release-gate agent.

It does not act as the primary coding agent.

## Pipeline

Project
  ↓
Project Detection
  ↓
Quality Checks
  ↓
Security Scanners
  ├── Semgrep
  ├── Gitleaks
  ├── Trivy
  └── OSV-Scanner
  ↓
Evidence Validation
  ↓
Finding Classification
  ↓
Severity Assessment
  ↓
Release Decision

## Core Principle

COMMAND EXECUTED ≠ TARGET COVERED ≠ SCAN PASSED

Executing a scanner command does not automatically mean that the intended target was scanned.

Coverage must be verified independently from the command execution result.

## Evidence Flow

AI → analysis
CLI → evidence
AI → interpretation
CLI → verification

## Read-Only Boundary

During audit mode the Guardian must not:

- modify files
- create files
- delete files
- install dependencies
- modify dependencies
- commit changes
- push changes
- deploy applications

## Release Decisions

PASS
  All relevant required checks have sufficient evidence.

BLOCK
  A confirmed blocking finding exists.

NEEDS_REVIEW
  Evidence is insufficient for a definitive PASS or BLOCK decision.
