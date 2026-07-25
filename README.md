# Miru Public Browser Runner

A public, secret-free Playwright runner for observing public web pages and verifying bounded public video playback on free GitHub-hosted Actions.

## Verified state

- GitHub public runner allocation: `VERIFIED`
- Browser startup: `VERIFIED`
- Generic public HTTPS page observation: `VERIFIED`
- Title, visible-text, screenshot and Playwright trace evidence: `VERIFIED`
- Public HTML5 video loading and playback-time advance: `VERIFIED`
- YouTube on GitHub-hosted cloud IPs: `BLOCKED_BY_SITE_CHALLENGE`

The YouTube result is not a browser startup failure. Direct embeds reached YouTube, but YouTube requested an authenticated human check. The runner does not bypass that challenge.

## Public request lane

A request PR may add exactly one `requests/PUBLIC-REQUEST-*.json` file. The workflow executes trusted code from `main`, permits only bounded public HTTPS reading, blocks private-network targets and non-GET/HEAD requests, and returns sanitized evidence. One-shot request PRs are closed without merging after their artifacts are read.

## Public-only boundary

This repository must not contain credentials, cookies, login sessions, private prompts, personal data, unpublished content, or secret-backed workflows. Public requests do not click, submit forms, download, upload or change external state.

Evidence is uploaded as short-lived GitHub Actions artifacts.
