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
`http://127.0.0.1:8001` under `.env.development` (the service allows CORS in dev,
so no dev proxy is configured).

## Verify

```sh
npm test           # vitest: unit and component specs under src/**/*.spec.ts
npm run check      # vue-tsc, message catalogue check, design token check
npm run build      # type check then production build into dist/
npm run gen:api    # export the OpenAPI snapshot and regenerate src/api/schema.d.ts
```

Specs sit next to the code they cover and are type-checked by `vue-tsc` along with
everything else. Pure predicates (permission gates, role combinations, the
navigation table) are tested directly; components are mounted with element-plus
left real unless a dialog needs its inputs driven, in which case only the widgets
under test are stubbed. Rationale and coverage map: `docs/plan/frontend-testing.md`.

`gen:api` runs `tools/export_openapi.py` from the repository root, which builds the
app object directly — no backend has to be running. Both the snapshot and
`schema.d.ts` are local-only (see `.gitignore`); `src/api/types.ts` is the
hand-maintained mirror, and `tests/integration/test_openapi_contract.py` keeps the
response models honest. A reachable backend at `LIBREF_BASE_URL` is used only when
the offline export is unavailable.

## Deploy

The image builds `dist/` in a Node stage and the Python service serves it with a
last-resort SPA fallback, so no manual copy is needed in the compose stack.
For nginx-side static delivery, copy `dist/` to the nginx root and use
`deploy/nginx.static.conf.example`; the backend must serve the same artifact
for client-side deep links. A missing directory or `index.html` keeps pure-API
mode with a startup warning.
