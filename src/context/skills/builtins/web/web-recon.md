---
name: web-recon
description: Web application reconnaissance — enumerate endpoints, tech stack, and attack surface
tags:
  - web
  - recon
tools:
  - shell_tool
  - fetch_tool
  - read_tool
  - write_tool
---

You are performing web application reconnaissance. Your objective is to fully map the target's attack surface before any exploitation attempt.

## Methodology

### 1. Passive Recon
- Check `robots.txt`, `sitemap.xml`, `.well-known/` endpoints
- Enumerate subdomains using tools like `subfinder`, `amass`, or `dnsx`
- Check certificate transparency logs for subdomains
- Search for exposed source, backup files: `.git/`, `.env`, `backup.zip`, `config.bak`

### 2. Tech Stack Fingerprinting
- Identify server, framework, CMS via response headers, HTML meta tags, error pages
- Use `whatweb` or `wappalyzer` to fingerprint technologies
- Note version numbers — cross-reference against CVE databases

### 3. Directory & Endpoint Enumeration
- Run `gobuster dir` or `ffuf` with appropriate wordlists (SecLists recommended)
- Enumerate API endpoints: `/api/v1/`, `/api/v2/`, `/graphql`, `/swagger`, `/openapi.json`
- Look for admin panels: `/admin`, `/dashboard`, `/wp-admin`, `/phpmyadmin`
- Check for exposed documentation or internal tools

### 4. Authentication Surface
- Identify login endpoints, registration, password reset flows
- Test for default credentials on discovered admin panels
- Note any OAuth, SSO, or API key authentication mechanisms

### 5. JS Analysis
- Extract JavaScript files and parse for hidden endpoints, API keys, hardcoded credentials
- Use `LinkFinder` or `gau` to extract URLs from JS files

## Tool Priority
- `gobuster` / `ffuf` for directory busting
- `whatweb` for fingerprinting
- `curl` / `httpx` for manual probing
- `gau` / `waybackurls` for historical URL discovery

## Output Format
Produce a structured attack surface summary:
- Target URL and IP
- Tech stack identified
- Interesting endpoints found
- Potential vulnerability areas to investigate next
- Recommended follow-up skills: `sqli`, `xss`, or specific exploit skills
