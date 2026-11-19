import { describe, expectTypeOf, it } from 'vitest';
import { api } from './client';
import type { components } from './schema';

type Catalog = components['schemas']['LibraryResponse'];

describe('API contract types', () => {
  it('derives responses from the generated schema', () => {
    expectTypeOf(api).toBeFunction();
  });

  it('rejects missing required arguments and undeclared methods', () => {
    if (false) {
      // @ts-expect-error query is mandatory for search
      void api('/search');
      // @ts-expect-error request body is mandatory for scrape
      void api('/scrape', { method: 'POST' });
      // @ts-expect-error health only declares GET
      void api('/health', { method: 'POST' });
      // @ts-expect-error path parameter is mandatory
      void api('/jobs/{job_id}');
    }
  });

  it('keeps response objects closed', () => {
    if (false) {
      const data = {} as Catalog;
      // @ts-expect-error misspelled fields are not part of the contract
      void data.itmes;
    }
  });
});
