# lex.services — application service layer
#
# Services own operational workflows. They take an open sqlite3.Connection
# as their first argument, raise ValueError on domain errors (not SystemExit),
# and return typed result objects. They do not print.
