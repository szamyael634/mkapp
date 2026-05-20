import re
import ast

FNAME = 'c:\\Users\\Jeon\\Documents\\Ptry\\Petopia\\app.py'

pattern = re.compile(r"cursor\.execute\s*\(\s*(?P<sql>r?['\"]{3}.*?['\"]{3}|r?['\"].*?['\"])\s*,\s*(?P<params>\(.+?\)|[a-zA-Z0-9_\.\[\]]+)\s*\)", re.DOTALL)

with open(FNAME, 'r', encoding='utf-8') as f:
    s = f.read()

matches = list(pattern.finditer(s))

issues = []
for m in matches:
    sql_raw = m.group('sql')
    params_raw = m.group('params')
    # clean SQL string
    try:
        sql_val = ast.literal_eval(sql_raw)
    except Exception:
        # fallback to raw content
        sql_val = sql_raw
    # count %s placeholders
    placeholder_count = sql_val.count('%s') if isinstance(sql_val, str) else 'N/A'

    # attempt to parse tuple param literal
    params_len = None
    if params_raw.startswith('(') and params_raw.endswith(')'):
        try:
            parsed = ast.literal_eval(params_raw)
            if isinstance(parsed, tuple):
                params_len = len(parsed)
        except Exception:
            params_len = None
    # If params appear as tuple(...) form, skip
    if params_len is None and params_raw.strip().startswith('tuple('):
        params_len = 'tuple(...)'  # unknown

    if isinstance(placeholder_count, int) and isinstance(params_len, int):
        if placeholder_count != params_len:
            issues.append((m.start(), placeholder_count, params_len, sql_val, params_raw))

print(f'Found {len(matches)} cursor.execute() calls; {len(issues)} mismatches (literal tuple param detection)')
for off, pc, pl, sql, params_raw in issues:
    print('\n--- MISMATCH DETECTED ---')
    print('Offset:', off)
    print('Placeholders:', pc)
    print('Params tuple length:', pl)
    print('Params raw:', params_raw)
    print('SQL:', sql)

# Also check f-string or triple-quoted uses with format_strings/format_order_ids
kinds = ['format_order_ids', 'format_strings', 'format_sellers', 'fmt', 'fmt_req']
for k in kinds:
    if k in s:
        print('\nFound dynamic placeholder var:', k)



