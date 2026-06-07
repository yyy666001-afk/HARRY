Deployment steps

1) Backend (Render example)
- Create a new Web Service on Render.com, connect your GitHub repo `HARRY`.
- Build and start commands:
  - Build: leave empty
  - Start Command: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 3`
- Environment: set `PORT` to default (Render provides it automatically). Add any secrets if needed.
- Alternatively, on Heroku, push branch and set Procfile present.

2) After backend is deployed, get its HTTPS URL (e.g. `https://my-backend.onrender.com`).

3) Frontend (GitHub Pages)
- Option A (docs/ in main branch): place built static site in `docs/` and enable GitHub Pages from `main` branch -> `docs/` folder.
- Option B (gh-pages branch): create a `gh-pages` branch containing static files and enable GitHub Pages from that branch.

Commands to publish docs/ to gh-pages branch (run locally):

```bash
git checkout -b gh-pages
cp -r docs/* .
git add .
git commit -m "chore: publish static site to gh-pages"
git push origin gh-pages
```

4) Configure frontend to call backend:
- Edit `docs/config.js` and set:
  window.__API_BASE__ = 'https://my-backend.onrender.com'
- Commit and push the change to the branch used by GitHub Pages.

Notes:
- I cannot create Render/Heroku services without your cloud credentials. I prepared files and instructions; if you want I can attempt to push `gh-pages` branch from this environment (requires git remote auth)."