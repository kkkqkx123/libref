// Catalog cache: library and version lists with explicit invalidation.
import { reactive } from 'vue';
import { api } from '../api/client';
import type { Envelope, LibraryRow, VersionRow } from '../api/types';

interface CatalogState {
  libraries: LibraryRow[];
  versionsByLibrary: Record<string, VersionRow[]>;
  loaded: boolean;
}

export const catalogStore = reactive<CatalogState>({
  libraries: [],
  versionsByLibrary: {},
  loaded: false,
});

export async function loadLibraries(ecosystem?: string): Promise<LibraryRow[]> {
  const { data } = await api<Envelope<LibraryRow>>('/catalog', {
    params: { ecosystem },
  });
  catalogStore.libraries = data.items;
  catalogStore.loaded = true;
  return data.items;
}

export async function loadVersions(library: string, ecosystem?: string): Promise<VersionRow[]> {
  const { data } = await api<Envelope<VersionRow>>('/catalog', {
    params: { library, ecosystem },
  });
  catalogStore.versionsByLibrary[library] = data.items;
  return data.items;
}

export function invalidateCatalog(): void {
  catalogStore.libraries = [];
  catalogStore.versionsByLibrary = {};
  catalogStore.loaded = false;
}
