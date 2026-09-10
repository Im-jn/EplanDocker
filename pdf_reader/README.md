# pdf_reader

Vite/TypeScript frontend for the Eplan processing service.

The root route and `/tasks` show the upload and task dashboard. `/viewer/<document_id>` opens the existing PDF object reader for a completed document. Both surfaces consume `/api/v1`; the frontend no longer starts Python processes or reads generated data from its source directory.

For local development, start the API first and then run:

```powershell
npm install
npm run dev
```

The Vite development proxy targets `http://127.0.0.1:8000` by default. Override it with `EPLAN_API_URL` when necessary.

Production is built and served by `docker/frontend.Dockerfile`. Nginx serves the SPA and proxies `/api/` to the API container.
