# CueSeal frontend

This Next.js scaffold lives alongside the Python backend. The first implementation milestone is the local report viewer in the [frontend checklist](../../../docs/frontend-todo.md). The [root README](../../../README.md) has the signed example and backend setup.

From this directory, use Node.js 22 and pnpm 10.20.0 for the development setup.

```sh
pnpm install --frozen-lockfile
pnpm dev
```

Open [the local app](http://localhost:3000). Check changes with `pnpm lint` and `pnpm build`. The production build uses `next/font` to fetch its configured fonts. For agent work, read the [local instructions](AGENTS.md) and start with the assigned frontend task ID.

## Third-party notices

The scaffold was generated with `create-next-app`. SoftAurora is incorporated into the app's landing page and adapted for this project. The respective notices and terms apply to those portions. [Next.js source](https://github.com/vercel/next.js/tree/v16.4.5) · [Next.js notice](licenses/next-js.txt) · [React Bits SoftAurora source](https://github.com/DavidHDev/react-bits/tree/9d9c687dc4f55b23539aa5417b703078047efa74/src/content/Backgrounds/SoftAurora) · [React Bits notice](licenses/react-bits.txt)
