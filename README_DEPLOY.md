Deployment steps

1) Backend (Render / Heroku / Railway)
- This repo includes a `Procfile` and `requirements.txt`. The recommended start command is:

  ```bash
  gunicorn app:app --bind 0.0.0.0:$PORT --workers 3
  ```

- Options:
  - Render: create a Web Service, connect GitHub repo, set start command above.
  - Heroku: create an app and enable GitHub deployment or use the Heroku API key.
  - Railway: connect repo or use Railway CLI to deploy.

2) CI / Automated deploy (GitHub Actions)
- This repo contains GitHub Actions workflows to deploy to multiple PaaS providers via `workflow_dispatch`:
  - `.github/workflows/deploy-heroku.yml` (requires `HEROKU_API_KEY`, `HEROKU_APP_NAME`, `HEROKU_EMAIL` secrets)
  - `.github/workflows/deploy-render.yml` (requires `RENDER_API_KEY`, `RENDER_SERVICE_ID`)
  - `.github/workflows/deploy-railway.yml` (requires `RAILWAY_TOKEN`, `RAILWAY_PROJECT_ID`)

3) After backend is deployed
- Obtain the backend HTTPS URL (e.g. `https://my-backend.onrender.com`). Then update the frontend config so GitHub Pages calls the public backend.

4) Update frontend `gh-pages`
- Quick manual method:

  ```bash
  ./scripts/update_config_and_publish.sh https://your-backend.example.com
  ```

- The script updates `config.js` and force-pushes to the `gh-pages` branch so the static site will call the correct backend.

5) Required repository secrets (examples)
- Heroku: `HEROKU_API_KEY`, `HEROKU_APP_NAME`, `HEROKU_EMAIL`
- Render: `RENDER_API_KEY`, `RENDER_SERVICE_ID`
- Railway: `RAILWAY_TOKEN`, `RAILWAY_PROJECT_ID`

Notes:
- I cannot create cloud services without your cloud credentials or connecting the provider in their UI. Instead, I added workflows and a publish script so you can run automated deploys by setting the recommended secrets in your repository settings and triggering the workflows.
- 如果你要我代為執行部署，請在此提供你要使用的平台與授權方式（或把必要的 repository secrets 設在 GitHub），我會在拿到授權後執行相應的 workflow 並把 `docs/config.js` 指向公開後端 URL。