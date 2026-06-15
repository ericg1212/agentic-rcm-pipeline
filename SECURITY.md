# Security Policy

## Scope

This project processes synthetic claim events generated from public CMS aggregate distributions.
**No real PHI, PII, or beneficiary-level data is present in this repository.**

HIPAA readiness boundary: the pipeline is architected for HIPAA compliance (encryption at rest,
least-privilege IAM, no PHI storage) but is not currently operating on covered data.

## Reporting a Vulnerability

If you discover a security vulnerability, please **do not open a public issue**.

Report privately via GitHub Security Advisories:
- Go to the Security tab → Report a vulnerability

Include: description, reproduction steps, and potential impact. You will receive a response within 5 business days.

## Security Controls (v1)

- No PHI in any data file, log, or Snowflake table
- Secrets via environment variables only (`.env`, never committed)
- S3: Block Public Access enabled on all buckets, AES-256 at rest
- Snowflake: role-based access, ACCOUNTADMIN only for DDL
- GitHub: secret scanning + push protection enabled
- Dependencies: weekly Dependabot scans + pip-audit in CI
