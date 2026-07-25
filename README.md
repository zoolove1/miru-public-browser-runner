# Miru Public Browser Runner

A public, secret-free Playwright runner for observing public web pages and verifying bounded public video playback on free GitHub-hosted Actions.

## Verified state

- Browser startup: `VERIFIED`
- Public HTML5 video loading and playback-time advance: `VERIFIED`
- YouTube on GitHub-hosted cloud IPs: `BLOCKED_BY_SITE_CHALLENGE`

The YouTube result is not a browser startup failure. Direct embeds reached YouTube, but YouTube requested an authenticated human check. The runner does not bypass that challenge.

## Public-only boundary

This repository must not contain credentials, cookies, login sessions, private prompts, personal data, unpublished content, or secret-backed workflows.

Evidence is uploaded as short-lived GitHub Actions artifacts.
