// Catalog cache: library and version lists with explicit invalidation.
import { reactive } from 'vue';
import { api } from '../api/client';
import type { LibraryRow, VersionRow } from '../api/types';

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
  const { data } = await api('/catalog', { params: { ecosystem } });
  const items = data.items as LibraryRow[];
  catalogStore.libraries = items;
  catalogStore.loaded = true;
  return items;
}

export async function loadVersions(library: string, ecosystem?: string): Promise<VersionRow[]> {
  const { data } = await api('/catalog', { params: { library, ecosystem } });
  const items = data.items as VersionRow[];
  catalogStore.versionsByLibrary[library] = items;
  return items;
}

export function invalidateCatalog(): void {
  catalogStore.libraries = [];
  catalogStore.versionsByLibrary = {};
  catalogStore.loaded = false;
}
