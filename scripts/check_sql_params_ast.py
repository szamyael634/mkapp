"""
AST-based SQL param validator for cursor.execute statements.
This script attempts to find common mistakes that lead to "Not enough parameters for SQL statement":
- SQL string has N %s placeholders but params in second argument are not a tuple literal (or a tuple-like expression)
- Params are a parenthesized single variable: (some_var) (i.e. missing trailing comma)
- Params are a single Name expression and SQL has more than 1 placeholder

This is intended to supplement the existing literal-eval checker that can miss dynamic param forms.

Usage:
    python scripts/check_sql_params_ast.py

It will print potential problem places with file offsets, line numbers, SQL placeholder count, and the param AST shape.

NOTE: This script cannot fully reason about dynamic values like tuple(order_ids) or func calls that return sequences. It flags potential issues for manual review.
"""
import ast
import os
import sys

SRC_FILE = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'app.py'))

with open(SRC_FILE, 'r', encoding='utf-8') as f:
    source = f.read()


class CursorExecuteVisitor(ast.NodeVisitor):
    def __init__(self):
        self.issues = []

    def visit_Call(self, node):
        # Looking for .execute(...) calls on objects named cursor, cur, rcur, hcur, etc.
        # We'll check for attribute calls: X.execute(SQL, params)
        if isinstance(node.func, ast.Attribute) and node.func.attr == 'execute':
            try:
                # Ensure we have at least 1 argument (SQL).
                if not node.args:
                    return
                sql_node = node.args[0]
                # Retrieve SQL string literal if available
                sql_val = None
                if isinstance(sql_node, (ast.Str, ast.Constant)) and isinstance(sql_node.s if isinstance(sql_node, ast.Str) else sql_node.value, str):
                    sql_val = sql_node.s if isinstance(sql_node, ast.Str) else sql_node.value
                elif isinstance(sql_node, ast.JoinedStr):
                    # f-strings; join values into a placeholder string
                    pieces = []
                    for v in sql_node.values:
                        if isinstance(v, ast.Str):
                            pieces.append(v.s)
                        else:
                            pieces.append('{}')
                    sql_val = ''.join(pieces)

                param_node = None
                if len(node.args) >= 2:
                    param_node = node.args[1]

                placeholder_count = sql_val.count('%s') if sql_val else 'N/A'

                suspicious = False
                reason = None
                if isinstance(placeholder_count, int) and placeholder_count > 1:
                    # If param_node is absent - suspect
                    if param_node is None:
                        suspicious = True
                        reason = 'No params passed'
                    else:
                        # Param node must be a Tuple for literal: (a, b)
                        if isinstance(param_node, ast.Tuple):
                            # OK if tuple length matches placeholder_count
                            tlen = len(param_node.elts)
                            if tlen != placeholder_count:
                                suspicious = True
                                reason = f'Tuple length mismatch: {tlen} vs placeholders={placeholder_count}'
                        elif isinstance(param_node, ast.Call):
                            # e.g., tuple(order_ids) or (order_ids,)
                            # We cannot determine length statically, skip
                            suspicious = False
                        elif isinstance(param_node, ast.Name):
                            # cursor.execute(sql, user_id) -> passing a single var instead of tuple
                            suspicious = True
                            reason = 'Single Name param (not a tuple)'
                        elif isinstance(param_node, ast.Subscript):
                            # e.g., params_list[i], can't determine length -> potential issue
                            suspicious = True
                            reason = 'Subscript param, potential single value'
                        elif isinstance(param_node, ast.BinOp):
                            # e.g., (a,)+b
                            suspicious = False
                        elif isinstance(param_node, ast.Attribute):
                            # passing obj.attr, likely a single value
                            suspicious = True
                            reason = 'Attribute param (likely scalar)'
                        else:
                            suspicious = False

                # Additional heuristic: SQL placeholder count is 1, but param is a tuple of single element
                if isinstance(placeholder_count, int) and placeholder_count == 1 and isinstance(param_node, ast.Tuple) and len(param_node.elts) == 1:
                    # Could be ok, but ensure tuple had trailing comma - AST Tuple has length 1 only if it had trailing comma
                    # This is allowed and fine. Skip.
                    pass

                if suspicious:
                    lineno = node.lineno
                    col = node.col_offset
                    self.issues.append((lineno, col, placeholder_count, ast.dump(param_node) if param_node is not None else None, reason, ast.get_source_segment(source, node)))

            except Exception as e:
                pass

        self.generic_visit(node)


if __name__ == '__main__':
    tree = ast.parse(source, SRC_FILE)
    v = CursorExecuteVisitor()
    v.visit(tree)

    if not v.issues:
        print('No obvious cursor.execute param issues detected by AST heuristic.')
        sys.exit(0)

    print(f'Found {len(v.issues)} potential issues (heuristic):')
    for lineno, col, pc, pn, reason, snippet in v.issues:
        print('\n--- Issue ---')
        print(f'Line {lineno} col {col} -> placeholders=%s' % pc)
        print('Param AST dump:', pn)
        print('Reason:', reason)
        if snippet:
            # Print the portion of source around the call for context
            lines = source.splitlines()
            start = max(0, lineno-4)
            end = min(len(lines), lineno+2)
            print('Context:')
            for i in range(start, end):
                prefix = '>' if i+1 == lineno else ' '
                print(f"{prefix} {i+1:4}: {lines[i]}")

    print("\nNote: This is a heuristic that can't capture every case (e.g., dynamic tuple() calls, function returns). Review flagged locations manually.")
