# libref webui

Vue 3 + Element Plus single-page app. The Python service is the only backend;
this project never talks to any other layer. Interface types mirror
`src/libref/web/api.py` (refresh the snapshot with `npm run gen:api`).

## Develop

```sh
npm install
npm run dev        # serves on :5173, calls the API at VITE_API_BASE
```

`VITE_API_BASE` defaults to the same origin in production builds and points at
`http://127.0.0.1:8001` under `.env.development`.

## Verify

```sh
npm run check      # vue-tsc plus design token check
npm run build      # type check then production build into dist/
npm run gen:api    # refresh src/api/schema.d.ts from a running backend
```

## Deploy

Copy `dist/` to the directory configured as `web_spa_dir`; the Python service
hosts it with a last-resort SPA fallback. Missing directory or `index.html`
keeps pure-API mode with a startup warning.
