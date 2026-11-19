import { defineConfig, mergeConfig } from 'vite';
import viteConfig from './vite.config';

// Reuses the app's own Vite pipeline so tests resolve `.vue` files, aliases and
// `import.meta.env` exactly as the production build does. happy-dom keeps the
// suite fast while still providing localStorage, fetch and DOM events.
export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      environment: 'happy-dom',
      include: ['src/**/*.spec.ts'],
      setupFiles: ['./src/test-setup.ts'],
      restoreMocks: true,
      clearMocks: true,
      unstubGlobals: true,
    },
  }),
);
