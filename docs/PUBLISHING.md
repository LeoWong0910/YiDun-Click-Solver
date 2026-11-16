# Publishing checklist

Before pushing or publishing this repository:

- Keep runtime artifacts out of Git: `captures/*` and `results/*` are ignored except `.gitkeep`.
- Do not commit cookies, request headers, HAR files, raw tokens, `validate`, `fp`, or private environment files.
- Keep `docs/assets/` limited to documentation images that do not contain reusable live credentials.
- Run a syntax sanity check:

```bash
python -m py_compile src/yidun_crypto.py src/yidun_ir.py src/yidun_vision.py \
  src/notify.py src/recognize.py \
  tools/yidun_click_solver.py tools/nas_api_server.py
python -m unittest discover -s tests -v
```

- Synology deploy files (`Dockerfile`, `compose.yaml`, `scripts/deploy-synology.sh`, `.env.example`) should stay free of real `YIDUN_API_TOKEN`, SMTP passwords, and push tokens.
