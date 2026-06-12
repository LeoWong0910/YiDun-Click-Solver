# Publishing checklist

Before pushing or publishing this repository:

- Keep runtime artifacts out of Git: `captures/*` and `results/*` are ignored except `.gitkeep`.
- Do not commit cookies, request headers, HAR files, raw tokens, `validate`, `fp`, or private environment files.
- Keep `docs/assets/` limited to documentation images that do not contain reusable live credentials.
- Run a syntax sanity check:

```bash
python -m py_compile src/yidun_crypto.py src/yidun_ir.py src/yidun_vision.py tools/yidun_click_solver.py
```
