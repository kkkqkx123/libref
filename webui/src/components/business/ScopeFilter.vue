<template>
  <el-form :inline="true" class="libref-scope-filter" @submit.prevent>
    <el-form-item label="库">
      <el-select
        v-model="scopeStore.library"
        filterable
        clearable
        placeholder="全部文库"
        style="width: 200px"
        @change="onLibraryChange"
      >
        <el-option
          v-for="library in libraries"
          :key="`${library.ecosystem}/${library.name}`"
          :label="`${library.name}（${library.ecosystem}）`"
          :value="library.name"
        />
      </el-select>
    </el-form-item>
    <el-form-item label="版本">
      <el-select
        v-model="scopeStore.version"
        clearable
        placeholder="全部版本"
        style="width: 160px"
      >
        <el-option
          v-for="version in versions"
          :key="version.version"
          :label="version.version"
          :value="version.version"
        />
      </el-select>
    </el-form-item>
    <el-form-item label="生态">
      <el-input v-model="scopeStore.ecosystem" clearable placeholder="如 npm" style="width: 140px" />
    </el-form-item>
    <el-form-item v-if="showPreset" label="档位">
      <el-radio-group v-model="scopeStore.preset">
        <el-radio-button label="precise">精确</el-radio-button>
        <el-radio-button label="balanced">均衡</el-radio-button>
        <el-radio-button label="thorough">详尽</el-radio-button>
      </el-radio-group>
    </el-form-item>
  </el-form>
</template>

<script setup lang="ts">
import { ElForm, ElFormItem, ElInput, ElOption, ElRadioButton, ElRadioGroup, ElSelect } from 'element-plus';
import { computed, onMounted, watch } from 'vue';
import { api } from '../../api/client';
import type { Envelope, LibraryRow, VersionRow } from '../../api/types';
import { catalogStore } from '../../stores/catalog';
import { notifyError } from '../../stores/notify';
import { scopeStore, setLibrary } from '../../stores/scope';

withDefaults(defineProps<{ showPreset?: boolean }>(), { showPreset: false });

const libraries = computed(() => catalogStore.libraries);
const versions = computed<VersionRow[]>(() => catalogStore.versionsByLibrary[scopeStore.library] || []);

async function ensureLibraries(): Promise<void> {
  if (catalogStore.loaded) return;
  try {
    const { data } = await api<Envelope<LibraryRow>>('/catalog');
    catalogStore.libraries = data.items;
    catalogStore.loaded = true;
  } catch (error) {
    notifyError(error, '加载文库目录失败');
  }
}

async function ensureVersions(): Promise<void> {
  if (!scopeStore.library) return;
  if (catalogStore.versionsByLibrary[scopeStore.library]) return;
  try {
    const { data } = await api<Envelope<VersionRow>>('/catalog', {
      params: { library: scopeStore.library, ecosystem: scopeStore.ecosystem || undefined },
    });
    catalogStore.versionsByLibrary[scopeStore.library] = data.items;
  } catch (error) {
    notifyError(error, '加载版本列表失败');
  }
}

function onLibraryChange(): void {
  setLibrary(scopeStore.library);
  void ensureVersions();
}

onMounted(() => {
  void ensureLibraries().then(() => ensureVersions());
});

watch(
  () => scopeStore.library,
  () => {
    void ensureVersions();
  },
);
</script>
