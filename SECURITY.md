# Security Policy

demo_cli is a local-first safety tool for AI coding agents. We take its own
security seriously and welcome responsible disclosure.

## Reporting a vulnerability

Please email **contact@wepwn.ma** with:

- a description of the issue and its impact,
- the version or commit affected (`demo_cli --version`),
- steps to reproduce, and a proof of concept if you have one.

Please do **not** open a public GitHub issue for security-sensitive reports.

We aim to acknowledge reports within a few business days and to keep you updated
as we investigate and fix. We're happy to credit you in the release notes once a
fix ships, unless you prefer to stay anonymous.

## Scope

In scope: the demo_cli codebase and its installers (`install.sh`, `install.ps1`).

Out of scope for this beta (documented limitations, not vulnerabilities):

- Adversarial evasion of command classification, and protection against a
  malicious same-user process. demo_cli's threat model is cooperative agents
  making mistakes, not an attacker actively trying to defeat it.
- Coarse directory restore, single undo depth, and the other limits described in
  the README's "What it covers, and what it does not" section.

## Supported versions

This is pre-release software. Only the latest published `v0.4.0-beta.*` tag is
supported.
