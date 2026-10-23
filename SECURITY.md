# Security

This is a private engineering tool.

Do not commit credentials, tokens, provider settings, telemetry, run outputs or product secrets.

If a secret is committed:

1. revoke/rotate the secret immediately;
2. remove it from Git history;
3. audit affected accounts;
4. document the incident privately.

See `docs/SECURITY_MODEL.md`.
