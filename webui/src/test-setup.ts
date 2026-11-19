import { Window } from 'happy-dom';

// Recent Node runtimes expose a disabled localStorage getter that shadows the
// one populated by happy-dom. Give browser code a working storage instance.
const storage = new Window({ url: 'http://localhost:3000' }).localStorage;
Object.defineProperty(globalThis, 'localStorage', {
  configurable: true,
  writable: true,
  value: storage,
});
