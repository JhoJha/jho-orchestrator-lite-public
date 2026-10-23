# Release / GitHub Checklist

Before the first push:

- [ ] `python scripts/validate_repo.py`
- [ ] in-memory compile of every `runner/*.py` with `PYTHONDONTWRITEBYTECODE=1`
- [ ] `PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p 'test_*.py'`
- [ ] inspect `git diff --cached`
- [ ] confirm no `.env`, credentials, telemetry, logs, outputs or Windows bridge files are staged
- [ ] confirm `protoagent.lock`
- [ ] confirm `HANDOFF_CURRENT.md`
- [ ] create private GitHub repository
- [ ] push `main`
- [ ] create tag `v0.2.5`
- [ ] keep repository private until security review says otherwise
